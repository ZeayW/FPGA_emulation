#include "partition/partition_publish.hpp"
#include "partition/assignment_publish.hpp"
#include "partition/cluster_catalog.hpp"
#include "partition/phase3_exact_binary.hpp"
#include "partition/primary_cluster_view.hpp"
#include "common/artifact_io.hpp"
#include "common/hash.hpp"
#include "common/json_record.hpp"
#include "storage/store.hpp"
#include <algorithm>
#include <filesystem>
#include <fstream>
#include <set>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs=std::filesystem;using U=std::uint64_t;
void Check(bool value,const std::string& text){if(!value)throw StageError(ErrorCode::kInvalidArtifact,"partition publication "+text);}
class Request {
 public:
  explicit Request(const fs::path& path):writer(path.string()){}
  template<typename T>void Put(T value){writer.Write(value);UpdateCrc32(crc,reinterpret_cast<const unsigned char*>(&value),sizeof value);}
  void Bytes(const void* data,std::size_t bytes){writer.WriteBytes(data,bytes);UpdateCrc32(crc,static_cast<const unsigned char*>(data),bytes);}
  void Text(const std::string& text){Check(text.size()<UINT32_MAX,"request string exceeds bound");Put(std::uint32_t(text.size()));Bytes(text.data(),text.size());}
  void Generation(const ArtifactRef& source){const auto& g=source.generation();Put(g.device);Put(g.inode);Put(g.bytes);Put(g.modified_seconds);Put(g.modified_nanoseconds);Put(g.changed_seconds);Put(g.changed_nanoseconds);}
  void Catalog(const std::vector<std::string>& values){Put(std::uint32_t(values.size()));for(const auto& value:values)Text(value);}
  void Finish(){writer.Write(crc);writer.Commit();}
  AtomicBinaryWriter writer;std::uint32_t crc=0;
};
struct Workspace {
  fs::path root;
  explicit Workspace(const fs::path& parent){auto path=(parent/".partition-publish-XXXXXX").string();Check(mkdtemp(path.data())!=nullptr,"cannot create workspace");root=path;}
  ~Workspace(){std::error_code error;fs::remove_all(root,error);}
};
std::vector<std::string> Catalog(CheckedRequestReader& r,bool ordered) {
  const auto count=r.Read<std::uint32_t>();Check(count && count<=255,"part catalog size is invalid");std::vector<std::string> values;std::set<std::string> unique;
  for(unsigned i=0;i<count;++i){auto value=r.ReadString32();JsonQuote(value);Check(value.find('\0')==std::string::npos && unique.insert(value).second &&
      (!ordered || values.empty() || values.back()<value),"part catalog is invalid");values.push_back(std::move(value));}return values;
}
std::string ReadSmall(const fs::path& path){Check(fs::file_size(path)<=8*1024*1024,"control document exceeds bound");std::ifstream in(path,std::ios::binary);
  std::string text(fs::file_size(path),'\0');in.read(text.data(),text.size());Check(bool(in),"cannot read control document");return text;}
std::string FileHash(const fs::path& path){std::ifstream in(path,std::ios::binary);Check(bool(in),"cannot seal cluster store");Sha256 hash;std::array<char,65536> bytes{};
  while(in){in.read(bytes.data(),bytes.size());hash.update(reinterpret_cast<const unsigned char*>(bytes.data()),in.gcount());}Check(in.eof(),"cannot read cluster store");
  std::string text;const char* digits="0123456789abcdef";for(auto value:hash.finish()){text+=digits[value>>4];text+=digits[value&15];}return text;}
