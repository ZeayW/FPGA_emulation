#include "emuflow_core/assignment_publish.hpp"
#include "emuflow_core/assignment_schema.hpp"
#include "emuflow_core/emuir_view.hpp"
#include "emuflow_core/primary_cluster_view.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/hash.hpp"
#include <algorithm>
#include <map>
#include <set>

namespace emuflow::core {
namespace {
void Check(bool valid,const char* message) {
  if (!valid) throw StageError(ErrorCode::kInvalidArtifact,message);
}
std::string Identity(CheckedRequestReader& request) {
  auto text=request.ReadString32();
  Check(text.find('\0')==std::string::npos,"assignment identity contains NUL");
  JsonQuote(text); // Validate UTF-8 using the common canonical serializer.
  return text;
}
std::vector<std::string> Catalog(CheckedRequestReader& request) {
  const auto count=request.Read<std::uint32_t>();
  Check(count && count<=65536 && count<=request.remaining()/4,"assignment partition catalog is invalid");
  std::vector<std::string> parts; std::size_t bytes=0;
  for (std::uint32_t i=0;i<count;++i) {
    auto part=Identity(request); bytes+=part.size();
    Check(bytes<=1024*1024 && (parts.empty() || parts.back()<part),"assignment partition order is invalid");
    parts.push_back(std::move(part));
  }
  return parts;
}
std::string Hex(const std::array<unsigned char,32>& digest) {
  const char* digits="0123456789abcdef"; std::string out;
  for (auto byte:digest) { out+=digits[byte>>4];out+=digits[byte&15]; }
  return out;
}
void Put(SqliteStatement& statement,const std::string& key,const std::string& value) {
  statement.BindText(1,key);statement.BindText(2,value);statement.Execute();statement.Reset();
}
}
void PublishAssignmentIndex(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  Check(inputs.size()==3,"assignment publication source coverage disagrees");
  CheckedRequestReader request(inputs[2].path(),inputs[2].generation().bytes);
  Check(request.Read<std::uint64_t>()==0x31514241554d5545ULL && request.Read<std::uint32_t>()==1,
        "unsupported assignment publication request");
  for (unsigned i=0;i<2;++i) {
    FileGeneration generation;
    generation.device=request.Read<std::uint64_t>();
    generation.inode=request.Read<std::uint64_t>();
    generation.bytes=request.Read<std::uint64_t>();
    generation.modified_seconds=request.Read<std::int64_t>();
    generation.modified_nanoseconds=request.Read<std::int64_t>();
    generation.changed_seconds=request.Read<std::int64_t>();
    generation.changed_nanoseconds=request.Read<std::int64_t>();
    if (!(generation==inputs[i].generation()))
      throw StageError(ErrorCode::kMixedGeneration,"assignment publication source generation changed");
  }
  const auto parts=Catalog(request),ownership_parts=Catalog(request);
  std::vector<std::uint32_t> remap;
  for (const auto& part:ownership_parts) {
    const auto found=std::lower_bound(parts.begin(),parts.end(),part);
    Check(found!=parts.end() && *found==part,"assignment ownership references an unknown FPGA");
    remap.push_back(found-parts.begin());
  }
  PrimaryClusterView primary(inputs[1],inputs[0]);
  EmuirView source(inputs[0]);
  const auto clusters=request.Read<std::uint64_t>(),cells=request.Read<std::uint64_t>();
  Check(clusters==primary.clusters() && cells==primary.cells() && cells==source.counts().cells &&
        clusters<=UINT32_MAX && clusters<=request.remaining()/4,"assignment ownership dimensions disagree");
  std::vector<std::uint32_t> ownership(clusters);
  for (auto& part:ownership) {
    const auto index=request.Read<std::uint32_t>();
    Check(index<remap.size(),"assignment ownership references an unknown FPGA"); part=remap[index];
  }
  SqliteArtifactWriter writer(output,"assignment-index-v4",inputs);
  auto& database=writer.database();
  database.Execute("PRAGMA cache_size=-65536");
  database.Execute(kAssignmentCoreDDL);database.Execute(kAssignmentCutsDDL);
  {
    SqliteTransaction transaction(database);
    auto metadata=database.Prepare("INSERT INTO metadata(key,value) VALUES(?,?)");
    const std::set<std::string> required{
      "schema","design","platform","provider","seed","source_instance_count","constraints",
      "semantic_contract_sha256","semantic_contract_schema","semantic_contract_header","semantic_contract_binary"};
    const auto count=request.Read<std::uint32_t>();
    Check(count==required.size(),"assignment metadata coverage disagrees");
    std::set<std::string> observed;
    for (std::uint32_t i=0;i<count;++i) {
      const auto key=Identity(request);
      Check(required.count(key) && observed.insert(key).second,"assignment metadata key is invalid");
      Put(metadata,key,request.ReadString32(false,64*1024*1024));
    }
    {
      auto invalid=database.Prepare("SELECT count(*) FROM metadata WHERE NOT json_valid(value)");
      Check(invalid.Step() && invalid.TypedInteger(0)==0,"assignment metadata JSON is invalid");
      auto dimensions=database.Prepare("SELECT json_extract(value,'$') FROM metadata WHERE key='source_instance_count'");
      Check(dimensions.Step() && dimensions.TypedInteger(0)==static_cast<std::int64_t>(cells),
            "assignment source metadata coverage disagrees");
      auto schema=database.Prepare("SELECT json_extract(value,'$') FROM metadata WHERE key='schema'");
      Check(schema.Step() && schema.TypedText(0)=="emuflow.phase3-assignment-sqlite/v4","assignment logical schema disagrees");
    }
    auto partition=database.Prepare("INSERT INTO parts VALUES(?,?)");
    for (std::size_t i=0;i<parts.size();++i) {
      partition.BindInteger(1,i);partition.BindText(2,parts[i]);partition.Execute();partition.Reset();
    }
    {
      auto source_cells=source.store().database().Prepare("SELECT seq,id FROM cell_records ORDER BY seq");
      auto copies=database.Prepare("INSERT INTO copies VALUES(?,?,?,0)");
      std::uint64_t observed_cells=0;
      while (source_cells.Step()) {
        Check(observed_cells<cells && source_cells.TypedInteger(0)==static_cast<std::int64_t>(observed_cells),
              "assignment source instance sequence disagrees");
        const auto cluster=primary.Owner(observed_cells);
        Check(cluster<clusters,"assignment source cluster is invalid");
        const auto name=source_cells.TypedText(1); Check(!name.empty(),"assignment source identity is empty");
        copies.BindInteger(1,observed_cells++);copies.BindInteger(2,ownership[cluster]);copies.BindText(3,name);
        copies.Execute();copies.Reset();
      }
      Check(observed_cells==cells,"assignment source instance coverage is incomplete");
    }
    ownership.clear();ownership.shrink_to_fit();primary.ReleasePages();
    auto cuts=database.Prepare("INSERT INTO cuts VALUES(?,?,?,?,?,?,?,?)");
    auto sinks=database.Prepare("INSERT INTO cut_sinks VALUES(?,?,?)");
    auto predecessors=database.Prepare("INSERT INTO cut_predecessors VALUES(?,?,?)");
    const auto cut_count=request.Read<std::uint64_t>();
    Check(cut_count<=static_cast<std::uint64_t>(INT64_MAX) && cut_count<=request.remaining()/48,
          "assignment cut count is invalid");
    Sha256 digest; std::string previous;
    for (std::uint64_t i=0;i<cut_count;++i) {
      const auto net=Identity(request),cut_class=Identity(request);
      Check(i==0 || previous<net,"assignment cut order is not canonical");previous=net;
      const auto source_part=request.Read<std::uint32_t>(),sink_count=request.Read<std::uint32_t>();
      Check(source_part<parts.size() && sink_count && sink_count<parts.size() && sink_count<=request.remaining()/4,
            "assignment cut ownership is invalid");
      std::vector<JsonRecord> sink_names; std::uint32_t previous_sink=0;
      for (std::uint32_t j=0;j<sink_count;++j) {
        const auto sink=request.Read<std::uint32_t>();
        Check(sink<parts.size() && sink!=source_part && (j==0 || previous_sink<sink),
              "assignment cut sink order is invalid");previous_sink=sink;
        sinks.BindText(1,net);sinks.BindInteger(2,source_part);sinks.BindInteger(3,sink);sinks.Execute();sinks.Reset();
        sink_names.emplace_back(parts[sink]);
      }
      const auto endpoints=request.Read<std::uint64_t>();
      const auto round=request.Read<std::int64_t>(),level=request.Read<std::int64_t>(),depth=request.Read<std::int64_t>();
      const auto predecessor_count=request.Read<std::uint32_t>();
      Check(endpoints && endpoints<=INT64_MAX && round>=-1 && level>=-1 && depth>=-1 &&
            ((level<0)==(depth<0)) && (level>=0 || predecessor_count==0) &&
            predecessor_count<=request.remaining()/4,"assignment cut dependency fields are invalid");
      std::vector<JsonRecord> predecessor_names; std::string previous_predecessor;
      for (std::uint32_t j=0;j<predecessor_count;++j) {
        const auto predecessor=Identity(request);
        Check(predecessor!=net && (j==0 || previous_predecessor<predecessor),
              "assignment predecessor order is invalid");previous_predecessor=predecessor;
        predecessors.BindInteger(1,i);predecessors.BindInteger(2,j);predecessors.BindText(3,predecessor);
        predecessors.Execute();predecessors.Reset();predecessor_names.emplace_back(predecessor);
      }
      cuts.BindInteger(1,i);cuts.BindText(2,net);cuts.BindInteger(3,source_part);
      cuts.BindText(4,cut_class);cuts.BindInteger(5,endpoints);
      unsigned column=6;
      for (const auto value:{round,level,depth}) {
        if (value<0) cuts.BindNull(column);else cuts.BindInteger(column,value);++column;
      }
      cuts.Execute();cuts.Reset();
      std::map<std::string,JsonRecord> record{{"net",net},{"cut_class",cut_class},
        {"source_fpgas",std::vector<JsonRecord>{parts[source_part]}},
        {"sink_fpgas",std::move(sink_names)},{"sink_endpoints",endpoints}};
      if (round>=0) record.emplace("transport_round",round);
      if (level>=0) {
        record.emplace("dependency_level",level);record.emplace("combinational_dependency_depth",depth);
        record.emplace("predecessor_cut_nets",std::move(predecessor_names));
      }
      const auto canonical=JsonRecord(std::move(record)).Encode()+"\n";
      digest.update(reinterpret_cast<const unsigned char*>(canonical.data()),canonical.size());
    }
    request.RequireEnd();
    Put(metadata,"cut_records_schema",JsonQuote("emuflow.phase3-cut-records/v1"));
    Put(metadata,"cut_records",std::to_string(cut_count));
    Put(metadata,"cut_records_sha256",JsonQuote(Hex(digest.finish())));
    {
      auto invalid=database.Prepare(
        "SELECT count(*) FROM cut_predecessors q LEFT JOIN cuts p ON p.net_id=q.predecessor_net_id "
        "WHERE p.cut_index IS NULL");
      Check(invalid.Step() && invalid.TypedInteger(0)==0,"assignment predecessor coverage is incomplete");
      auto missing=database.Prepare(
        "SELECT count(*) FROM cuts WHERE dependency_level IS NULL AND "
        "(SELECT json_type(value) FROM metadata WHERE key='semantic_contract_sha256')!='null'");
      Check(missing.Step() && missing.TypedInteger(0)==0,"assignment exact dependencies are missing");
    }
    // Source/schema counts are independent of the host's per-instance view.
    // The host's existing typed relation validator remains a separate gate.
    transaction.Commit();
  }
  for (const auto& input:inputs) input.RequireUnchanged();
  writer.Commit();
}
}
