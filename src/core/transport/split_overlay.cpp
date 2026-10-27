#include "transport/split_overlay.hpp"
#include "ir/emuir_view.hpp"
#include "common/json_record.hpp"
#include "partition/phase3_exact_binary.hpp"
#include "storage/schema/split_overlay_schema.hpp"
#include <algorithm>
#include <array>
#include <charconv>
#include <filesystem>
#include <fstream>
#include <map>
#include <set>

namespace emuflow::core {
namespace {
using Object=std::map<std::string,JsonRecord>;using Array=std::vector<JsonRecord>;
namespace fs=std::filesystem;
void Check(bool valid,const std::string& message){if(!valid)throw StageError(ErrorCode::kInvalidArtifact,"split overlay: "+message);}
std::string Meta(SqliteDatabase& db,const std::string& name,const std::string& alias="main"){
  auto row=db.Prepare("SELECT value FROM "+alias+".metadata WHERE key=?");row.BindText(1,name);Check(row.Step(),"missing metadata "+name);auto value=row.TypedText(0);Check(!row.Step(),"duplicate metadata");return value;
}
std::string Member(SqliteDatabase& db,const std::string& object,const std::string& name){
  bool found=false;VisitJsonObjectTokens(object,[&](const auto& key,const auto&){if(key==name)found=true;});
  return found?JsonMemberToken(db,object,name):"null";
}
std::int64_t Integer(const std::string& text){std::int64_t value=0;const auto r=std::from_chars(text.data(),text.data()+text.size(),value);Check(r.ec==std::errc() && r.ptr==text.data()+text.size(),"invalid integer");return value;}
std::uint64_t Unsigned(const std::string& text){std::uint64_t value=0;const auto r=std::from_chars(text.data(),text.data()+text.size(),value);Check(r.ec==std::errc() && r.ptr==text.data()+text.size(),"invalid unsigned integer");return value;}
std::string Text(SqliteDatabase& db,const std::string& object,const std::string& name){return DecodeJsonString(Member(db,object,name));}
void Attach(SqliteDatabase& db,const ArtifactRef& input,const std::string& alias){auto q=db.Prepare("ATTACH DATABASE ? AS "+alias);q.BindText(1,SqliteImmutableUri(input.path()));q.Execute();}
std::int64_t Scalar(SqliteDatabase& db,const std::string& sql){auto row=db.Prepare(sql);Check(row.Step(),"missing scalar");return row.TypedInteger(0);}
JsonRecord Reference(const ArtifactRef& input){const auto& g=input.generation();return Object{{"path",input.path()},{"schema",input.schema()},{"generation",Array{g.device,g.inode,g.bytes,g.modified_seconds,g.modified_nanoseconds,g.changed_seconds,g.changed_nanoseconds}}};}
void Identifier(const std::string& value,bool empty=false){Check((empty && value.empty()) || (!value.empty() && std::all_of(value.begin(),value.end(),[](unsigned char c){return (c>='a'&&c<='z') || (c>='A'&&c<='Z') || (c>='0'&&c<='9') || c=='_';})),"unsafe SQL namespace");}
std::string Substitute(std::string value,const std::map<std::string,std::string>& fields){for(const auto& field:fields){std::size_t at=0;while((at=value.find(field.first,at))!=std::string::npos){value.replace(at,field.first.size(),field.second);at+=field.second.size();}}return value;}
std::vector<ArtifactRef> Sources(SqliteDatabase& db,const std::string& alias){
  Check(Scalar(db,"SELECT count(*) FROM "+alias+".metadata WHERE NOT json_valid(value)")==0,"invalid control metadata JSON");
  const auto text=Meta(db,"source_bindings",alias);Check(text.size()<=65536,"source catalog exceeds bound");
  const std::map<std::string,std::string> expected{{"source","emuir-compact-v2"},{"assignment","assignment-index-v4"},{"schedule","tdm-schedule-store-v4"},{"exact","phase3-exact-data-v2"}};
  std::map<std::string,ArtifactRef> refs;
  VisitJsonObjectTokens(text,[&](const auto& role,const auto& record){const auto schema=expected.find(role);Check(schema!=expected.end(),"unknown source role");
    std::set<std::string> fields;VisitJsonObjectTokens(record,[&](const auto& name,const auto&){Check(fields.insert(name).second,"duplicate source field");});Check(fields==std::set<std::string>{"path","schema","generation"},"source field coverage disagrees");
    const auto selected_schema=Text(db,record,"schema");Check(selected_schema==schema->second || (role=="exact" && selected_schema=="phase3-exact-data-v1"),"source schema disagrees");const auto path=Text(db,record,"path");Check(fs::path(path).is_absolute(),"source path is not absolute");
    auto ref=ArtifactRef::Bind(path,selected_schema);std::vector<std::string> g;VisitJsonArrayTokens(Member(db,record,"generation"),[&](const auto& token){g.push_back(token);});Check(g.size()==7,"source generation coverage disagrees");
    const FileGeneration generation{Unsigned(g[0]),Unsigned(g[1]),Unsigned(g[2]),Integer(g[3]),Integer(g[4]),Integer(g[5]),Integer(g[6])};
    if(!(generation==ref.generation()))throw StageError(ErrorCode::kMixedGeneration,"split source generation changed: "+role);
    Check(refs.emplace(role,std::move(ref)).second,"duplicate source role");});
  Check(refs.count("source") && refs.count("assignment") && refs.count("schedule") && refs.size()==3+refs.count("exact"),"missing source owner");
  EmuirView source(refs.at("source"));SqliteStore assignment(refs.at("assignment")),schedule(refs.at("schedule"));
  const auto root_design=Meta(db,"design",alias),root_platform=Meta(db,"platform",alias),semantic=Meta(db,"semantic_contract_sha256",alias);
  RequireEquivalentJson(db,root_design,Meta(source.store().database(),"design_json"),"split original design owner disagrees");
  Check(DecodeJsonString(Meta(assignment.database(),"design"))==Text(db,root_design,"name"),"split assignment design disagrees");
  Check(Unsigned(Meta(assignment.database(),"source_instance_count"))==source.counts().cells,"split original source cell namespace disagrees");
  Check(DecodeJsonString(Meta(assignment.database(),"platform"))==DecodeJsonString(root_platform) && Meta(assignment.database(),"semantic_contract_sha256")==semantic,"split assignment namespace disagrees");
  const auto header=Meta(schedule.database(),"header");
  Check(Text(db,header,"design")==Text(db,root_design,"name") && Text(db,header,"platform")==DecodeJsonString(root_platform) && Member(db,header,"semantic_contract_sha256")==semantic,"split schedule namespace disagrees");
  const auto descriptor=Meta(db,"semantic_contract_binary",alias);RequireEquivalentJson(db,descriptor,Meta(assignment.database(),"semantic_contract_binary"),"split original exact descriptor disagrees");
  std::vector<ArtifactRef> result{refs.at("source"),refs.at("assignment"),refs.at("schedule")};
  Check((descriptor!="null")==bool(refs.count("exact")),"split exact owner coverage disagrees");
  if(descriptor!="null"){const auto artifact=Text(db,descriptor,"artifact");Check(fs::path(artifact).filename()==artifact && fs::canonical(fs::path(refs.at("assignment").path()).parent_path()/artifact)==refs.at("exact").path(),"split original exact namespace disagrees");result.push_back(refs.at("exact"));}
  return result;
}
void RequireSources(const std::vector<ArtifactRef>& inputs){for(const auto& input:inputs)input.RequireUnchanged();}
void Install(SqliteDatabase& db,const std::vector<ArtifactRef>& refs,const std::string& alias,const std::string& prefix){
  Identifier(alias);Identifier(prefix,true);const auto stem=prefix.empty()?"_split_owner_":prefix+"owner_";
  const auto source=stem+"source",assignment=stem+"assignment",schedule=stem+"schedule";
  Attach(db,refs[0],source);Attach(db,refs[1],assignment);Attach(db,refs[2],schedule);
  db.Execute(Substitute(kSplitProjectionDDL,{{"{{SPLIT}}",alias},{"{{PREFIX}}",prefix},{"{{SOURCE}}",source},{"{{ASSIGNMENT}}",assignment},{"{{SCHEDULE}}",schedule}}));
  if(refs.size()==3){
    auto legacy=db.Prepare("SELECT count(*) FROM "+assignment+".sqlite_master WHERE type='table' AND name IN ('exact_cut_nodes','exact_cut_sources','exact_logic_segments')");Check(legacy.Step(),"missing inline authority census");
    if(legacy.TypedInteger(0)==3){for(const auto* name:{"exact_cut_nodes","exact_cut_sources","exact_logic_segments"})db.Execute("DROP VIEW temp."+prefix+name+";CREATE TEMP VIEW "+prefix+name+" AS SELECT * FROM "+assignment+"."+name);}
    else Check(Meta(db,"semantic_contract_sha256",alias)=="null","missing exact semantic authority");
  }
}
std::string Summary(SqliteDatabase& db,const std::string& prefix=""){
  // The validation aggregate is private scratch, reused for coverage and counts.
  // No original cell/payload/endpoint population is written into the public owner.
  db.Execute("CREATE TEMP TABLE split_validation_counts AS SELECT part_index,source_net_seq,count(*) AS endpoints,sum(synthetic) AS synthetic,sum(CASE WHEN role=0 AND synthetic=1 THEN 1 ELSE 0 END) AS shadows FROM "+prefix+"endpoint_segments GROUP BY part_index,source_net_seq");
  Object results;
  results["copy_coverage_errors"]=Scalar(db,"SELECT abs((SELECT count(*) FROM "+prefix+"instance_copies)-(SELECT count(*) FROM _split_owner_assignment.copies))");
  results["net_materialization_errors"]=Scalar(db,"SELECT abs((SELECT count(*) FROM "+prefix+"net_segments)-(SELECT count(*) FROM split_validation_counts))");
  results["orphan_endpoint_errors"]=Scalar(db,"SELECT count(*) FROM split_validation_counts e LEFT JOIN "+prefix+"net_segments n ON n.part_index=e.part_index AND n.source_net_seq=e.source_net_seq WHERE n.source_net_seq IS NULL");
  results["shadow_driver_errors"]=Scalar(db,"SELECT count(*) FROM "+prefix+"net_segments n LEFT JOIN split_validation_counts v ON v.part_index=n.part_index AND v.source_net_seq=n.source_net_seq WHERE n.source_kind='transport_shadow' AND coalesce(v.shadows,0)!=1");
  results["unexpected_shadow_errors"]=Scalar(db,"SELECT count(*) FROM split_validation_counts v JOIN "+prefix+"net_segments n ON n.part_index=v.part_index AND n.source_net_seq=v.source_net_seq WHERE v.synthetic>0 AND n.source_kind!='transport_shadow'");
  for(const auto& result:results)Check(result.second.Encode()=="0",result.first+" failed");
  Object summary=results;summary["status"]="pass";summary["provider"]="sqlite-constructive-split-certificate-v2";
  summary["instances"]=Scalar(db,"SELECT count(*) FROM "+prefix+"instance_copies");summary["replica_instances"]=Scalar(db,"SELECT count(*) FROM "+prefix+"instance_copies WHERE is_replica=1");
  summary["net_segments"]=Scalar(db,"SELECT count(*) FROM "+prefix+"net_segments");summary["endpoints"]=Scalar(db,"SELECT coalesce(sum(endpoints),0) FROM split_validation_counts");summary["synthetic_shadow_drivers"]=Scalar(db,"SELECT coalesce(sum(synthetic),0) FROM split_validation_counts");summary["parts"]=Scalar(db,"SELECT count(*) FROM "+prefix+"parts");
  db.Execute("DROP TABLE split_validation_counts");return JsonRecord(summary).Encode();
}
}
std::vector<ArtifactRef> PrepareSplitProjection(SqliteDatabase& database,const ArtifactRef& split,const std::string& alias,const std::string& prefix){
  Check(split.schema()=="split-netlists-v3","projection requires source-bound v3 owner");split.RequireUnchanged();auto refs=Sources(database,alias);Install(database,refs,alias,prefix);RequireSources(refs);split.RequireUnchanged();return refs;
}
std::string ValidateSplitOverlay(const ArtifactRef& split){
  SqliteStore store(split);auto refs=PrepareSplitProjection(store.database(),split);auto result=Summary(store.database());RequireSources(refs);store.RequireUnchanged();return result;
}
StageResult AuditSplitOverlay(const ArtifactRef& split,const std::string& output){
  std::vector<ArtifactRef> inputs{split};
  return RunStage("phase6.validate-split",[&]{SqliteStore store(split);auto refs=Sources(store.database(),"main");inputs.insert(inputs.end(),refs.begin(),refs.end());return inputs;},[&](const auto& refs){
    auto summary=ValidateSplitOverlay(split);PublishTextArtifact(output,refs,[&](std::ostream& out){out<<summary<<'\n';});
  },[&]{return std::vector<ArtifactRef>{ArtifactRef::Bind(output,"split-validation-v1")};});
}
StageResult BuildSplitOverlay(const std::vector<ArtifactRef>& original,const std::string& output,
                              std::string* validated_summary){
  if(validated_summary)validated_summary->clear();
  std::vector<ArtifactRef> inputs;std::string descriptor;
  return RunStage("phase6.split",[&]{
    Check(original.size()==3 && original[0].schema()=="emuir-compact-v2" && original[1].schema()=="assignment-index-v4" && original[2].schema()=="tdm-schedule-store-v4","source role/schema disagrees");inputs=original;
    SqliteStore assignment(inputs[1]);descriptor=Meta(assignment.database(),"semantic_contract_binary");if(descriptor=="null")return inputs;
    const auto artifact=Text(assignment.database(),descriptor,"artifact");Check(fs::path(artifact).filename()==artifact,"invalid exact owner path");
    const auto version=Text(assignment.database(),descriptor,"schema");Check(version=="emuflow.phase3-static-exact-binary/v1" || version=="emuflow.phase3-static-exact-binary/v2","unsupported exact owner schema");
    inputs.push_back(ArtifactRef::Bind((fs::path(inputs[1].path()).parent_path()/artifact).string(),version=="emuflow.phase3-static-exact-binary/v1"?"phase3-exact-data-v1":"phase3-exact-data-v2"));return inputs;
  },[&](const auto& bound){
    EmuirView source(bound[0]);AssignmentView assignment_view(bound[1]);assignment_view.VisitCopies([](const InstanceCopy&){});(void)assignment_view.ValidateCuts();Check(assignment_view.source_cells()==source.counts().cells,"assignment/source cell count disagrees");SqliteStore assignment(bound[1]);SqliteStore schedule(bound[2]);
    const auto design=Meta(source.store().database(),"design_json"),header=Meta(schedule.database(),"header");
    Check(Text(schedule.database(),header,"schema")=="emuflow.tdm-schedule/v1","invalid schedule header");
    Check(Text(source.store().database(),design,"name")==Text(schedule.database(),header,"design"),"source/schedule design disagrees");
    const auto semantic=Meta(assignment.database(),"semantic_contract_sha256");
    Check(Member(schedule.database(),header,"semantic_contract_sha256")==semantic,"assignment/schedule semantic identity disagrees");
    if(bound.size()==4)(void)ValidateExactBinaryArtifact(bound[3],descriptor);
    SqliteArtifactWriter writer(output,"split-netlists-v3",bound);auto& db=writer.database();db.Execute(kSplitOverlayDDL);db.Execute("PRAGMA temp_store=FILE;PRAGMA cache_size=-131072");
    Attach(db,bound[0],"src");Attach(db,bound[1],"assignment");Attach(db,bound[2],"schedule");
    Check(Scalar(db,"SELECT count(*) FROM schedule.schedule_entries e LEFT JOIN assignment.parts p ON p.fpga=e.sink_fpga LEFT JOIN assignment.parts s ON s.fpga=e.source_fpga WHERE p.part_index IS NULL OR s.part_index IS NULL")==0,"schedule references unknown part");
    Check(Scalar(db,"SELECT count(*) FROM assignment.cut_sinks c LEFT JOIN schedule.schedule_entries e ON e.net=c.net_id AND e.sink_fpga=(SELECT fpga FROM assignment.parts WHERE part_index=c.sink_part_index) WHERE e.entry_id IS NULL")==0,"schedule misses a cut sink");
    const auto shadow_pin=Scalar(db,"SELECT coalesce((SELECT seq FROM src.pin_names WHERE name='shadow_out'),(SELECT coalesce(max(seq),0)+1 FROM src.pin_names))");
    const auto empty=Scalar(db,"SELECT seq FROM src.payloads WHERE value='{}'");
    Object references{{"source",Reference(bound[0])},{"assignment",Reference(bound[1])},{"schedule",Reference(bound[2])}};if(bound.size()==4)references["exact"]=Reference(bound[3]);
    std::map<std::string,std::string> metadata{{"schema",JsonQuote("emuflow.split-netlists-sqlite/v3")},{"logical_schema",JsonQuote(Meta(source.store().database(),"logical_schema"))},{"design",design},{"platform",Member(schedule.database(),header,"platform")},{"warnings",Meta(source.store().database(),"warnings_json")},{"semantic_contract_sha256",semantic},{"semantic_contract_binary",descriptor},{"source_bindings",JsonRecord(references).Encode()},{"shadow_pin_seq",std::to_string(shadow_pin)},{"shadow_extras_seq",std::to_string(empty)}};
    std::vector<std::string> clocks;auto clock=source.store().database().Prepare("SELECT record_json FROM clocks ORDER BY seq");while(clock.Step()){Check(clocks.size()<16384,"clock catalog exceeds bound");clocks.push_back(clock.TypedText(0));}std::string clock_json="[";for(const auto& item:clocks){if(clock_json.size()>1)clock_json+=',';clock_json+=item;}metadata["clocks"]=clock_json+']';
    {auto q=db.Prepare("INSERT INTO metadata VALUES (?,?)");for(const auto& item:metadata){q.BindText(1,item.first);q.BindText(2,item.second);q.Execute();q.Reset();}}
    {SqliteTransaction transaction(db);db.Execute(kSplitOverlayBuildSQL);transaction.Commit();}
    auto refs=Sources(db,"main");Install(db,refs,"main","");
    metadata["validation"]=Summary(db);
    {auto q=db.Prepare("INSERT INTO main.metadata VALUES ('validation',?)");q.BindText(1,metadata["validation"]);q.Execute();}
    RequireSources(refs);writer.Commit();
    if(validated_summary)*validated_summary=metadata.at("validation");
  },[&]{return std::vector<ArtifactRef>{ArtifactRef::Bind(output,"split-netlists-v3")};});
}
}
