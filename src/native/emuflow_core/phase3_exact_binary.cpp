#include "emuflow_core/phase3_exact_binary.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/native_stages.hpp"
#include "emuflow_core/store.hpp"
#include <algorithm>
#include <array>
#include <filesystem>
#include <map>
#include <memory>
#include <set>
#include <fstream>
#include <unistd.h>

namespace emuflow::core {
namespace {
using U=std::uint64_t;
namespace fs=std::filesystem;
constexpr U kMagic=0x5443415845554d45ULL,kPreamble=28;
constexpr std::uint32_t kMissing=UINT32_MAX;
void Check(bool value,const std::string& reason) {
  if(!value)throw StageError(ErrorCode::kInvalidArtifact,"Static Exact binary "+reason);
}
std::string Hex(const std::array<unsigned char,32>& value) {
  std::string text;const char* digits="0123456789abcdef";
  for(auto byte:value){text+=digits[byte>>4];text+=digits[byte&15];}return text;
}
std::string Hash(const std::string& text) {
  Sha256 hash;hash.update(reinterpret_cast<const unsigned char*>(text.data()),text.size());return Hex(hash.finish());
}
class Json {
 public:
  explicit Json(std::string text):db(":memory:",SqliteDatabase::Mode::kReadOnly),text(std::move(text)) {
    Check(this->text.size()<=8*1024*1024,"manifest exceeds bound");
    auto row=db.Prepare("SELECT json_valid(?1),json_type(?1)");row.BindText(1,this->text);
    Check(row.Step() && row.Integer(0)==1 && row.Text(1)=="object","manifest is invalid");
    auto duplicate=db.Prepare("SELECT 1 FROM json_tree(?1) WHERE parent IS NOT NULL GROUP BY parent,key HAVING count(*)>1 LIMIT 1");
    duplicate.BindText(1,this->text);Check(!duplicate.Step(),"manifest repeats a JSON key");
    auto keys=db.Prepare("SELECT key FROM json_tree(?1) WHERE typeof(key)='text'");keys.BindText(1,this->text);
    while(keys.Step())Check(keys.TypedText(0).find('\0')==std::string::npos,"manifest member name contains NUL");
  }
  U Integer(const std::string& path) {
    auto row=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2),typeof(json_extract(?1,?2))");row.BindText(1,text);row.BindText(2,path);
    Check(row.Step() && row.Text(0)=="integer" && row.Text(2)=="integer" && row.TypedInteger(1)>=0,"population count is invalid: "+path);
    return row.Integer(1);
  }
  std::string String(const std::string& path) {
    auto row=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");row.BindText(1,text);row.BindText(2,path);
    Check(row.Step() && row.Text(0)=="text","manifest string is invalid: "+path);
    const auto dot=path.rfind('.');Check(dot!=std::string::npos,"manifest string path is invalid");
    return DecodeJsonString(JsonMemberToken(db,Object(path.substr(0,dot)),path.substr(dot+1)));
  }
  std::string Object(const std::string& path) {
    auto row=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");row.BindText(1,text);row.BindText(2,path);
    Check(row.Step() && row.Text(0)=="object","manifest object is invalid: "+path);return row.TypedText(1);
  }
  std::vector<std::string> Parts() {
    auto type=db.Prepare("SELECT json_type(?1,'$.parts')");type.BindText(1,text);
    Check(type.Step() && type.Text(0)=="array","parts are invalid");
    auto row=db.Prepare("SELECT json_extract(?1,'$.parts')");row.BindText(1,text);Check(row.Step(),"parts are missing");
    const auto raw=row.TypedText(0);std::vector<std::string> parts;std::size_t position=1;
    // JSON1 has validated syntax and array type. Decode only its bounded raw
    // string spans, preserving escaped NUL/Unicode on older SQLite versions.
    while(position<raw.size()) {
      while(position<raw.size() && (raw[position]==' ' || raw[position]=='\n' || raw[position]=='\r' || raw[position]=='\t' || raw[position]==','))++position;
      Check(position<raw.size(),"part array is truncated");if(raw[position]==']')break;
      const auto begin=position++;Check(raw[begin]=='"',"part is invalid");
      bool ended=false;
      while(position<raw.size()) {auto ch=raw[position++];if(ch=='\\'){Check(position<raw.size(),"part is truncated");++position;}else if(ch=='"'){ended=true;break;}}
      Check(ended,"part is truncated");auto part=DecodeJsonString(raw.substr(begin,position-begin));
      Check(parts.size()<255 && !part.empty() && part.find('\0')==std::string::npos && (parts.empty() || parts.back()<part),"parts are invalid");parts.push_back(std::move(part));
    }
    Check(!parts.empty(),"parts are invalid");return parts;
  }
  SqliteDatabase db;std::string text;
};
struct Section {U offset=0,bytes=0,records=0;std::string sha;};
class Reader {
 public:
  Reader(const ArtifactRef& source,const Section& section):reader(source.path()),left(section.bytes),expected(section.sha) {
    reader.Seek(section.offset);
  }
  template<typename T>T Read(){T value;Bytes(&value,sizeof value);return value;}
  void Bytes(void* target,std::size_t bytes) {
    Check(bytes<=left,"record exceeds its section");reader.ReadBytes(target,bytes);
    hash.update(static_cast<const unsigned char*>(target),bytes);left-=bytes;
  }
  std::string String() {
    const auto size=Read<std::uint32_t>();Check(size && size<=left,"string is invalid");
    std::string text(size,'\0');Bytes(text.data(),size);JsonQuoteUtf8(text);return text;
  }
  void Finish(){Check(!left,"section has trailing bytes");Check(Hex(hash.finish())==expected,"section digest disagrees");}
  BinaryReader reader;U left;Sha256 hash;std::string expected;
};
struct Counts {
  U nodes=0,sources=0,segments=0,consumers=0,captures=0;
  std::string JsonText() const {
    return JsonRecord(std::map<std::string,JsonRecord>{{"cut_nodes",nodes},{"source_segments",sources},
      {"logic_segments",segments},{"consumer_segments",consumers},{"capture_requirements",captures}}).Encode();
  }
};
struct Manifest {
  unsigned version=0;U offset=0,length=0;std::string encoded;
  std::unique_ptr<Json> json;std::vector<std::string> parts;std::map<std::string,Section> sections;Counts counts;
};
std::string SectionPath(const std::string& name){return "$.sections.\""+name+"\"";}
std::string Schema(unsigned version){return "emuflow.phase3-static-exact-binary/v"+std::to_string(version);}
Manifest Load(const ArtifactRef& artifact,const std::string& descriptor) {
  artifact.RequireUnchanged();BinaryReader file(artifact.path());Manifest m;
  Check(file.size()>=kPreamble && file.Read<U>()==kMagic,"preamble is invalid");m.version=file.Read<std::uint32_t>();
  m.offset=file.Read<U>();m.length=file.Read<U>();
  Check((m.version==1 || m.version==2) && artifact.schema()=="phase3-exact-data-v"+std::to_string(m.version) &&
        m.offset>=kPreamble && m.offset<=file.size() && m.length && m.length==file.size()-m.offset && m.length<=8*1024*1024,
        "preamble is invalid");
  file.Seek(m.offset);m.encoded.resize(m.length);file.ReadBytes(m.encoded.data(),m.encoded.size());file.RequireEnd();
  m.json=std::make_unique<Json>(m.encoded);auto& json=*m.json;Json expected(descriptor);
  Check(json.String("$.schema")==Schema(m.version),"schema is invalid");
  Check(expected.String("$.artifact")=="assignment.exact.bin" && expected.String("$.schema")==Schema(m.version) &&
        expected.Integer("$.bytes")==file.size() && expected.String("$.manifest_sha256")==Hash(m.encoded) &&
        expected.String("$.semantic_contract_schema")==json.String("$.semantic_contract_schema") &&
        expected.String("$.semantic_contract_sha256")==json.String("$.semantic_contract_sha256"),"descriptor disagrees");
  RequireEquivalentJson(json.db,expected.Object("$.counts"),json.Object("$.counts"),"Static Exact binary descriptor counts disagree");
  if(m.version==2)Check(json.Integer("$.capture_bit_max_bits")==8192 && expected.Integer("$.capture_bit_max_bits")==8192,
                        "capture-bit limit is invalid");
  m.parts=json.Parts();std::set<std::string> names{"nets","node_sources"};
  for(unsigned p=0;p<m.parts.size();++p)for(const auto* kind:{"segments","captures"})names.insert(std::string(kind)+":"+std::to_string(p));
  auto section_names=json.db.Prepare("SELECT key,type FROM json_each(?1,'$.sections')");section_names.BindText(1,json.text);
  std::vector<std::pair<U,U>> occupied;
  while(section_names.Step()) {
    const auto name=section_names.TypedText(0);Check(names.erase(name)==1 && section_names.Text(1)=="object","sections are incomplete");
    const auto path=SectionPath(name);Section s{json.Integer(path+".offset"),json.Integer(path+".bytes"),json.Integer(path+".records"),json.String(path+".sha256")};
    const U minimum=name=="nets"?5:name=="node_sources"?4:14;
    Check(s.offset>=kPreamble && s.offset<=m.offset && s.bytes<=m.offset-s.offset && s.sha.size()==64 && s.records<=s.bytes/minimum,
          "section count or descriptor is invalid");
    if(s.bytes)occupied.emplace_back(s.offset,s.offset+s.bytes);m.sections.emplace(name,std::move(s));
  }
  Check(names.empty(),"sections are incomplete");std::sort(occupied.begin(),occupied.end());
  for(std::size_t i=1;i<occupied.size();++i)Check(occupied[i-1].second<=occupied[i].first,"sections overlap");
  auto& c=m.counts;c={json.Integer("$.counts.cut_nodes"),json.Integer("$.counts.source_segments"),json.Integer("$.counts.logic_segments"),
      json.Integer("$.counts.consumer_segments"),json.Integer("$.counts.capture_requirements")};
  U segments=0,captures=0;
  for(unsigned p=0;p<m.parts.size();++p){segments+=m.sections.at("segments:"+std::to_string(p)).records;captures+=m.sections.at("captures:"+std::to_string(p)).records;}
  const auto& nodes=m.sections.at("node_sources");
  Check(c.nodes==m.sections.at("nets").records && c.nodes==nodes.records && c.nodes<kMissing &&
        c.sources<kMissing && c.segments<kMissing && c.captures<kMissing &&
        c.sources<=(nodes.bytes-4*nodes.records)/4 && c.segments==segments && c.consumers<=c.segments && c.captures==captures,
        "population counts disagree");return m;
}
void Validate(const ArtifactRef& artifact,Manifest& m) {
  const auto& c=m.counts;
  {Reader nets(artifact,m.sections.at("nets"));std::string previous;
    for(U i=0;i<c.nodes;++i){auto name=nets.String();Check(!i || previous<name,"cut nets are invalid");previous=std::move(name);}nets.Finish();}
  // Prove nested source counts before allocating any population-sized buffers.
  {Reader sources(artifact,m.sections.at("node_sources"));U count=0;
    for(U i=0;i<c.nodes;++i){const auto n=sources.Read<std::uint32_t>();Check(n<=sources.left/4 && n<=c.sources-count,"source population disagrees");
      for(U j=0;j<n;++j)sources.Read<std::uint32_t>();count+=n;}
    sources.Finish();Check(count==c.sources,"source population disagrees");}
  std::vector<std::uint32_t> capture_nets(c.captures,kMissing);std::vector<unsigned char> capture_parts(c.captures,255),capture_bound(c.captures);
  U observed_captures=0;
  for(unsigned p=0;p<m.parts.size();++p) {
    const auto& section=m.sections.at("captures:"+std::to_string(p));Reader captures(artifact,section);
    for(U i=0;i<section.records;++i) {
      const auto number=captures.Read<std::uint32_t>(),net=captures.Read<std::uint32_t>();const auto kind=captures.Read<std::uint8_t>();
      Check(number<c.captures && net<c.nodes && kind<=1 && capture_parts[number]==255,"capture record or identity repeats");
      capture_parts[number]=p;capture_nets[number]=net;++observed_captures;captures.String();
      if(kind) {
        captures.String();
        if(m.version==1)captures.Read<std::uint32_t>();
        else {
          const auto bytes=captures.Read<std::uint32_t>();
          Check(bytes<=1024,"capture bit must be a non-negative integer of at most 8192 bits");
          Check(bytes && bytes<=captures.left,"capture bit is invalid");std::array<unsigned char,1024> bit{};captures.Bytes(bit.data(),bytes);
          Check(bytes==1 || bit[0],"capture bit is not canonical");
        }
      }
    }
    captures.Finish();
  }
  Check(observed_captures==c.captures,"capture coverage disagrees");
  std::vector<unsigned char> seen(c.segments);std::vector<std::uint32_t> targets(c.segments,kMissing);
  U segments=0,dependencies=0,consumers=0;
  for(unsigned p=0;p<m.parts.size();++p) {
    const auto& section=m.sections.at("segments:"+std::to_string(p));Reader records(artifact,section);
    for(U i=0;i<section.records;++i) {
      const auto number=records.Read<std::uint32_t>();const auto kind=records.Read<std::uint8_t>(),flags=records.Read<std::uint8_t>();
      const auto source=records.Read<std::uint32_t>(),target=records.Read<std::uint32_t>();
      Check(number<c.segments && !seen[number] && kind<=2 && !(flags&~1U),"segment flags or identity repeats");seen[number]=1;++segments;
      if(kind==0){Check(source==kMissing && target<c.nodes,"launch segment is invalid");targets[number]=target;}
      else if(kind==1){Check(!flags && source<c.nodes && target<c.nodes,"dependency segment is invalid");targets[number]=target;++dependencies;++consumers;}
      else {
        Check(!flags && source<c.nodes && target<c.captures,"capture segment is invalid");
        Check(!capture_bound[target] && capture_parts[target]==p && capture_nets[target]==source,"capture binding disagrees");capture_bound[target]=1;++consumers;
      }
    }
    records.Finish();
  }
  Check(segments==c.segments && consumers==c.consumers,"segment or consumer population disagrees");
  Check(std::all_of(capture_bound.begin(),capture_bound.end(),[](auto value){return value!=0;}),"capture binding is incomplete");
  std::vector<std::uint32_t>().swap(capture_nets);std::vector<unsigned char>().swap(capture_parts);std::vector<unsigned char>().swap(capture_bound);
  std::fill(seen.begin(),seen.end(),0);U sources=0;Reader records(artifact,m.sections.at("node_sources"));
  for(U net=0;net<c.nodes;++net) {
    const auto count=records.Read<std::uint32_t>();Check(count<=records.left/4,"source count is invalid");
    for(U i=0;i<count;++i){const auto number=records.Read<std::uint32_t>();
      Check(number<c.segments && !seen[number] && targets[number]==net,"source segment is invalid");seen[number]=1;++sources;}
  }
  records.Finish();Check(sources==c.sources,"source population disagrees");
  for(U i=0;i<c.segments;++i)Check(targets[i]==kMissing || seen[i],"source coverage is incomplete");
  auto& json=*m.json;
  Check(json.Integer("$.contract_header.metrics.transported_cut_nets")==c.nodes && json.Integer("$.contract_header.metrics.logic_segments")==c.segments &&
        json.Integer("$.contract_header.metrics.capture_requirements")==c.captures && json.Integer("$.contract_header.metrics.dependency_edges")==dependencies,"metrics disagree");
  artifact.RequireUnchanged();
}
}
void ProjectExactBoundarySegments(const ArtifactRef& artifact,const std::string& descriptor,
                                 const std::string& fpga,SqliteDatabase& db){
  auto m=Load(artifact,descriptor);const auto part=std::find(m.parts.begin(),m.parts.end(),fpga);
  Check(part!=m.parts.end(),"boundary partition is missing");
  db.Execute("CREATE TEMP TABLE exact_boundary_segments(net_id TEXT,side INTEGER,ordinal INTEGER,segment_id TEXT,PRIMARY KEY(net_id,side,ordinal)) WITHOUT ROWID;"
      "CREATE TEMP TABLE exact_cut_ordinals(seq INTEGER PRIMARY KEY,net_id TEXT NOT NULL UNIQUE);"
      "CREATE TEMP TABLE selected_exact_segments(number INTEGER PRIMARY KEY,source INTEGER,kind INTEGER)");
  const auto name=[](std::uint32_t number){auto digits=std::to_string(number);return "segment"+std::string(digits.size()<6?6-digits.size():0,'0')+digits;};
  {
    Reader nets(artifact,m.sections.at("nets")),sources(artifact,m.sections.at("node_sources"));
    auto net=db.Prepare("INSERT INTO exact_cut_ordinals VALUES (?,?)");
    auto member=db.Prepare("INSERT INTO exact_boundary_segments SELECT ?,0,?,? WHERE EXISTS(SELECT 1 FROM exact_info WHERE net_id=? AND role='cut_source')");
    U total=0;std::string previous;
    for(U i=0;i<m.counts.nodes;++i){
      const auto identity=nets.String();Check(!i || previous<identity,"cut nets are invalid");previous=identity;
      net.BindInteger(1,i);net.BindText(2,identity);net.Execute();
      const auto count=sources.Read<std::uint32_t>();Check(count<=sources.left/4 && count<=m.counts.sources-total,"source membership count disagrees");total+=count;
      for(U ordinal=0;ordinal<count;++ordinal){const auto number=sources.Read<std::uint32_t>();Check(number<m.counts.segments,"source segment is invalid");
        member.BindText(1,identity);member.BindInteger(2,ordinal);member.BindText(3,name(number));member.BindText(4,identity);member.Execute();}
    }
    Check(total==m.counts.sources,"source membership coverage disagrees");nets.Finish();sources.Finish();
  }
  {
    const auto& section=m.sections.at("segments:"+std::to_string(part-m.parts.begin()));Reader segments(artifact,section);
    auto put=db.Prepare("INSERT INTO selected_exact_segments VALUES (?,?,?)");
    auto member=db.Prepare("INSERT INTO exact_boundary_segments SELECT n.net_id,1,?,? FROM exact_cut_ordinals n JOIN exact_info i ON i.net_id=n.net_id WHERE n.seq=? AND i.role='transport_shadow_sink'");
    for(U i=0;i<section.records;++i){
      const auto number=segments.Read<std::uint32_t>();const auto kind=segments.Read<std::uint8_t>(),flags=segments.Read<std::uint8_t>();
      const auto source=segments.Read<std::uint32_t>(),target=segments.Read<std::uint32_t>();
      Check(number<m.counts.segments && kind<=2 && !(flags&~1U),"boundary segment flags are invalid");
      Check(kind==0?(source==kMissing && target<m.counts.nodes):(!flags && source<m.counts.nodes && target<(kind==1?m.counts.nodes:m.counts.captures)),"boundary segment coordinate is invalid");
      put.BindInteger(1,number);put.BindInteger(2,source);put.BindInteger(3,kind);put.Execute();
      if(kind){member.BindInteger(1,number);member.BindText(2,name(number));member.BindInteger(3,source);member.Execute();}
    }
    segments.Finish();
  }
  db.Execute("DROP TABLE exact_cut_ordinals;DROP TABLE selected_exact_segments");artifact.RequireUnchanged();
}

