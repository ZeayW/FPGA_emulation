#include "timing/sta_weights.hpp"
#include "storage/schema/sta_path_schema.hpp"
#include "storage/schema/sta_weight_schema.hpp"
#include "storage/store.hpp"
#include "common/json_record.hpp"
#include <algorithm>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <limits>

namespace emuflow::core {
namespace {
using I=std::int64_t;
using Object=std::map<std::string,JsonRecord>;
void Check(bool valid,const std::string& reason) {
  if(!valid)throw StageError(ErrorCode::kInvalidArtifact,"STA net weights "+reason);
}
std::string Text(SqliteStatement& row,int column) {
  const auto value=row.TypedText(column);Check(!value.empty() && value.find('\0')==std::string::npos,"identity is empty or contains NUL");(void)JsonQuoteUtf8(value);return value;
}
void Json(SqliteDatabase& db,const std::string& text) {
  auto valid=db.Prepare("SELECT json_valid(?1),json_type(?1)");valid.BindText(1,text);
  Check(valid.Step() && valid.Integer(0) && valid.Text(1)=="object","control object is invalid");
  auto duplicate=db.Prepare("SELECT 1 FROM json_tree(?1) WHERE parent IS NOT NULL GROUP BY parent,key HAVING count(*)>1 LIMIT 1");duplicate.BindText(1,text);
  Check(!duplicate.Step(),"control object has duplicate fields");
}
std::string ReadSmall(SqliteDatabase& db,const std::string& path) {
  const auto size=std::filesystem::file_size(path);Check(size<=512*1024,"selector exceeds bound");
  std::ifstream stream(path,std::ios::binary);std::string text(size,'\0');stream.read(text.data(),size);Check(bool(stream),"selector cannot be read");(void)JsonQuoteUtf8(text);Json(db,text);return text;
}
std::string String(SqliteDatabase& db,const std::string& object,const std::string& path) {
  auto row=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");row.BindText(1,object);row.BindText(2,path);
  Check(row.Step() && row.Text(0)=="text","requires text "+path);return Text(row,1);
}
std::string ObjectAt(SqliteDatabase& db,const std::string& object,const std::string& path) {
  auto row=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");row.BindText(1,object);row.BindText(2,path);
  Check(row.Step() && row.Text(0)=="object","requires object "+path);return row.TypedText(1);
}
I Integer(SqliteDatabase& db,const std::string& object,const std::string& path) {
  auto row=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");row.BindText(1,object);row.BindText(2,path);
  Check(row.Step() && row.Text(0)=="integer","requires integer "+path);const auto value=row.TypedInteger(1);Check(value>=0,"negative "+path);return value;
}
double Number(SqliteDatabase& db,const std::string& object,const std::string& key) {
  auto row=db.Prepare("SELECT type FROM json_each(?1) WHERE key=?2");row.BindText(1,object);row.BindText(2,key);
  Check(row.Step() && (row.Text(0)=="integer" || row.Text(0)=="real"),"requires number "+key);
  return ParseFiniteDecimal(JsonMemberToken(db,object,key),"STA net weight "+key);
}
std::string Request(const std::vector<ArtifactRef>& inputs) {
  Check(inputs.size()==4 || inputs.size()==6,"input coverage disagrees");
  CheckedRequestReader reader(inputs.back().path(),256*1024);
  Check(reader.Read<std::uint64_t>()==0x31574754534d5545ULL && reader.Read<std::uint32_t>()==1,"request version is invalid");
  for(std::size_t i=0;i+1<inputs.size();++i)if(!(ReadFileGeneration(reader)==inputs[i].generation()))throw StageError(ErrorCode::kMixedGeneration,"STA weight original source changed");
  const auto result=reader.ReadString32(false,128*1024);reader.RequireEnd();return result;
}
void Attach(SqliteDatabase& db,const ArtifactRef& input,const std::string& alias) {
  auto row=db.Prepare("ATTACH DATABASE ? AS "+alias);row.BindText(1,SqliteImmutableUri(input.path()));row.Execute();
}
struct Configuration {
  double scale=0,exponent=0;I paths=0;std::string design,provider;
  double Weight(double criticality)const {
    // Match the reference's separately rounded multiplication and addition;
    // contraction to an FMA changes some fractional-exponent records by 1 ULP.
    volatile double product=scale*std::pow(criticality,exponent);
    const auto value=1.0+product;Check(std::isfinite(value) && value>0,"weight overflows");return value;
  }
};
void Payload(SqliteDatabase& db,const std::string& manifest,const std::string& path,const std::string& format,const ArtifactRef& payload) {
  const auto object=ObjectAt(db,manifest,path),relative=String(db,object,"$.path"),digest=String(db,object,"$.sha256");
  Check(String(db,object,"$.format")==format && std::filesystem::path(relative).filename()==relative && relative==std::filesystem::path(payload.path()).filename(),"payload identity disagrees");
  Check(digest.size()==64 && digest.find_first_not_of("0123456789abcdef")==std::string::npos,"payload seal is invalid");
}
Configuration Prepare(SqliteDatabase& db,const std::vector<ArtifactRef>& inputs,const std::string& request) {
  Json(db,request);Configuration c;c.scale=Number(db,request,"criticality_scale");c.exponent=Number(db,request,"criticality_exponent");
  Check(c.scale>0 && c.exponent>0,"power-law parameters must be positive");
  SqliteStore ir(inputs[0]),paths(inputs[2]);RequireSqliteDDL(paths.database(),kStaPathStoreDDL);
  Attach(db,inputs[0],"src");Attach(db,inputs[2],"timing");
  db.Execute("PRAGMA temp_store=FILE;PRAGMA cache_size=-8192;PRAGMA timing.cache_size=-32768;PRAGMA src.cache_size=-8192");
  const auto manifest=ReadSmall(db,inputs[1].path());
  Check(String(db,manifest,"$.schema")=="emuflow.sta-path-database/v3","requires current TimingPathDB owner");
  c.design=String(db,manifest,"$.design");c.provider=String(db,manifest,"$.source.provider");c.paths=Integer(db,manifest,"$.summary.paths");
  Check(c.provider=="opensta-fpga-path-database-v1" && c.paths>0,"path source/provider is invalid");
  Payload(db,manifest,"$.payloads.paths","sqlite-sta-path/v1",inputs[2]);Check(Integer(db,manifest,"$.payloads.paths.records")==c.paths,"path selector count disagrees");
  auto identity=db.Prepare("SELECT json_extract(value,'$.name') FROM src.metadata WHERE key='design_json'");Check(identity.Step() && Text(identity,0)==c.design,"path/EmuIR designs disagree");
  auto count=db.Prepare("SELECT count(*),min(ordinal),max(ordinal) FROM timing.paths");Check(count.Step() && count.TypedInteger(0)==c.paths && count.TypedInteger(1)==0 && count.TypedInteger(2)==c.paths-1,"path sequence/count disagrees");
  return c;
}
struct Summary {
  I paths=0,nets=0,weighted=0;double minimum=1,maximum=1;
  void Add(double criticality,double weight) {
    ++nets;if(criticality>0){if(weighted++==0){minimum=weight;maximum=weight;}else{minimum=std::min(minimum,weight);maximum=std::max(maximum,weight);}}
  }
  Object Fields()const{return {{"paths",paths},{"timed_nets",nets},{"weighted_nets",weighted},{"minimum_weight",minimum},{"maximum_weight",maximum}};}
};
// Independent ordered source reduction. The producer uses a SQL aggregate;
// this checker visits each source membership and retains only one live net.
Summary Audit(SqliteDatabase& db,const Configuration& c) {
  RequireSqliteDDL(db,kStaNetWeightStoreDDL);Summary result;result.paths=c.paths;
  auto rows=db.Prepare("SELECT n.net_id,p.clock_period_ns,p.slack_ns,s.seq FROM timing.path_nets n LEFT JOIN timing.paths p ON p.ordinal=n.path_ordinal LEFT JOIN src.net_records s ON s.id=n.net_id ORDER BY n.net_id");
  auto actual=db.Prepare("SELECT net_id,criticality,path_count,weight FROM net_weights ORDER BY net_id");bool next=actual.Step();
  std::string current;double criticality=0;I count=0;
  const auto finish=[&] {
    if(!count)return;
    Check(next && Text(actual,0)==current,"net coverage disagrees");
    const auto value=actual.TypedReal(1),weight=actual.TypedReal(3),expected=c.Weight(criticality);
    Check(std::isfinite(value) && value>=0 && value<=1 && actual.TypedInteger(2)==count && std::isfinite(weight) && weight>0,"weight row is invalid");
    Check(std::abs(value-criticality)<=1e-12 && std::abs(weight-expected)<=1e-12,"source weight reconstruction failed");
    result.Add(criticality,expected);next=actual.Step();
  };
  while(rows.Step()) {
    const auto net=Text(rows,0);Check(!rows.IsNull(3),"source net is unknown");
    const auto period=rows.TypedReal(1),slack=rows.TypedReal(2);Check(std::isfinite(period) && period>0 && std::isfinite(slack),"source path timing is invalid");
    if(net!=current){finish();current=net;criticality=0;count=0;}
    criticality=std::max(criticality,std::max(0.0,std::min(1.0,1.0-slack/period)));Check(count<std::numeric_limits<I>::max(),"path count overflows");++count;
  }
  finish();Check(!next,"weight owner has extra nets");return result;
}
void CompareSummary(SqliteDatabase& db,const std::string& manifest,const Summary& summary) {
  const auto object=ObjectAt(db,manifest,"$.summary");auto fields=db.Prepare("SELECT count(*) FROM json_each(?)");fields.BindText(1,object);Check(fields.Step() && fields.Integer(0)==5,"summary fields disagree");
  Check(Integer(db,object,"$.paths")==summary.paths && Integer(db,object,"$.timed_nets")==summary.nets && Integer(db,object,"$.weighted_nets")==summary.weighted && Number(db,object,"minimum_weight")==summary.minimum && Number(db,object,"maximum_weight")==summary.maximum,"summary disagrees");
}
void Output(AtomicBinaryWriter& writer,const Configuration& c,const Summary& summary,const char* schema) {
  Object result=summary.Fields();result.emplace("schema",schema);result.emplace("status","pass");result.emplace("design",c.design);result.emplace("implementation_id","native-sta-net-weights-v1");
  const auto text=JsonRecord(result).Encode();Check(text.size()<128*1024,"summary exceeds bound");writer.stream()<<text<<'\n';
}
}
void BuildStaWeights(const std::vector<ArtifactRef>& inputs,const std::string& store,const std::string& summary) {
  Check(inputs.size()==4,"producer inputs disagree");const auto request=Request(inputs);
  SqliteArtifactWriter writer(store,"net-weight-store-v1",inputs);AtomicBinaryWriter report(summary,inputs);
  {
    auto& db=writer.database();const auto c=Prepare(db,inputs,request);SqliteTransaction transaction(db);db.Execute(kStaNetWeightStoreDDL);
    db.Execute("INSERT INTO net_weights SELECT n.net_id,max(max(0.0,min(1.0,1.0-p.slack_ns/p.clock_period_ns))),count(*),1.0 FROM timing.path_nets n JOIN timing.paths p ON p.ordinal=n.path_ordinal GROUP BY n.net_id ORDER BY n.net_id");
    auto rows=db.Prepare("SELECT net_id,criticality FROM net_weights ORDER BY net_id"),put=db.Prepare("UPDATE net_weights SET weight=? WHERE net_id=?");
    while(rows.Step()){const auto criticality=rows.TypedReal(1);Check(std::isfinite(criticality) && criticality>=0 && criticality<=1,"criticality is invalid");put.BindReal(1,c.Weight(criticality));put.BindText(2,Text(rows,0));put.Execute();}
    rows=SqliteStatement{};put=SqliteStatement{};const auto checked=Audit(db,c);Output(report,c,checked,"emuflow.sta-net-weights-production/v1");transaction.Commit();
  }
  writer.Commit();report.Commit();
}
void ValidateStaWeights(const std::vector<ArtifactRef>& inputs,const std::string& summary) {
  Check(inputs.size()==6,"validator inputs disagree");const auto request=Request(inputs);SqliteStore weights(inputs[4]);auto& db=weights.database();const auto c=Prepare(db,inputs,request);
  const auto manifest=ReadSmall(db,inputs[3].path());Check(String(db,manifest,"$.schema")=="emuflow.partition-net-weights/v2" && String(db,manifest,"$.design")==c.design,"weight selector/design disagrees");
  Check(String(db,manifest,"$.source.provider")=="sta-max-criticality-power-law-v2-indexed" && String(db,manifest,"$.source.path_database")==std::filesystem::path(inputs[1].path()).filename() && String(db,manifest,"$.source.path_database_provider")==c.provider,"source binding disagrees");
  const auto parameters=ObjectAt(db,manifest,"$.parameters");Check(Number(db,parameters,"criticality_scale")==c.scale && Number(db,parameters,"criticality_exponent")==c.exponent && String(db,parameters,"$.criticality_definition")=="clamp(1-slack/period,0,1)" && String(db,parameters,"$.path_reduction")=="maximum","weight policy disagrees");
  Payload(db,manifest,"$.payloads.weights","sqlite-net-weights/v1",inputs[4]);
  const auto checked=Audit(db,c);Check(Integer(db,manifest,"$.payloads.weights.records")==checked.nets,"payload count disagrees");CompareSummary(db,manifest,checked);
  AtomicBinaryWriter report(summary,inputs);Output(report,c,checked,"emuflow.sta-net-weights-validation/v1");report.Commit();
}
}
