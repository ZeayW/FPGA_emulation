#include "emuflow_core/vivado_lineage.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/sqlite.hpp"
#include <charconv>
#include <filesystem>
#include <fstream>
#include <map>
#include <set>
#include <sstream>

namespace emuflow::core {
namespace {
constexpr const char* kSchema="emuflow.vivado-cell-lineage/v2";
constexpr const char* kCoordinates="native-device-tile-column-row-v1";
void Check(bool valid,const std::string& message) {
  if (!valid) throw StageError(ErrorCode::kInvalidArtifact,"Vivado lineage "+message);
}
std::string Hex(const std::array<unsigned char,32>& bytes) {
  static const char* digits="0123456789abcdef";std::string out;
  for (auto byte:bytes) {out+=digits[byte>>4];out+=digits[byte&15];}return out;
}
std::string FileHash(const std::string& path) {
  Sha256 digest;std::ifstream in(path,std::ios::binary);std::array<char,65536> buffer;
  Check(bool(in),"file cannot be read");
  while (in) {in.read(buffer.data(),buffer.size());digest.update(reinterpret_cast<const unsigned char*>(buffer.data()),in.gcount());}
  Check(in.eof(),"file read failed");return Hex(digest.finish());
}
std::int64_t Integer(const std::string& value) {
  std::int64_t result=0;auto parsed=std::from_chars(value.data(),value.data()+value.size(),result);
  Check(!value.empty() && parsed.ec==std::errc{} && parsed.ptr==value.data()+value.size() && result>=0 && std::to_string(result)==value,"invalid integer");
  return result;
}
std::vector<std::string> Fields(const std::string& text) {
  Check(text.find('\0')==std::string::npos,"inventory contains NUL");
  std::vector<std::string> out;std::size_t begin=0;
  while (true) {auto end=text.find('\t',begin);out.push_back(text.substr(begin,end-begin));if(end==std::string::npos)break;begin=end+1;}
  return out;
}
std::string Unhex(const std::string& value) {
  Check(value.size()%2==0,"invalid transformation encoding");std::string out;
  auto nibble=[](char c) {return c>='0'&&c<='9'?c-'0':c>='a'&&c<='f'?c-'a'+10:c>='A'&&c<='F'?c-'A'+10:-1;};
  for(std::size_t i=0;i<value.size();i+=2) {int a=nibble(value[i]),b=nibble(value[i+1]);Check(a>=0&&b>=0,"invalid transformation encoding");out+=char(a*16+b);}
  (void)JsonQuote(out);return out;
}
std::int64_t Count(SqliteDatabase& db,const std::string& sql,const std::string& stage="") {
  auto q=db.Prepare(sql);if(!stage.empty())q.BindText(1,stage);Check(q.Step(),"count is absent");return q.TypedInteger(0);
}
void Meta(SqliteStatement& insert,const std::string& key,const std::string& value) {
  insert.BindText(1,key);insert.BindText(2,value);insert.Execute();insert.Reset();
}
}
void ImportVivadoLineage(const std::vector<ArtifactRef>& inputs,const std::string& output,const std::string& summary) {
  Check(inputs.size()>=4 && inputs.size()<=67,"input count is invalid");
  Check(inputs[2].generation().bytes<=65536,"request is too large");
  for(const auto& ref:inputs)ref.RequireUnchanged();
  StagedArtifact staged(output,{".sqlite",".sqlite-journal",".sqlite-wal",".sqlite-shm"});
  const auto database_path=staged.path()+".sqlite";
  std::map<std::string,JsonRecord> stage_metrics;std::string mapped_sha,retain;std::int64_t source_count=0;
  {
    SqliteDatabase db(database_path,SqliteDatabase::Mode::kCreateExclusive);
    db.Execute("PRAGMA foreign_keys=ON; PRAGMA application_id=1162696268; PRAGMA user_version=2;");
    SqliteTransaction transaction(db);
    std::ifstream request_file(inputs[2].path(),std::ios::binary);
    std::string request((std::istreambuf_iterator<char>(request_file)),{});
    auto valid=db.Prepare("SELECT json_valid(?1)");valid.BindText(1,request);Check(valid.Step()&&valid.Integer(0)==1,"request JSON is invalid");
    auto keys=db.Prepare("SELECT key,type FROM json_each(?1)");keys.BindText(1,request);
    std::set<std::string> seen;
    while(keys.Step())Check(seen.insert(keys.Text(0)).second,"request repeats a field");
    Check(seen==std::set<std::string>{"schema","mapped_sha256","retain_stage","stages"},"request fields are invalid");
    auto header=db.Prepare("SELECT json_extract(?1,'$.schema'),json_type(?1,'$.mapped_sha256'),json_extract(?1,'$.mapped_sha256'),json_type(?1,'$.retain_stage'),json_extract(?1,'$.retain_stage'),json_type(?1,'$.stages')");
    header.BindText(1,request);Check(header.Step()&&header.Text(0)=="emuflow.vivado-lineage-import/v1"&&header.Text(1)=="text"&&header.Text(3)=="text"&&header.Text(5)=="array","request header is invalid");
    mapped_sha=header.Text(2);retain=header.Text(4);
    Check(mapped_sha.size()==64&&mapped_sha.find_first_not_of("0123456789abcdef")==std::string::npos,"mapped digest is invalid");
    std::vector<std::string> stages;seen.clear();
    auto stage_rows=db.Prepare("SELECT value,type FROM json_each(?1,'$.stages') ORDER BY CAST(key AS INTEGER)");stage_rows.BindText(1,request);
    while(stage_rows.Step()) {auto stage=stage_rows.Text(0);Check(stage_rows.Text(1)=="text"&&!stage.empty()&&seen.insert(stage).second,"inventory stage identity is invalid");stages.push_back(stage);}
    Check(stages.size()==inputs.size()-3&&seen.count(retain),"retained stage is absent or inventory coverage disagrees");
    auto attach=db.Prepare("ATTACH DATABASE ? AS mapped");attach.BindText(1,SqliteImmutableUri(inputs[0].path()));attach.Execute();
    auto functions=db.Prepare("ATTACH DATABASE ? AS functions");functions.BindText(1,SqliteImmutableUri(inputs[1].path()));functions.Execute();
    auto owner=db.Prepare("SELECT value FROM functions.metadata WHERE key='mapped_index_sha256'");
    Check(owner.Step()&&owner.Text(0)==mapped_sha,"function signatures belong to a different mapped input");
    source_count=Count(db,"SELECT count(*) FROM mapped.cells");
    Check(Count(db,"SELECT count(*) FROM functions.signatures")==source_count&&
          Count(db,"SELECT count(*) FROM mapped.cells c LEFT JOIN functions.signatures f ON c.seq=f.seq WHERE f.seq IS NULL")==0,"function-signature source coverage disagrees");
    db.Execute(R"SQL(
      CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
      CREATE TABLE sites(seq INTEGER PRIMARY KEY,stage TEXT NOT NULL,site TEXT NOT NULL,
        tile_column INTEGER NOT NULL CHECK(typeof(tile_column)='integer' AND tile_column>=0),
        tile_row INTEGER NOT NULL CHECK(typeof(tile_row)='integer' AND tile_row>=0),UNIQUE(stage,site));
      CREATE TABLE objects(stage TEXT NOT NULL,name TEXT NOT NULL,ref TEXT NOT NULL,origin INTEGER NOT NULL,transformations TEXT NOT NULL,
        site_seq INTEGER REFERENCES sites(seq),bel TEXT,primitive_level TEXT NOT NULL,PRIMARY KEY(stage,name),
        CHECK((site_seq IS NULL AND bel IS NULL) OR (site_seq IS NOT NULL AND bel IS NOT NULL AND length(bel)>0))) WITHOUT ROWID;
      CREATE INDEX objects_origin ON objects(stage,origin);
      CREATE UNIQUE INDEX objects_placement ON objects(stage,site_seq,bel) WHERE site_seq IS NOT NULL;
    )SQL");
    auto insert=db.Prepare("INSERT INTO objects VALUES(?,?,?,?,?,?,?,?)");
    auto add_site=db.Prepare("INSERT OR IGNORE INTO sites(stage,site,tile_column,tile_row) VALUES(?,?,?,?)");
    auto find_site=db.Prepare("SELECT seq,tile_column,tile_row FROM sites WHERE stage=? AND site=?");
    for(std::size_t index=0;index<stages.size();++index) {
      const auto& stage=stages[index];Sha256 digest;std::ifstream in(inputs[index+3].path(),std::ios::binary);Check(bool(in),"inventory cannot be opened");
      std::string line;bool first=true;
      while(std::getline(in,line)) {
        Check(line.size()<=1024*1024,"inventory record is too large");
        digest.update(reinterpret_cast<const unsigned char*>(line.data()),line.size());
        if(!in.eof()) {const unsigned char newline='\n';digest.update(&newline,1);}
        if(!line.empty()&&line.back()=='\r')line.pop_back();
        auto row=Fields(line);
        if(first) {Check(row==std::vector<std::string>{"name","ref_name","source_origin","transformations_hex","site","bel","tile_column","tile_row","primitive_level"},"inventory header is invalid");first=false;continue;}
        Check(row.size()==9&&!row[0].empty()&&!row[1].empty()&&row[2].size()>1&&row[2][0]=='s',"contains invalid or duplicate objects");
        (void)JsonQuote(row[0]);(void)JsonQuote(row[1]);
        auto origin=Integer(row[2].substr(1));auto transformations=Unhex(row[3]);
        insert.BindText(1,stage);insert.BindText(2,row[0]);insert.BindText(3,row[1]);insert.BindInteger(4,origin);insert.BindText(5,transformations);
        Check((row[8].empty()&&stage!="routed")||row[8]=="LEAF"||row[8]=="INTERNAL"||row[8]=="MACRO","unknown mapped primitive level");
        insert.BindText(8,row[8]);
        bool placed=!row[4].empty()||!row[5].empty()||!row[6].empty()||!row[7].empty();
        if(placed) {
          Check(row[8]!="MACRO","macro wrapper must not consume placement");
          Check(!row[4].empty()&&!row[5].empty(),"partial placement is invalid");(void)JsonQuote(row[4]);(void)JsonQuote(row[5]);
          auto column=Integer(row[6]),r=Integer(row[7]);Check(column<=2147483647&&r<=2147483647,"placement exceeds integer bounds");
          add_site.BindText(1,stage);add_site.BindText(2,row[4]);add_site.BindInteger(3,column);add_site.BindInteger(4,r);add_site.Execute();add_site.Reset();
          find_site.BindText(1,stage);find_site.BindText(2,row[4]);
          Check(find_site.Step() && find_site.Integer(1)==column && find_site.Integer(2)==r,"one site has inconsistent native coordinates");
          insert.BindInteger(6,find_site.Integer(0));insert.BindText(7,row[5]);find_site.Reset();
        } else {Check(stage!="routed"||row[1]=="GND"||row[1]=="VCC"||row[8]=="MACRO","routed object lacks placement");insert.BindNull(6);insert.BindNull(7);}
        try {insert.Execute();insert.Reset();} catch(const std::exception& e) {throw StageError(ErrorCode::kInvalidArtifact,std::string("Vivado lineage contains invalid or duplicate objects: ")+e.what());}
      }
      Check(in.eof()&&!first,"inventory is truncated");
      Check(Count(db,"SELECT count(*) FROM objects a LEFT JOIN mapped.cells c ON a.origin=c.seq WHERE a.stage=? AND c.seq IS NULL",stage)==0,"contains an unknown source origin");
      const auto objects=Count(db,"SELECT count(*) FROM objects WHERE stage=?",stage);
      const auto observed=Count(db,"SELECT count(DISTINCT origin) FROM objects WHERE stage=?",stage);
      const auto placed=Count(db,"SELECT count(*) FROM objects WHERE stage=? AND site_seq IS NOT NULL",stage);
      stage_metrics.emplace(stage,std::map<std::string,JsonRecord>{
        {"observed_objects",objects},{"source_cells_with_lineage",observed},{"unretained_source_cells",source_count-observed},
        {"source_cells_with_multiple_objects",Count(db,"SELECT count(*) FROM (SELECT origin FROM objects WHERE stage=? GROUP BY origin HAVING count(*)>1)",stage)},
        {"changed_type_objects",Count(db,"SELECT count(*) FROM objects a JOIN mapped.cells c ON c.seq=a.origin WHERE a.stage=? AND a.ref!=c.type",stage)},
        {"placed_objects",placed},{"unplaced_objects",objects-placed},{"inventory_sha256",Hex(digest.finish())}});
      if(stage!=retain) {auto remove=db.Prepare("DELETE FROM objects WHERE stage=?");remove.BindText(1,stage);remove.Execute();
        auto remove_sites=db.Prepare("DELETE FROM sites WHERE stage=?");remove_sites.BindText(1,stage);remove_sites.Execute();}
    }
    auto metadata=db.Prepare("INSERT INTO metadata VALUES(?,?)");
    Meta(metadata,"schema",kSchema);Meta(metadata,"coordinate_system",kCoordinates);
    Meta(metadata,"mapped_index_sha256",mapped_sha);Meta(metadata,"retained_stage",retain);
    Meta(metadata,"stages_json",JsonRecord(stage_metrics).Encode());
    transaction.Commit();
  }
  for(const auto& ref:inputs)ref.RequireUnchanged();
  const auto sha=FileHash(database_path);const auto bytes=std::filesystem::file_size(database_path);
  AtomicBinaryWriter result(summary,inputs);
  result.stream()<<JsonRecord(std::map<std::string,JsonRecord>{
    {"schema",kSchema},{"coordinate_system",kCoordinates},{"mapped_index_sha256",mapped_sha},
    {"implementation_id","native-vivado-lineage-v1"},{"status","pass"},{"provider","vivado-optimized-source-lineage-v1"},{"source_cells",source_count},
    {"stages",stage_metrics},{"output",output},{"sha256",sha},{"bytes",bytes},
    {"qualification","observed-source-dispositions; not-an-equivalence-proof"}}).Encode()<<'\n';
  PublishTemporaryFile(database_path,output,inputs);result.Commit();
}
}
