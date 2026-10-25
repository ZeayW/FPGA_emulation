#include "emuflow_core/atomic_ownership.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/store.hpp"
#include <map>

namespace emuflow::core {
namespace {
void Check(bool condition,const std::string& message) {
  if (!condition) throw StageError(ErrorCode::kInvalidArtifact,message);
}
FileGeneration ReadGeneration(CheckedRequestReader& request) {
  FileGeneration g;
  g.device=request.Read<std::uint64_t>();g.inode=request.Read<std::uint64_t>();g.bytes=request.Read<std::uint64_t>();
  g.modified_seconds=request.Read<std::int64_t>();g.modified_nanoseconds=request.Read<std::int64_t>();
  g.changed_seconds=request.Read<std::int64_t>();g.changed_nanoseconds=request.Read<std::int64_t>();
  return g;
}
std::string Metadata(SqliteDatabase& db,const std::string& key) {
  auto row=db.Prepare("SELECT value FROM metadata WHERE key=?");row.BindText(1,key);
  Check(row.Step(),"atomic ownership metadata is missing: "+key);
  auto value=row.TypedText(0);Check(!row.Step(),"atomic ownership metadata repeats: "+key);return value;
}
std::string Text(SqliteDatabase& db,const std::string& value,const std::string& path) {
  auto row=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");row.BindText(1,value);row.BindText(2,path);
  Check(row.Step() && row.Text(0)=="text","atomic ownership JSON identity is invalid: "+path);
  return row.TypedText(1);
}
std::int64_t Number(SqliteDatabase& db,const std::string& value,const std::string& path) {
  auto row=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");row.BindText(1,value);row.BindText(2,path);
  Check(row.Step() && row.Text(0)=="integer","atomic ownership count is invalid: "+path);
  auto n=row.TypedInteger(1);Check(n>=0,"atomic ownership count is negative");return n;
}
std::int64_t Count(SqliteDatabase& db,const char* query) {
  auto row=db.Prepare(query);Check(row.Step(),"atomic ownership count is absent");return row.TypedInteger(0);
}
void Digest(const std::string& value) {
  Check(value.size()==64 && value.find_first_not_of("0123456789abcdef")==std::string::npos,
        "atomic ownership source digest is invalid");
}
std::string Singleton(std::uint64_t rank) {
  auto digits=std::to_string(rank);
  return "atomic-source-"+std::string(digits.size()<6?6-digits.size():0,'0')+digits;
}
}  // namespace
void ValidateAtomicOwnership(const std::vector<ArtifactRef>& inputs,const std::string& summary) {
  Check(inputs.size()==4,"atomic ownership input coverage is invalid");
  for(const auto& input:inputs) input.RequireUnchanged();
  CheckedRequestReader request(inputs[3].path(),8192);
  Check(request.Read<std::uint64_t>()==0x314f5341464d5545ULL && request.Read<std::uint32_t>()==1,
        "unsupported atomic ownership request");
  for(unsigned i=0;i<3;++i) if(!(ReadGeneration(request)==inputs[i].generation()))
    throw StageError(ErrorCode::kMixedGeneration,"atomic ownership source generation changed");
  const auto mapped_sha=request.ReadString32(false,64),source_sha=request.ReadString32(false,64),certificate_sha=request.ReadString32(false,64),top=request.ReadString32(false,1024);
  Digest(mapped_sha);Digest(source_sha);Digest(certificate_sha);request.RequireEnd();
  SqliteStore mapped(inputs[0]),source(inputs[1]),certificate(inputs[2]);
  auto& m=mapped.database();auto& s=source.database();auto& c=certificate.database();
  for(auto* db:{&m,&s,&c}) db->Execute("PRAGMA cache_size=-16384; PRAGMA temp_store=FILE");
  const auto header=Metadata(s,"header"),counts=Metadata(s,"counts"),binding=Metadata(s,"mapped");
  Check(Text(m,Metadata(m,"top_json"),"$")==top && Text(s,header,"$.top")==top &&
        Text(s,header,"$.status")=="pass" && Text(s,header,"$.source.mapped_sha256")==mapped_sha &&
        Text(s,binding,"$.sha256")==mapped_sha && Number(s,binding,"$.bytes")==static_cast<std::int64_t>(inputs[0].generation().bytes),
        "atomic ownership mapped source identity disagrees");
  Check(Text(s,Metadata(s,"logical_sha256"),"$")==source_sha && Metadata(c,"logical_sha256")==certificate_sha,
        "atomic ownership logical source identity disagrees");
  const auto physical_expected=Number(s,counts,"$.physical_cells"),constant_expected=Number(s,counts,"$.constant_cells"),macro_expected=Number(s,counts,"$.macro_assignments"),singleton_expected=Number(s,counts,"$.singleton_clusters");
  Check(Count(s,"SELECT count(*) FROM clusters")==Number(s,counts,"$.macro_clusters") &&
        Count(s,"SELECT count(*) FROM cascades")==Number(s,counts,"$.cascade_chains"),
        "atomic ownership source relation coverage disagrees");
  {
    auto empty=s.Prepare("SELECT 1 FROM clusters c LEFT JOIN assignments a ON a.cluster_seq=c.seq WHERE a.instance IS NULL LIMIT 1");
    Check(!empty.Step(),"OpenPARF atomic source macro has no assignments");
  }
  auto mapped_rows=m.Prepare("SELECT id,type FROM cells ORDER BY id");
  auto macro_rows=s.Prepare("SELECT a.instance,c.cluster_id,a.cell_type FROM assignments a LEFT JOIN clusters c ON c.seq=a.cluster_seq ORDER BY a.instance");
  auto constant_rows=s.Prepare("SELECT instance FROM constants ORDER BY instance");
  auto placed_rows=c.Prepare("SELECT instance,cell_type,source_cluster FROM assignments ORDER BY instance");
  bool macro=macro_rows.Step(),constant=constant_rows.Step(),placed=placed_rows.Step();
  std::int64_t rank=0,physical=0,constants=0,macros=0,singletons=0;
  std::string previous;
  while(mapped_rows.Step()) {
    const auto name=mapped_rows.TypedText(0),type=mapped_rows.TypedText(1);
    Check(!name.empty() && !type.empty() && (rank==0 || previous<name),"atomic ownership mapped identity repeats or is invalid");
    previous=name;
    Check(!macro || macro_rows.TypedText(0)>=name,"OpenPARF atomic source macro cell is absent from mapped input");
    Check(!constant || constant_rows.TypedText(0)>=name,"OpenPARF atomic source constant is absent from mapped input");
    Check(!placed || placed_rows.TypedText(0)>=name,"OpenPARF certificate has an extra or repeated cell");
    if(type=="GND" || type=="VCC") {
      Check(!macro || macro_rows.TypedText(0)!=name,"OpenPARF atomic source macro owns a constant");
      Check(constant && constant_rows.TypedText(0)==name,"OpenPARF atomic source constant coverage is incomplete");
      Check(!placed || placed_rows.TypedText(0)!=name,"OpenPARF certificate places a constant");
      constant=constant_rows.Step();++constants;
    } else {
      Check(!constant || constant_rows.TypedText(0)!=name,"OpenPARF atomic source constant type is invalid");
      std::string owner;
      if(macro && macro_rows.TypedText(0)==name) {
        owner=macro_rows.TypedText(1);
        Check(!owner.empty() && owner.rfind("atomic-source-",0)!=0 && macro_rows.TypedText(2)==type,
              "OpenPARF atomic source macro ownership or type is invalid");
        macro=macro_rows.Step();++macros;
      } else {owner=Singleton(rank);++singletons;}
      Check(placed && placed_rows.TypedText(0)==name,"OpenPARF atomic placement coverage is incomplete");
      Check(placed_rows.TypedText(1)==type,"OpenPARF certificate mapped cell identity is invalid");
      Check(placed_rows.TypedText(2)==owner,"OpenPARF atomic source cluster identity is invalid");
      placed=placed_rows.Step();++physical;
    }
    ++rank;
  }
  Check(!macro && !constant && !placed,"OpenPARF atomic source or certificate ownership has unmatched cells");
  Check(physical==physical_expected && constants==constant_expected && macros==macro_expected && singletons==singleton_expected &&
        physical==macros+singletons && Count(c,"SELECT count(*) FROM assignments")==physical,
        "OpenPARF atomic source assignment coverage disagrees");
  const auto result=JsonRecord(std::map<std::string,JsonRecord>{
      {"schema","emuflow.atomic-ownership-validation/v1"},{"status","pass"},
      {"implementation_id","native-atomic-ownership-merge-v1"},{"cells",rank},
      {"assignments",physical},{"constants",constants},{"macro_assignments",macros},{"singleton_clusters",singletons},
      {"mapped_sha256",mapped_sha},{"source_logical_sha256",source_sha},{"certificate_logical_sha256",certificate_sha},
      {"ownership_snapshots",0}});
  PublishTextArtifact(summary,inputs,[&](std::ostream& stream){stream<<result.Encode()<<'\n';});
}
}  // namespace emuflow::core
