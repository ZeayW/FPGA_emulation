#include "emuflow_core/exact_semantic_view.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/json_record.hpp"
#include <algorithm>
#include <filesystem>
#include <map>
#include <set>

namespace emuflow::core {
namespace {
void Check(bool valid, const char* message) {
  if (!valid) throw StageError(ErrorCode::kInvalidArtifact, message);
}
std::string Hex(Sha256& digest) {
  const char* digits="0123456789abcdef";std::string result;
  for (auto byte:digest.finish()) { result+=digits[byte>>4];result+=digits[byte&15]; }
  return result;
}
void Digest(const std::string& value) {
  Check(value.size()==64 && std::all_of(value.begin(),value.end(),[](char c) {
    return (c>='0' && c<='9') || (c>='a' && c<='f');
  }), "invalid semantic digest");
}
std::string String(SqliteDatabase& db, const std::string& json, const std::string& key) {
  auto query=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");
  query.BindText(1,json);query.BindText(2,key);
  Check(query.Step() && !query.IsNull(0) && query.TypedText(0)=="text", "missing semantic text fact");
  return query.TypedText(1);
}
std::uint64_t Integer(SqliteDatabase& db, const std::string& json, const std::string& key) {
  auto query=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");
  query.BindText(1,json);query.BindText(2,key);
  Check(query.Step() && !query.IsNull(0) && query.TypedText(0)=="integer" && query.TypedInteger(1)>=0,
        "missing or invalid semantic integer fact");
  return static_cast<std::uint64_t>(query.Integer(1));
}
struct Section { std::uint64_t offset, bytes, records;std::string sha256; };
class SectionReader {
 public:
  SectionReader(const std::string& path,const Section& section)
      : input_(path),remaining_(section.bytes),expected_(section.sha256) { input_.Seek(section.offset); }
  template<class T> T Read() { T result{};Bytes(&result,sizeof result);return result; }
  std::string Text(bool allow_nul=false) {
    const auto bytes=Read<std::uint32_t>();
    Check(bytes && bytes<=remaining_,"semantic net string is truncated");
    std::string result(bytes,'\0');Bytes(result.data(),bytes);
    Check(allow_nul || result.find('\0')==std::string::npos,"semantic net contains NUL");
    JsonQuote(result); // Common strict UTF-8 validation, preserving the spelling.
    return result;
  }
  void Finish() { Check(!remaining_ && Hex(hash_)==expected_,"semantic section checksum or extent differs"); }
  std::vector<std::uint8_t> CaptureBit() {
    const auto bytes=Read<std::uint32_t>();
    Check(bytes && bytes<=1024 && bytes<=remaining_,"semantic capture bit extent is invalid");
    std::vector<std::uint8_t> result(bytes);Bytes(result.data(),bytes);
    Check(bytes==1 || result.front()!=0,"semantic capture bit is not canonical");
    return result;
  }
 private:
  void Bytes(void* output,std::uint64_t bytes) {
    Check(bytes<=remaining_,"semantic section is truncated");
    input_.ReadBytes(output,bytes);hash_.update(static_cast<unsigned char*>(output),bytes);remaining_-=bytes;
  }
  BinaryReader input_;
  std::uint64_t remaining_;
  std::string expected_;
  Sha256 hash_;
};
}

ExactSemanticView::ExactSemanticView(const ArtifactRef& assignment,const ArtifactRef& semantic)
    : assignment_(assignment),semantic_(semantic) {
  Check(assignment.schema()=="assignment-index-v4" && semantic.schema()=="exact-semantic-binary-v2",
        "native exact semantic view requires current typed owners");
  RequireUnchanged();
  SqliteStore owner(assignment);auto& db=owner.database();
  auto metadata=db.Prepare("SELECT value FROM metadata WHERE key='semantic_contract_binary'");
  Check(metadata.Step(),"assignment lacks its semantic descriptor");
  const auto descriptor=metadata.TypedText(0);
  Check(String(db,descriptor,"$.schema")=="emuflow.phase3-static-exact-binary/v2" &&
        String(db,descriptor,"$.artifact")=="assignment.exact.bin" &&
        Integer(db,descriptor,"$.bytes")==semantic.generation().bytes,
        "assignment semantic sidecar descriptor differs");
  Check(std::filesystem::equivalent(std::filesystem::path(assignment.path()).parent_path()/"assignment.exact.bin",semantic.path()),
        "semantic sidecar belongs to another assignment location");
  BinaryReader file(semantic.path());
  ReadAndValidateHeader(file,{0x5443415845554d45ULL,2,"semantic sidecar"});
  const auto manifest_offset=file.Read<std::uint64_t>(),length=file.Read<std::uint64_t>();
  Check(manifest_offset>=28 && manifest_offset<=file.size() && length && length<=1024*1024 &&
        length==file.size()-manifest_offset,"semantic manifest extent is invalid");
  file.Seek(manifest_offset);std::string manifest(length,'\0');file.ReadBytes(manifest.data(),length);file.RequireEnd();
  Sha256 manifest_hash;manifest_hash.update(reinterpret_cast<const unsigned char*>(manifest.data()),manifest.size());
  Check(Hex(manifest_hash)==String(db,descriptor,"$.manifest_sha256"),"semantic manifest identity differs");
  Check(String(db,manifest,"$.schema")=="emuflow.phase3-static-exact-binary/v2" &&
        String(db,manifest,"$.semantic_contract_schema")==String(db,descriptor,"$.semantic_contract_schema") &&
        Integer(db,manifest,"$.capture_bit_max_bits")==8192 && Integer(db,descriptor,"$.capture_bit_max_bits")==8192,
        "semantic manifest schema differs");
  semantic_sha256_=String(db,manifest,"$.semantic_contract_sha256");Digest(semantic_sha256_);
  auto seal=db.Prepare("SELECT json_extract(value,'$') FROM metadata WHERE key='semantic_contract_sha256'");
  Check(seal.Step() && seal.TypedText(0)==semantic_sha256_ &&
        semantic_sha256_==String(db,descriptor,"$.semantic_contract_sha256"),"semantic assignment seal differs");
  std::map<std::string,std::uint64_t> counts;
  for (const auto* key: {"cut_nodes","logic_segments","source_segments","consumer_segments","capture_requirements"}) {
    const auto path=std::string("$.counts.")+key;
    const auto count=Integer(db,manifest,path);
    Check(count<UINT32_MAX && count==Integer(db,descriptor,path),"semantic count differs");counts.emplace(key,count);
  }
  auto parts=db.Prepare("SELECT part_index,fpga FROM parts ORDER BY part_index");
  while(parts.Step()) {
    Check(parts_.size()<255 && parts.TypedInteger(0)==static_cast<std::int64_t>(parts_.size()),"semantic part order is invalid");
    const auto name=parts.TypedText(1);
    Check(name==String(db,manifest,"$.parts["+std::to_string(parts_.size())+"]") &&
          (parts_.empty() || name>parts_.back()),"semantic part identity differs");parts_.push_back(name);
  }
  auto part_count=db.Prepare("SELECT json_type(?1,'$.parts'),json_array_length(?1,'$.parts')");part_count.BindText(1,manifest);
  Check(!parts_.empty() && part_count.Step() && part_count.TypedText(0)=="array" &&
        part_count.TypedInteger(1)==static_cast<std::int64_t>(parts_.size()),"semantic part coverage differs");
  std::map<std::string,Section> sections;
  auto rows=db.Prepare("SELECT key,value,type FROM json_each(?1,'$.sections')");rows.BindText(1,manifest);
  std::vector<std::pair<std::uint64_t,std::uint64_t>> occupied;
  std::set<std::string> expected{"nets","node_sources"};
  for (std::size_t part=0;part<parts_.size();++part) {
    expected.insert("segments:"+std::to_string(part));expected.insert("captures:"+std::to_string(part));
  }
  while(rows.Step()) {
    const auto name=rows.TypedText(0),value=rows.TypedText(1);
    Check(rows.TypedText(2)=="object" && expected.erase(name)==1,"semantic section set differs");
    Section section{Integer(db,value,"$.offset"),Integer(db,value,"$.bytes"),Integer(db,value,"$.records"),String(db,value,"$.sha256")};
    Digest(section.sha256);
    Check(section.offset>=28 && section.offset<=manifest_offset && section.bytes<=manifest_offset-section.offset,
          "semantic section extent is invalid");
    const auto minimum=name=="nets"?5ULL:name=="node_sources"?4ULL:name.rfind("segments:",0)==0?14ULL:14ULL;
    Check(section.records<=section.bytes/minimum,"semantic section count exceeds its extent");
    if (section.bytes) occupied.emplace_back(section.offset,section.offset+section.bytes);
    sections.emplace(name,std::move(section));
  }
  Check(expected.empty(),"semantic sections are missing");std::sort(occupied.begin(),occupied.end());
  for (std::size_t i=1;i<occupied.size();++i) Check(occupied[i-1].second<=occupied[i].first,"semantic sections overlap");
  const auto cuts=counts.at("cut_nodes"),segment_count=counts.at("logic_segments");
  Check(sections.at("nets").records==cuts && sections.at("node_sources").records==cuts,
        "semantic cut section coverage differs");
  auto cut_metadata=db.Prepare("SELECT json_extract(value,'$') FROM metadata WHERE key='cut_records'");
  Check(cut_metadata.Step() && cut_metadata.TypedInteger(0)==static_cast<std::int64_t>(cuts),"semantic cut count differs from assignment");
  source_parts_.reserve(cuts);
  SectionReader nets(semantic.path(),sections.at("nets"));
  auto cut_rows=db.Prepare("SELECT cut_index,net_id,source_part_index FROM cuts ORDER BY cut_index");
  for (std::uint64_t i=0;i<cuts;++i) {
    const auto name=nets.Text();
    Check(cut_rows.Step() && cut_rows.TypedInteger(0)==static_cast<std::int64_t>(i) && cut_rows.TypedText(1)==name &&
          cut_rows.TypedInteger(2)>=0 && static_cast<std::uint64_t>(cut_rows.Integer(2))<parts_.size(),
          "semantic cut identity differs from assignment");
    source_parts_.push_back(static_cast<std::uint32_t>(cut_rows.Integer(2)));
  }
  Check(!cut_rows.Step(),"assignment contains extra semantic cuts");nets.Finish();
  Check(segment_count<=semantic.generation().bytes/14,"semantic segment allocation exceeds source extent");
  segments_.resize(segment_count);std::vector<bool> seen(segment_count,false);
  capture_segments_.assign(counts.at("capture_requirements"),UINT32_MAX);
  std::uint64_t observed=0,capture_segments=0,launches=0,captures=0;
  for (std::size_t part=0;part<parts_.size();++part) {
    const auto& section=sections.at("segments:"+std::to_string(part));
    Check(section.records<=segment_count-observed && section.bytes==section.records*14,"semantic segment section extent differs");
    SectionReader input(semantic.path(),section);
    for (std::uint64_t row=0;row<section.records;++row) {
      const auto id=input.Read<std::uint32_t>();const auto kind=input.Read<std::uint8_t>(),flags=input.Read<std::uint8_t>();
      const auto from=input.Read<std::uint32_t>(),to=input.Read<std::uint32_t>();
      Check(id<segment_count && !seen[id] && kind<=2 && flags<=1 && (!flags || kind==0),"semantic segment identity is invalid");
      Check((kind==0 && from==UINT32_MAX && to<cuts) || (kind==1 && from<cuts && to<cuts && from!=to) ||
            (kind==2 && from<cuts && to<counts.at("capture_requirements")),"semantic segment coordinates are invalid");
      Check(kind==2 || source_parts_[to]==part,"semantic segment source FPGA differs");
      seen[id]=true;segments_[id]={from,to,static_cast<std::uint32_t>(part),kind,flags!=0};
      if(kind==2) {
        Check(capture_segments_[to]==UINT32_MAX,"semantic capture has more than one segment");
        capture_segments_[to]=id;
      }
      ++observed;if(kind==2)++capture_segments;if(kind==0)++launches;
    }
    input.Finish();const auto& capture_section=sections.at("captures:"+std::to_string(part));
    captures+=capture_section.records;
    capture_sections_.push_back({capture_section.offset,capture_section.bytes,capture_section.records,capture_section.sha256});
  }
  Check(observed==segment_count && capture_segments==counts.at("capture_requirements") && captures==capture_segments &&
        observed-capture_segments==counts.at("source_segments") && observed-launches==counts.at("consumer_segments"),
        "semantic segment population differs");
  SectionReader source_ids(semantic.path(),sections.at("node_sources"));offsets_.push_back(0);
  std::fill(seen.begin(),seen.end(),false);source_ids_.reserve(counts.at("source_segments"));
  for (std::uint64_t cut=0;cut<cuts;++cut) {
    const auto n=source_ids.Read<std::uint32_t>();
    Check(n && n<=counts.at("source_segments")-source_ids_.size(),"semantic source fanin count differs");
    bool stable=false;
    for (std::uint32_t row=0;row<n;++row) {
      const auto id=source_ids.Read<std::uint32_t>();
      Check(id<segments_.size() && !seen[id] && segments_[id].kind!=2 && segments_[id].target==cut &&
            (!row || id>source_ids_.back()),"semantic source fanin identity differs");
      stable=stable || segments_[id].configuration_stable;seen[id]=true;source_ids_.push_back(id);
    }
    Check(!stable || n==1,"configuration-stable cut has dynamic fanins");offsets_.push_back(source_ids_.size());
  }
  source_ids.Finish();Check(source_ids_.size()==counts.at("source_segments"),"semantic fanin coverage is incomplete");
  RequireUnchanged();
}

std::uint32_t ExactSemanticView::SourcePart(std::uint32_t cut) const {
  Check(cut<source_parts_.size(),"semantic cut coordinate is unknown");return source_parts_[cut];
}
ExactSourceRange ExactSemanticView::Sources(std::uint32_t cut) const {
  Check(cut<source_parts_.size(),"semantic cut coordinate is unknown");
  return {source_ids_.data()+offsets_[cut],source_ids_.data()+offsets_[cut+1]};
}
void ExactSemanticView::VisitCaptures(const std::function<void(const ExactCaptureEndpoint&)>& visitor) const {
  RequireUnchanged();std::uint64_t observed=0;
  for(std::uint32_t part=0;part<capture_sections_.size();++part) {
    const auto& section=capture_sections_[part];
    SectionReader input(semantic_.path(),{section.offset,section.bytes,section.records,section.sha256});
    std::uint32_t previous=0;
    for(std::uint64_t row=0;row<section.records;++row) {
      const auto id=input.Read<std::uint32_t>(),cut=input.Read<std::uint32_t>();
      const auto kind=input.Read<std::uint8_t>();
      Check(id<capture_segments_.size() && cut<cut_count() && kind<=1 && (!row || id>previous),
            "semantic capture identity or order is invalid");
      const auto segment=capture_segments_[id];
      Check(segment<segments_.size() && segments_[segment].kind==2 && segments_[segment].target==id &&
            segments_[segment].source==cut && segments_[segment].part==part,
            "semantic capture endpoint differs from its segment");
      ExactCaptureEndpoint capture{id,segment,cut,part,kind,input.Text(true),{}, {}};
      if(kind) {capture.port=input.Text(true);capture.bit=input.CaptureBit();}
      visitor(capture);previous=id;++observed;
    }
    input.Finish();
  }
  Check(observed==capture_segments_.size(),"semantic capture endpoint coverage is incomplete");
  RequireUnchanged();
}
void ExactSemanticView::RequireUnchanged() const { assignment_.RequireUnchanged();semantic_.RequireUnchanged(); }
}  // namespace emuflow::core
