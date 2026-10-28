#include "emuflow_core/original_lineage_index.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/store.hpp"
#include <array>
#include <fstream>
#include <set>

namespace emuflow::core {
namespace {
constexpr std::uint64_t kMagic=0x31514c4f4e4d5545ULL;
void Check(bool ok,const char* message){if(!ok)throw StageError(ErrorCode::kInvalidArtifact,message);}
std::string Meta(SqliteDatabase& db,const std::string& key){
  auto q=db.Prepare("SELECT value FROM metadata WHERE key=?");q.BindText(1,key);
  Check(q.Step(),"original lineage metadata is absent");auto value=q.TypedText(0);
  Check(!q.Step(),"original lineage metadata repeats");return value;
}
std::int64_t Count(SqliteDatabase& db,const std::string& sql){
  auto q=db.Prepare(sql);Check(q.Step(),"original lineage count is absent");return q.TypedInteger(0);
}
void Attach(SqliteDatabase& db,const std::string& path,const std::string& name){
  auto q=db.Prepare("ATTACH DATABASE ? AS "+name);q.BindText(1,SqliteImmutableUri(path));q.Execute();
}
std::string FileHash(const std::string& path){
  std::ifstream input(path,std::ios::binary);Check(bool(input),"original lineage mapped input cannot be read");
  std::array<char,1024*1024> buffer;Sha256 hash;
  while(input){input.read(buffer.data(),buffer.size());hash.update(reinterpret_cast<const unsigned char*>(buffer.data()),input.gcount());}
  Check(input.eof(),"original lineage mapped input read failed");std::string result;
  for(auto byte:hash.finish()){result+="0123456789abcdef"[byte>>4];result+="0123456789abcdef"[byte&15];}
  return result;
}
std::vector<ArtifactRef> OriginalParents(SqliteDatabase& db){
  auto rows=db.Prepare("SELECT json_extract(j.value,'$.path'),json_extract(j.value,'$.schema'),"
    "json_extract(j.value,'$.generation[0]'),json_extract(j.value,'$.generation[1]'),"
    "json_extract(j.value,'$.generation[2]'),json_extract(j.value,'$.generation[3]'),"
    "json_extract(j.value,'$.generation[4]'),json_extract(j.value,'$.generation[5]'),"
    "json_extract(j.value,'$.generation[6]'),json_array_length(j.value,'$.generation') "
    "FROM metadata m,json_each(m.value)j WHERE m.key='source_generations' ORDER BY CAST(j.key AS INTEGER)");
  std::vector<ArtifactRef> result;std::set<std::string> paths;
  while(rows.Step()){
    Check(rows.TypedInteger(9)==7 && result.size()<128,"original lineage parent generation is invalid");
    const FileGeneration expected{static_cast<std::uint64_t>(rows.TypedInteger(2)),static_cast<std::uint64_t>(rows.TypedInteger(3)),
      static_cast<std::uint64_t>(rows.TypedInteger(4)),rows.TypedInteger(5),rows.TypedInteger(6),rows.TypedInteger(7),rows.TypedInteger(8)};
    auto parent=ArtifactRef::Bind(rows.TypedText(0),rows.TypedText(1));
    Check(parent.generation()==expected && paths.insert(parent.path()).second,"original lineage source parent generation differs");
    result.push_back(std::move(parent));
  }
  Check(!result.empty(),"original lineage source parents are absent");return result;
}
JsonRecord Parents(const std::vector<ArtifactRef>& parents){
  std::vector<JsonRecord> records;
  for(const auto& p:parents){const auto& g=p.generation();records.emplace_back(std::map<std::string,JsonRecord>{
      {"path",p.path()},{"schema",p.schema()},{"generation",JsonRecord(std::vector<JsonRecord>{g.device,g.inode,g.bytes,
       g.modified_seconds,g.modified_nanoseconds,g.changed_seconds,g.changed_nanoseconds})}});}
  return JsonRecord(std::move(records));
}
}
void BuildOriginalLineageIndex(const std::vector<ArtifactRef>& inputs,const std::string& output){
  Check(inputs.size()>=4 && inputs.size()<=130 && (inputs.size()-2)%2==0,"original lineage partition input coverage differs");
  CheckedRequestReader request(inputs[1].path(),1024*1024);
  Check(request.Read<std::uint64_t>()==kMagic && request.Read<std::uint32_t>()==1,"original lineage request version differs");
  const auto parts=request.Read<std::uint32_t>();Check(parts==(inputs.size()-2)/2 && parts>0 && parts<=64,"original lineage request partition count differs");
  for(std::size_t i=0;i<inputs.size();++i){if(i==1)continue;
    const FileGeneration expected{request.Read<std::uint64_t>(),request.Read<std::uint64_t>(),request.Read<std::uint64_t>(),
      request.Read<std::int64_t>(),request.Read<std::int64_t>(),request.Read<std::int64_t>(),request.Read<std::int64_t>()};
    Check(expected==inputs[i].generation(),"original lineage input generation differs from its request");
  }
  const auto stage=request.ReadString32();Check(stage=="synthesized"||stage=="optimized"||stage=="routed","original lineage observation stage is invalid");
  std::vector<std::string> names;for(std::uint32_t i=0;i<parts;++i)names.push_back(request.ReadString32());request.RequireEnd();
  SqliteStore original(inputs[0]);auto historical=OriginalParents(original.database());
  std::vector<ArtifactRef> durable{inputs[0]};std::set<std::string> paths{inputs[0].path()};
  for(const auto& refs:{historical,inputs})for(const auto& ref:refs){if(ref.path()==inputs[1].path())continue;
    if(paths.insert(ref.path()).second)durable.push_back(ref);}
  std::vector<std::string> mapped_parents;
  for(const auto& ref:historical)if(ref.schema()=="xilinx-mapped-index-v2")mapped_parents.push_back(ref.path());
  Check(mapped_parents.size()==parts,"original lineage original mapped source coverage differs");
  {
    auto rows=original.database().Prepare("SELECT part_index,fpga FROM parts ORDER BY part_index");std::size_t count=0;
    while(rows.Step()){Check(count<parts && rows.TypedInteger(0)==static_cast<std::int64_t>(count) && rows.TypedText(1)==names[count],
        "original lineage partition namespace differs");++count;}
    Check(count==parts,"original lineage partition coverage differs");
  }
  SqliteArtifactWriter writer(output,"native-original-lineage-index-v1",durable);auto& db=writer.database();Attach(db,inputs[0].path(),"original");
  db.Execute("PRAGMA cache_size=-65536;"
    "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;"
    "CREATE TABLE parts(part_index INTEGER PRIMARY KEY,fpga TEXT UNIQUE NOT NULL,mapped_sha256 TEXT NOT NULL,stage TEXT NOT NULL);"
    "CREATE TABLE source_dispositions(owner_seq INTEGER PRIMARY KEY,source_cell_seq INTEGER NOT NULL,part_index INTEGER NOT NULL,"
      "mapped_cell_seq INTEGER,observed_objects INTEGER NOT NULL,changed_type_objects INTEGER NOT NULL,status TEXT NOT NULL);"
    "CREATE INDEX source_disposition_cells ON source_dispositions(source_cell_seq);"
    "CREATE TABLE endpoint_dispositions(endpoint_seq INTEGER PRIMARY KEY,source_kind TEXT NOT NULL,copies INTEGER NOT NULL,"
      "observed_copies INTEGER NOT NULL,observed_objects INTEGER NOT NULL,status TEXT NOT NULL);"
    "CREATE TABLE members(ordinal INTEGER PRIMARY KEY,start_endpoint INTEGER NOT NULL,end_endpoint INTEGER NOT NULL,budget INTEGER NOT NULL);");
  for(std::uint32_t part=0;part<parts;++part){
    const auto& mapped=inputs[2+2*part];const auto& lineage=inputs[3+2*part];
    Check(mapped.path()==mapped_parents[part],"original lineage mapped partition owner differs");
    SqliteStore observed(lineage);
    Check(Meta(observed.database(),"retained_stage")==stage,"original lineage observations mix implementation stages");
    const auto digest=Meta(observed.database(),"mapped_index_sha256");
    Check(digest==FileHash(mapped.path()),"original lineage native objects belong to another mapped input");
    Attach(db,mapped.path(),"mapped");Attach(db,lineage.path(),"native");
    {
      SqliteTransaction transaction(db);
      auto insert_part=db.Prepare("INSERT INTO parts VALUES(?,?,?,?)");insert_part.BindInteger(1,part);insert_part.BindText(2,names[part]);
      insert_part.BindText(3,digest);insert_part.BindText(4,stage);insert_part.Execute();
      Check(Count(db,"SELECT count(*) FROM native.objects a LEFT JOIN mapped.cells c ON c.seq=a.origin WHERE a.stage='"+stage+"' AND c.seq IS NULL")==0,
          "original lineage native inventory has an unknown mapped source");
      Check(Count(db,"SELECT count(*) FROM original.cell_owners o LEFT JOIN mapped.cells c ON c.seq=o.mapped_cell_seq WHERE o.part_index="+
          std::to_string(part)+" AND o.mapped_cell_seq IS NOT NULL AND c.seq IS NULL")==0,"original lineage original mapped source is absent");
      db.Execute("CREATE TEMP TABLE native_counts(origin INTEGER PRIMARY KEY,objects INTEGER NOT NULL,changed INTEGER NOT NULL);");
      auto counts=db.Prepare("INSERT INTO native_counts SELECT a.origin,count(*),sum(a.ref!=c.type) FROM native.objects a "
        "JOIN mapped.cells c ON c.seq=a.origin WHERE a.stage=? GROUP BY a.origin");counts.BindText(1,stage);counts.Execute();
      auto copies=db.Prepare("INSERT INTO source_dispositions SELECT o.seq,o.source_cell_seq,o.part_index,o.mapped_cell_seq,"
        "coalesce(n.objects,0),coalesce(n.changed,0),CASE WHEN o.mapped_cell_seq IS NULL THEN 'mapped-copy-unavailable' "
        "WHEN n.origin IS NULL THEN 'native-origin-unretained' ELSE 'native-origin-observed' END "
        "FROM original.cell_owners o LEFT JOIN native_counts n ON n.origin=o.mapped_cell_seq WHERE o.part_index=?");
      copies.BindInteger(1,part);copies.Execute();db.Execute("DROP TABLE native_counts");transaction.Commit();
    }
    db.Execute("DETACH DATABASE native;DETACH DATABASE mapped");
  }
  {
    SqliteTransaction transaction(db);
    db.Execute("INSERT INTO endpoint_dispositions SELECT e.seq,e.source_kind,count(d.owner_seq),"
      "coalesce(sum(d.observed_objects>0),0),coalesce(sum(d.observed_objects),0),CASE "
      "WHEN e.source_cell_seq=-1 THEN 'original-port-observation-pending' WHEN count(d.owner_seq)=0 THEN 'partition-owner-unavailable' "
      "WHEN sum(d.observed_objects)>0 THEN 'native-source-collection-observed' ELSE 'native-source-collection-unretained' END "
      "FROM original.endpoints e LEFT JOIN source_dispositions d ON d.source_cell_seq=e.source_cell_seq GROUP BY e.seq;"
      "INSERT INTO members SELECT ordinal,start_endpoint,end_endpoint,budget FROM original.members;");
    const auto members=Count(db,"SELECT count(*) FROM members"),endpoints=Count(db,"SELECT count(*) FROM endpoint_dispositions"),copies=Count(db,"SELECT count(*) FROM source_dispositions");
    Check(members==Count(db,"SELECT count(*) FROM original.members") && endpoints==Count(db,"SELECT count(*) FROM original.endpoints") &&
        copies==Count(db,"SELECT count(*) FROM original.cell_owners"),"original lineage complete source/member coverage differs");
    Check(Count(db,"SELECT count(*) FROM members m LEFT JOIN endpoint_dispositions a ON a.endpoint_seq=m.start_endpoint "
      "LEFT JOIN endpoint_dispositions b ON b.endpoint_seq=m.end_endpoint WHERE a.endpoint_seq IS NULL OR b.endpoint_seq IS NULL")==0,
      "original lineage member endpoint is absent");
    auto meta=db.Prepare("INSERT INTO metadata VALUES(?,?)");
    const auto put=[&](const std::string& key,const std::string& value){meta.BindText(1,key);meta.BindText(2,value);meta.Execute();meta.Reset();};
    put("schema","emuflow.native-original-lineage-index/v1");put("status","complete-source-lineage-observations");put("stage",stage);
    put("source_generations",Parents(durable).Encode());put("members",std::to_string(members));put("endpoints",std::to_string(endpoints));put("copies",std::to_string(copies));
    put("observed_copies",std::to_string(Count(db,"SELECT count(*) FROM source_dispositions WHERE observed_objects>0")));
    put("changed_type_objects",std::to_string(Count(db,"SELECT coalesce(sum(changed_type_objects),0) FROM source_dispositions")));
    put("physical_acceptance","pending");put("original_path_sta_acceptance","pending");
    put("scope","Complete original members and source-copy/native-object observations. Absent tags and changed types do not reject normal optimization. Native pin/check association, rewrite/removal evidence and numerical global STA are separate; no delay is inferred from absence.");
    transaction.Commit();
  }
  for(const auto& ref:durable)ref.RequireUnchanged();for(const auto& ref:inputs)ref.RequireUnchanged();writer.Commit();
}
}
