#include "physical/physical_cells.hpp"
#include "common/artifact_io.hpp"
#include "common/json_record.hpp"
#include "storage/store.hpp"
#include <map>
#include <set>

namespace emuflow::core {
namespace {
void Check(bool condition, const std::string& message) {
  if (!condition) throw StageError(ErrorCode::kInvalidArtifact, message);
}
FileGeneration ReadGeneration(CheckedRequestReader& reader) {
  FileGeneration value;
  value.device=reader.Read<std::uint64_t>(); value.inode=reader.Read<std::uint64_t>();
  value.bytes=reader.Read<std::uint64_t>();
  value.modified_seconds=reader.Read<std::int64_t>(); value.modified_nanoseconds=reader.Read<std::int64_t>();
  value.changed_seconds=reader.Read<std::int64_t>(); value.changed_nanoseconds=reader.Read<std::int64_t>();
  return value;
}
std::int64_t Count(SqliteDatabase& db, const std::string& query) {
  auto row=db.Prepare(query); Check(row.Step(),"physical population count is missing");
  const auto count=row.TypedInteger(0); Check(count>=0 && !row.Step(),"physical population count is invalid");
  return count;
}
void NoRows(SqliteDatabase& db, const std::string& query, const char* message) {
  auto row=db.Prepare(query); Check(!row.Step(),message);
}
}  // namespace

void ValidatePhysicalCells(const ArtifactRef& mapped, const ArtifactRef& physical,
                           const ArtifactRef& request_ref, const std::string& summary) {
  const std::vector<ArtifactRef> inputs{mapped,physical,request_ref};
  for (const auto& input:inputs) input.RequireUnchanged();
  CheckedRequestReader request(request_ref.path(),4096);
  Check(request.Read<std::uint64_t>()==0x31564350464d5545ULL && request.Read<std::uint32_t>()==1,
        "unsupported physical population request");
  const auto mapped_generation=ReadGeneration(request),physical_generation=ReadGeneration(request);
  if (!(mapped_generation==mapped.generation()) || !(physical_generation==physical.generation()))
    throw StageError(ErrorCode::kMixedGeneration,"physical population source generation changed");
  const auto mapped_sha=request.ReadString32(false,64),top=request.ReadString32(false,1024);
  Check(mapped_sha.size()==64 && mapped_sha.find_first_not_of("0123456789abcdef")==std::string::npos,
        "physical population mapped digest is invalid");
  request.RequireEnd();
  SqliteStore source(mapped);
  auto& db=source.database();
  db.Execute("PRAGMA temp_store=FILE; PRAGMA cache_size=-16384; PRAGMA temp.cache_size=-16384");
  auto attach=db.Prepare("ATTACH DATABASE ? AS physical");
  attach.BindText(1,SqliteImmutableUri(physical.path())); attach.Execute();
  {
    auto row=db.Prepare("SELECT json_extract(value,'$') FROM metadata WHERE key='top_json'");
    Check(row.Step() && row.TypedText(0)==top && !row.Step(),"mapped top identity disagrees");
    auto header=db.Prepare("SELECT json_extract(value_json,'$.top'),"
        "json_extract(value_json,'$.source.mapped_json_sha256') FROM physical.document WHERE name='packed'");
    Check(header.Step() && header.TypedText(0)==top && header.TypedText(1)==mapped_sha && !header.Step(),
          "physical population mapped source identity disagrees");
  }
  NoRows(db,"SELECT 1 FROM cells WHERE typeof(id)!='text' OR length(id)=0 OR typeof(seq)!='integer' "
      "OR seq<0 OR typeof(type)!='text' OR length(type)=0 OR NOT json_valid(record_json) LIMIT 1",
      "mapped cell metadata is invalid");
  NoRows(db,"SELECT 1 FROM cells WHERE json_type(record_json)!='object' "
      "OR json_extract(record_json,'$.type') IS NOT type LIMIT 1",
      "mapped cell type projection disagrees");
  NoRows(db,"SELECT 1 FROM cells GROUP BY id HAVING count(*)!=1 LIMIT 1","mapped cell identity repeats");
  NoRows(db,"SELECT 1 FROM cells GROUP BY seq HAVING count(*)!=1 LIMIT 1","mapped cell sequence repeats");
  const std::set<std::string> supported{
      "LUT1","LUT2","LUT3","LUT4","LUT5","LUT6","LUT6_2", "CARRY8",
      "FDCE","FDPE","FDRE","FDSE","MUXF7","MUXF8","MUXF9",
      "DSP48E2","RAMB18E2","RAMB36E2","URAM288","GND","VCC"};
  std::int64_t cells=0,constants=0;
  {
    auto rows=db.Prepare("SELECT type,count(*) FROM cells GROUP BY type ORDER BY type");
    while (rows.Step()) {
      const auto type=rows.TypedText(0); const auto count=rows.TypedInteger(1);
      Check(supported.count(type) && count>0,"physical population contains unsupported primitives: "+type);
      cells+=count; if (type=="GND" || type=="VCC") constants+=count;
    }
  }
  // Check both directions, including constants and duplicate identities. A
  // count-only comparison cannot prove coverage when a name was substituted.
  NoRows(db,"SELECT 1 FROM physical.assignment a LEFT JOIN cells c ON c.id=a.instance "
      "WHERE c.id IS NULL OR c.type IS NOT a.cell_type OR c.type IN ('GND','VCC') LIMIT 1",
      "PackedSiteNetlist assignment does not match source");
  NoRows(db,"SELECT 1 FROM cells c LEFT JOIN physical.assignment a ON a.instance=c.id "
      "WHERE c.type NOT IN ('GND','VCC') AND a.instance IS NULL LIMIT 1",
      "PackedSiteNetlist cell ownership is incomplete");
  NoRows(db,"SELECT 1 FROM physical.assignment GROUP BY instance HAVING count(*)!=1 LIMIT 1",
      "PackedSiteNetlist assigns a cell more than once");
  NoRows(db,"SELECT 1 FROM physical.constant k LEFT JOIN cells c ON c.id=k.name "
      "WHERE c.id IS NULL OR c.type NOT IN ('GND','VCC') LIMIT 1",
      "PackedSiteNetlist constants are invalid");
  NoRows(db,"SELECT 1 FROM cells c LEFT JOIN physical.constant k ON k.name=c.id "
      "WHERE c.type IN ('GND','VCC') AND k.name IS NULL LIMIT 1",
      "PackedSiteNetlist constants are incomplete");
  NoRows(db,"SELECT 1 FROM physical.constant GROUP BY name HAVING count(*)!=1 LIMIT 1",
      "PackedSiteNetlist constant identity repeats");
  NoRows(db,"SELECT 1 FROM physical.assignment a LEFT JOIN physical.cluster c ON c.ordinal=a.cluster_ordinal "
      "WHERE c.ordinal IS NULL OR typeof(a.bel)!='text' OR length(a.bel)=0 LIMIT 1",
      "PackedSiteNetlist has orphan assignments or invalid BELs");
  NoRows(db,"SELECT 1 FROM physical.assignment GROUP BY cluster_ordinal,bel HAVING count(*)!=1 LIMIT 1",
      "PackedSiteNetlist reuses a BEL");
  // Every typed pin is checked against its source owner. Port completeness
  // checks the exact ordinal range, width and direction without reconstructing
  // a cell dictionary or opening one query for every cell.
  NoRows(db,"SELECT 1 FROM cell_pins p LEFT JOIN cells c ON c.id=p.cell_id "
      "WHERE c.id IS NULL OR p.cell_seq IS NOT c.seq OR typeof(p.port)!='text' OR length(p.port)=0 "
      "OR typeof(p.port_bit)!='integer' OR typeof(p.port_width)!='integer' OR p.port_width<=0 "
      "OR p.port_bit<0 OR p.port_bit>=p.port_width OR typeof(p.direction)!='integer' "
      "OR p.direction NOT IN (0,1,2) OR typeof(p.value_kind)!='integer' OR p.value_kind NOT IN (0,1) "
      "OR (p.value_kind=0 AND (typeof(p.net_bit)!='integer' OR p.constant IS NOT NULL)) "
      "OR (p.value_kind=1 AND (typeof(p.constant)!='text' OR p.net_bit IS NOT NULL)) LIMIT 1",
      "indexed mapped pin row is invalid");
  NoRows(db,"SELECT 1 FROM cell_pins GROUP BY cell_id,port HAVING count(*)!=max(port_width) "
      "OR count(DISTINCT port_bit)!=count(*) OR min(port_bit)!=0 OR max(port_bit)!=max(port_width)-1 "
      "OR min(port_width)!=max(port_width) OR min(direction)!=max(direction) LIMIT 1",
      "indexed mapped port is incomplete or has conflicting directions");
  {
    auto duplicate=db.Prepare("SELECT net_bit FROM cell_pins WHERE value_kind=0 AND direction=1 "
        "GROUP BY net_bit HAVING count(*)>1 ORDER BY net_bit LIMIT 1");
    if (duplicate.Step()) throw StageError(ErrorCode::kInvalidArtifact,
        "mapped net bit "+std::to_string(duplicate.TypedInteger(0))+" has multiple drivers");
  }
  const auto assignments=Count(db,"SELECT count(*) FROM physical.assignment");
  const auto clusters=Count(db,"SELECT count(*) FROM physical.cluster");
  const auto pin_count=Count(db,"SELECT count(*) FROM cell_pins");
  Check(assignments==cells-constants && Count(db,"SELECT count(*) FROM physical.constant")==constants,
        "physical population coverage disagrees");
  {
    auto manifest=db.Prepare("SELECT assignments,packed_clusters,constants FROM physical.manifest WHERE singleton=1");
    Check(manifest.Step() && manifest.TypedInteger(0)==assignments && manifest.TypedInteger(1)==clusters &&
          manifest.TypedInteger(2)==constants && !manifest.Step(),"physical population manifest coverage disagrees");
  }
  const JsonRecord result(std::map<std::string,JsonRecord>{
      {"schema","emuflow.physical-cell-validation/v1"},{"status","pass"},
      {"implementation_id","native-typed-physical-cells-v1"},
      {"cells",cells},{"assignments",assignments},{"constants",constants},
      {"clusters",clusters},{"pins",pin_count},{"driver_uniqueness","complete-mapped-output-population"}});
  PublishTextArtifact(summary,inputs,[&](std::ostream& stream){ stream<<result.Encode()<<'\n'; });
}
}  // namespace emuflow::core