void ProjectExactCoverageSegments(const ArtifactRef& artifact,const std::string& descriptor,
                                  SqliteDatabase& db) {
  auto manifest=Load(artifact,descriptor);
  auto insert=db.Prepare("INSERT INTO expected_logic VALUES(?,?,?,?)");
  U total=0;
  for(unsigned part=0;part<manifest.parts.size();++part) {
    const auto& section=manifest.sections.at("segments:"+std::to_string(part));Reader records(artifact,section);
    for(U i=0;i<section.records;++i) {
      const auto number=records.Read<std::uint32_t>();
      const auto kind=records.Read<std::uint8_t>(),flags=records.Read<std::uint8_t>();
      const auto source=records.Read<std::uint32_t>(),target=records.Read<std::uint32_t>();
      Check(number<manifest.counts.segments && kind<=2 && !(flags&~1U),"coverage segment identity/flags disagree");
      Check(kind==0?(source==kMissing && target<manifest.counts.nodes):
          (!flags && source<manifest.counts.nodes && target<(kind==1?manifest.counts.nodes:manifest.counts.captures)),
          "coverage segment coordinates disagree");
      auto digits=std::to_string(number);
      insert.BindText(1,"segment"+std::string(digits.size()<6?6-digits.size():0,'0')+digits);
      insert.BindText(2,manifest.parts[part]);
      insert.BindText(3,kind==0?"launch_to_tx":kind==1?"rx_to_tx":"rx_to_capture");
      insert.BindInteger(4,flags);insert.Execute();++total;
    }
    records.Finish();
  }
  Check(total==manifest.counts.segments,"coverage segment population disagrees");
  artifact.RequireUnchanged();
}