std::string Object(const std::map<std::string,std::string>& fields){std::string text="{";for(const auto& field:fields){if(text.size()>1)text+=',';text+=JsonQuote(field.first)+':'+field.second;}return text+'}';}
void Write(const fs::path& path,const std::string& text,const std::vector<ArtifactRef>& inputs){PublishTextArtifact(path.string(),inputs,[&](std::ostream& out){out<<text<<'\n';});}
}
StageResult PublishPartition(const std::vector<ArtifactRef>& inputs,const std::string& directory) {
  return RunStage("phase3.publish-partition",[&]{for(const auto& input:inputs)input.RequireUnchanged();return inputs;},[&](const auto& refs) {
    Check(refs.size()>=8 && refs.size()<=10,"source coverage disagrees");CheckedRequestReader request(refs.back().path(),8*1024*1024);
    Check(request.Read<U>()==0x31555050464d5545ULL && request.Read<std::uint32_t>()==1,"request is invalid");
    const auto kind=request.Read<std::uint32_t>();Check(kind<=2 && refs.size()==8+kind,"owner coverage disagrees");
    const char* schemas[]={"emuir-compact-v2","partition-cluster-columns-v2","exact-nodes-v2","exact-captures-v1","partition-cut-counts-v1","cluster-catalog-request-v1","candidate-id-columns-v1"};
    for(unsigned i=0;i<7;++i)Check(refs[i].schema()==schemas[i],"source schema disagrees");
    Check(refs.back().schema()=="partition-publication-request-v1","request schema disagrees");
    if(kind)Check(refs[7].schema()==(kind==1?"patron-initial-columns-v1":"patron-output-v1"),"owner schema disagrees");
    if(kind==2)Check(refs[8].schema()=="patron-cluster-order-v1","coordinate schema disagrees");
    for(unsigned i=0;i<refs.size()-1;++i)if(!(ReadFileGeneration(request)==refs[i].generation()))
      throw StageError(ErrorCode::kMixedGeneration,"partition publication original source generation changed");
    const auto parts=Catalog(request,true),owner_parts=Catalog(request,false);const auto header=request.ReadString32(false,8*1024*1024);request.RequireEnd();
    SqliteDatabase json(":memory:",SqliteDatabase::Mode::kReadOnly);
    auto valid=json.Prepare("SELECT json_valid(?1),json_type(?1)");valid.BindText(1,header);Check(valid.Step() && valid.Integer(0)==1 && valid.Text(1)=="object","header is invalid");
    auto duplicate=json.Prepare("SELECT 1 FROM json_tree(?1) WHERE parent IS NOT NULL GROUP BY parent,key HAVING count(*)>1 LIMIT 1");duplicate.BindText(1,header);Check(!duplicate.Step(),"header repeats a JSON key");
    auto keys=json.Prepare("SELECT key FROM json_tree(?1) WHERE typeof(key)='text'");keys.BindText(1,header);
    while(keys.Step())Check(keys.TypedText(0).find('\0')==std::string::npos,"header member name contains NUL");
    const auto raw=[&](const std::string& name){return JsonMemberToken(json,header,name);};
    const auto metadata=raw("index_metadata");
    const auto member=[&](const std::string& name){return JsonMemberToken(json,metadata,name);};
    const auto report_header=raw("report"),document=raw("document");
    for(const auto* field:{"design","platform","provider","seed"}) {
      RequireEquivalentJson(json,JsonMemberToken(json,report_header,field),member(field),"partition report identity disagrees");
      RequireEquivalentJson(json,JsonMemberToken(json,document,field),member(field),"partition document identity disagrees");
    }
    {SqliteStore source(refs[0]);auto design=source.database().Prepare("SELECT value FROM metadata WHERE key='design_json'");
      Check(design.Step() && DecodeJsonString(JsonMemberToken(json,design.TypedText(0),"name"))==DecodeJsonString(member("design")),"source design disagrees");}
    const auto semantic_header=member("semantic_contract_header");const auto semantic=DecodeJsonString(member("semantic_contract_sha256"));
    const auto destination=fs::absolute(directory);Check(!fs::exists(destination) && !fs::is_symlink(destination),"final directory must be absent");
    fs::create_directories(destination.parent_path());Workspace workspace(destination.parent_path());const auto selected=workspace.root/"selected";fs::create_directory(selected);
    const auto exact_request=workspace.root/"exact-request.bin",exact_directory=workspace.root/"exact";
    {Request out(exact_request);out.Put(U(0x31504245554d5545ULL));out.Put(std::uint32_t(1));out.Put(std::uint32_t(2));out.Generation(refs[2]);out.Generation(refs[3]);
      out.Catalog(parts);out.Text(semantic_header);out.Text(semantic);out.Finish();}
    const auto exact=PublishExactBinary({refs[2],refs[3],ArtifactRef::Bind(exact_request.string(),"exact-binary-publication-request-v1")},exact_directory.string());
    Check(exact.ok(),"exact binary transaction failed: "+exact.message);
    const auto exact_descriptor=ReadSmall(exact_directory/"descriptor.json");fs::rename(exact_directory/"assignment.exact.bin",selected/"assignment.exact.bin");
    PrimaryClusterView primary(refs[1],refs[0]);const auto index_request=workspace.root/"index-request.bin";
    {Request out(index_request);out.Put(U(0x31514241554d5545ULL));out.Put(std::uint32_t(3));
      for(unsigned i:{0U,1U,2U,4U})out.Generation(refs[i]);for(unsigned i=7;i<refs.size()-1;++i)out.Generation(refs[i]);
      out.Catalog(parts);out.Catalog(owner_parts);out.Put(U(primary.clusters()));out.Put(U(primary.cells()));
      const std::set<std::string> names{"schema","design","platform","provider","seed","source_instance_count","constraints","semantic_contract_sha256","semantic_contract_schema","semantic_contract_header","semantic_contract_binary"};
      out.Put(std::uint32_t(names.size()));for(const auto& name:names){out.Text(name);out.Text(name=="semantic_contract_binary"?exact_descriptor:member(name));}
      auto counts=json.Prepare("SELECT json_extract(?1,'$.counts.cut_nodes')");counts.BindText(1,exact_descriptor);Check(counts.Step(),"missing exact count");out.Put(U(counts.TypedInteger(0)));out.Finish();}
    std::vector<ArtifactRef> index_sources{refs[0],refs[1],ArtifactRef::Bind(index_request.string(),"assignment-publication-request-v3"),refs[2],refs[4]};
    index_sources.insert(index_sources.end(),refs.begin()+7,refs.end()-1);PublishAssignmentIndex(index_sources,(selected/"assignment.sqlite").string());
    const auto index=ArtifactRef::Bind((selected/"assignment.sqlite").string(),"assignment-index-v4");AssignmentCutSummary cuts;
    {AssignmentView validation(index);cuts=validation.ValidateCuts();}
    const auto cut_index=JsonRecord(std::map<std::string,JsonRecord>{{"artifact","assignment.sqlite"},{"schema","emuflow.phase3-cut-records/v1"},{"records",cuts.records},{"sha256",cuts.sha256}}).Encode();
    const auto assignment_header=Object({{"document",document},{"partitions",raw("partitions")},
      {"cut_nets_index",cut_index},{"semantic_contract_index",JsonRecord(std::map<std::string,JsonRecord>{{"artifact","assignment.sqlite"},
        {"schema","emuflow.static-exact-combinational-cut/v3"},{"sha256",semantic}}).Encode()}});
    const auto descriptor_request=workspace.root/"descriptor-request.bin";
    {Request out(descriptor_request);out.Put(U(0x31514441554d5545ULL));out.Put(std::uint32_t(1));out.Generation(refs[0]);out.Generation(refs[1]);out.Generation(index);out.Text(assignment_header);out.Finish();}
    PublishAssignmentDescriptor({refs[0],refs[1],index,ArtifactRef::Bind(descriptor_request.string(),"assignment-descriptor-request-v1")},(selected/"assignment.json").string());
    const auto cluster_file=selected/"clusters.sqlite";PublishClusterCatalog({refs[0],refs[1],refs[5],refs[6]},cluster_file.string());
    const auto cluster_hash=FileHash(cluster_file),cluster_name="clusters."+cluster_hash+".sqlite";fs::rename(cluster_file,selected/cluster_name);
    std::string cluster_counts;{SqliteStore catalog(ArtifactRef::Bind((selected/cluster_name).string(),"phase3-cluster-catalog-v3"));auto row=catalog.database().Prepare("SELECT value FROM metadata WHERE key='counts'");Check(row.Step(),"cluster counts are missing");cluster_counts=row.TypedText(0);}
    const auto selector=Object({{"schema",JsonQuote("emuflow.phase3-clusters-storage/v3")},{"logical_schema",JsonQuote("emuflow.clusters/v1")},
      {"format",JsonQuote("sqlite-phase3-clusters/v1")},{"application_id",std::to_string(0x454d5543)},{"user_version","1"},
      {"artifact",JsonQuote(cluster_name)},{"store_bytes",std::to_string(fs::file_size(selected/cluster_name))},{"store_sha256",JsonQuote(cluster_hash)},{"counts",cluster_counts}});
    Write(selected/"clusters.json",selector,refs);Write(selected/"constraints.normalized.json",member("constraints"),refs);
    auto report=json.Prepare("SELECT json_set(?1,'$.artifacts.clusters_data',?2,'$.finalization_implementation_id','native-partition-publication-v1',"
        "'$.assignment_index_writer_implementation_id','native-primary-assignment-index-v1','$.assignment_descriptor_writer_implementation_id','native-indexed-assignment-descriptor-v1',"
        "'$.native_assignment_index_metrics',json('{\"host_cut_records\":0,\"host_ownership_records\":0}'))");
    report.BindText(1,raw("report"));report.BindText(2,cluster_name);Check(report.Step(),"cannot assemble final report");
    Write(selected/"phase3_report.json",report.TypedText(0),refs);
    const auto ready=destination.parent_path()/(workspace.root.filename().string()+"-publish");fs::rename(selected,ready);
    try{PublishNewDirectory(ready.string(),destination.string(),refs);}catch(...){std::error_code error;fs::remove_all(ready,error);throw;}
  },[&]{return std::vector<ArtifactRef>{ArtifactRef::Bind((fs::path(directory)/"phase3_report.json").string(),"phase3-report-v1"),
    ArtifactRef::Bind((fs::path(directory)/"assignment.sqlite").string(),"assignment-index-v4"),
    ArtifactRef::Bind((fs::path(directory)/"assignment.exact.bin").string(),"phase3-exact-data-v2"),
    ArtifactRef::Bind((fs::path(directory)/"assignment.json").string(),"assignment-selector-v4")};});
}
}