std::string ValidateExactBinaryArtifact(const ArtifactRef& artifact,const std::string& descriptor) {
  auto manifest=Load(artifact,descriptor);Validate(artifact,manifest);const auto& c=manifest.counts;
  std::map<std::string,JsonRecord> summary{{"status","pass"},{"schema",Schema(manifest.version)},
    {"semantic_contract_sha256",manifest.json->String("$.semantic_contract_sha256")},{"cut_nodes",c.nodes},{"source_segments",c.sources},
    {"logic_segments",c.segments},{"capture_requirements",c.captures},
    {"dependency_edges",manifest.json->Integer("$.contract_header.metrics.dependency_edges")}};
  if(manifest.version==2)summary["capture_bit_max_bits"]=8192;return JsonRecord(summary).Encode();
}
void ValidateExactBinary(const std::vector<ArtifactRef>& inputs,const std::string& summary) {
  Check(inputs.size()==2,"validation source coverage disagrees");CheckedRequestReader request(inputs[1].path(),8*1024*1024);
  Check(request.Read<U>()==0x31565145554d5545ULL && request.Read<std::uint32_t>()==1,"validation request is invalid");
  const auto version=request.Read<std::uint32_t>();
  Check(version==1 || version==2,"validation format version is invalid");
  Check(inputs[0].schema()=="phase3-exact-data-v"+std::to_string(version),"validation format schema disagrees");
  if(!(ReadFileGeneration(request)==inputs[0].generation()))throw StageError(ErrorCode::kMixedGeneration,"Static Exact binary source generation changed");
  const auto descriptor=request.ReadString32(false,8*1024*1024);request.RequireEnd();
  const auto text=ValidateExactBinaryArtifact(inputs[0],descriptor);PublishTextArtifact(summary,inputs,[&](std::ostream& out){out<<text<<'\n';});
}
namespace {
std::string ObjectText(const std::map<std::string,std::string>& fields) {
  std::string result="{";for(const auto& field:fields){if(result.size()>1)result+=',';result+=JsonQuote(field.first)+':'+field.second;}return result+'}';
}
void WriteString(AtomicBinaryWriter& writer,const std::string& value) {
  Check(!value.empty() && value.size()<UINT32_MAX,"section request identity is invalid");writer.Write(std::uint32_t(value.size()));writer.WriteBytes(value.data(),value.size());
}
struct Workspace {
  fs::path root;
  explicit Workspace(const fs::path& parent) {
    auto path=(parent/".exact-binary-XXXXXX").string();Check(mkdtemp(path.data())!=nullptr,"cannot create private publication directory");root=path;
  }
  ~Workspace(){std::error_code error;fs::remove_all(root,error);}
};
}
StageResult PublishExactBinary(const std::vector<ArtifactRef>& inputs,const std::string& directory) {
  return RunStage("phase3.publish-exact-binary",[&]{for(const auto& source:inputs)source.RequireUnchanged();return inputs;},[&](const auto& refs) {
    Check(refs.size()==3 && refs[0].schema()=="exact-nodes-v2" && refs[1].schema()=="exact-captures-v1" &&
          refs[2].schema()=="exact-binary-publication-request-v1","publication sources disagree");
    CheckedRequestReader request(refs[2].path(),8*1024*1024);
    Check(request.Read<U>()==0x31504245554d5545ULL && request.Read<std::uint32_t>()==1,"publication request is invalid");
    const auto version=request.Read<std::uint32_t>();Check(version==1 || version==2,"publication format is invalid");
    for(unsigned i=0;i<2;++i)if(!(ReadFileGeneration(request)==refs[i].generation()))
      throw StageError(ErrorCode::kMixedGeneration,"Static Exact binary original semantic generation changed");
    const auto count=request.Read<std::uint32_t>();Check(count && count<=255,"publication parts are invalid");
    std::vector<std::string> parts;std::vector<JsonRecord> part_json;
    for(unsigned p=0;p<count;++p){auto part=request.ReadString32();JsonQuote(part);
      Check(part.find('\0')==std::string::npos && (parts.empty() || parts.back()<part),"publication parts are invalid");parts.push_back(part);part_json.emplace_back(part);}
    const auto header=request.ReadString32(false,8*1024*1024),semantic=request.ReadString32();request.RequireEnd();Json contract(header);
    Check(semantic.size()==64 && std::all_of(semantic.begin(),semantic.end(),[](auto c){return (c>='0' && c<='9') || (c>='a' && c<='f');}),"semantic digest is invalid");
    const auto schema=contract.String("$.schema");Check(schema=="emuflow.static-exact-combinational-cut/v3","semantic schema is invalid");
    const auto destination=fs::absolute(directory);Check(!fs::exists(destination) && !fs::is_symlink(destination),"publication directory must be absent");
    fs::create_directories(destination.parent_path());Workspace workspace(destination.parent_path());
    const auto selected=workspace.root/"selected";fs::create_directory(selected);
    const auto data=selected/"assignment.exact.bin",section_request=workspace.root/"sections.bin",section_summary=workspace.root/"counts.bin";
    {
      AtomicBinaryWriter writer(section_request.string(),refs);writer.Write(U(0x31574553464d5545ULL));writer.Write(std::uint32_t(1));
      writer.Write(version);writer.Write(count);for(const auto& part:parts)WriteString(writer,part);WriteString(writer,data.string());writer.Commit();
    }
    emuflow::native::WriteStaticExactSections({refs[0],refs[1],ArtifactRef::Bind(section_request.string(),"exact-section-request-v1")},section_summary.string());
    BinaryReader summary(section_summary.string());const U sections=2+2*U(count);
    Check(summary.size()==16+56*sections+40 && summary.Read<U>()==0x314f5753464d5545ULL &&
          summary.Read<std::uint32_t>()==2 && summary.Read<std::uint32_t>()==sections,"section summary is invalid");
    std::vector<std::string> names{"nets","node_sources"};
    for(unsigned p=0;p<count;++p){names.push_back("segments:"+std::to_string(p));names.push_back("captures:"+std::to_string(p));}
    std::map<std::string,JsonRecord> section_json;U cursor=kPreamble;
    for(const auto& name:names){const auto offset=summary.Read<U>(),bytes=summary.Read<U>(),records=summary.Read<U>();std::array<unsigned char,32> hash{};summary.ReadBytes(hash.data(),hash.size());
      Check(offset==cursor && bytes<=UINT64_MAX-cursor,"section extents disagree");cursor+=bytes;
      section_json.emplace(name,std::map<std::string,JsonRecord>{{"offset",offset},{"bytes",bytes},{"records",records},{"sha256",Hex(hash)}});}
    Counts c{summary.Read<U>(),summary.Read<U>(),summary.Read<U>(),summary.Read<U>(),summary.Read<U>()};summary.RequireEnd();
    Check(fs::file_size(data)==cursor,"section extent disagrees with file");
    std::map<std::string,std::string> fields{{"schema",JsonQuote(Schema(version))},{"semantic_contract_schema",JsonQuote(schema)},
      {"semantic_contract_sha256",JsonQuote(semantic)},{"contract_header",header},{"parts",JsonRecord(part_json).Encode()},
      {"counts",c.JsonText()},{"sections",JsonRecord(section_json).Encode()}};
    if(version==2)fields["capture_bit_max_bits"]="8192";const auto manifest=ObjectText(fields);
    // Only this invocation owns the still-private section file. Append the
    // bounded manifest and seal its preamble in place, without a second copy.
    {
      std::fstream file(data,std::ios::binary|std::ios::in|std::ios::out);Check(bool(file),"cannot finish private file");
      file.seekp(cursor);file.write(manifest.data(),manifest.size());file.seekp(0);
      const auto put=[&](const auto value){file.write(reinterpret_cast<const char*>(&value),sizeof value);};
      put(kMagic);put(version);put(cursor);put(U(manifest.size()));file.flush();Check(bool(file),"cannot close private file");file.close();Check(!file.fail(),"cannot close private file");
    }
    std::map<std::string,std::string> descriptor{{"artifact",JsonQuote("assignment.exact.bin")},{"schema",JsonQuote(Schema(version))},
      {"bytes",std::to_string(fs::file_size(data))},{"manifest_sha256",JsonQuote(Hash(manifest))},{"semantic_contract_schema",JsonQuote(schema)},
      {"semantic_contract_sha256",JsonQuote(semantic)},{"counts",c.JsonText()}};
    if(version==2)descriptor["capture_bit_max_bits"]="8192";const auto text=ObjectText(descriptor);
    const auto binary=ArtifactRef::Bind(data.string(),"phase3-exact-data-v"+std::to_string(version));
    (void)ValidateExactBinaryArtifact(binary,text);
    auto bindings=refs;bindings.push_back(binary);PublishTextArtifact((selected/"descriptor.json").string(),bindings,[&](std::ostream& out){out<<text<<'\n';});
    const auto ready=destination.parent_path()/(workspace.root.filename().string()+"-publish");fs::rename(selected,ready);
    try{PublishNewDirectory(ready.string(),destination.string(),refs);}catch(...){std::error_code error;fs::remove_all(ready,error);throw;}
  },[&]{
    // The version is already validated by the completed production request.
    CheckedRequestReader request(inputs.at(2).path(),8*1024*1024);request.Read<U>();request.Read<std::uint32_t>();const auto version=request.Read<std::uint32_t>();
    return std::vector<ArtifactRef>{ArtifactRef::Bind((fs::path(directory)/"assignment.exact.bin").string(),"phase3-exact-data-v"+std::to_string(version)),
      ArtifactRef::Bind((fs::path(directory)/"descriptor.json").string(),"exact-binary-descriptor-v1")};
  });
}
}
