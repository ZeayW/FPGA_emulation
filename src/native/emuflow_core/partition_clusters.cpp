#include "emuflow_core/partition_clusters.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/emuir_view.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/sqlite.hpp"
#include <algorithm>
#include <array>
#include <limits>
#include <map>
#include <numeric>

namespace emuflow::core {
namespace {
void Check(bool condition, const char* message) {
  if (!condition) throw StageError(ErrorCode::kInvalidArtifact,message);
}
constexpr std::uint32_t missing=UINT32_MAX;
constexpr std::array<const char*,12> fields={
  "lut","ff","bram","dsp","carry","bram18k","uram288","dsp48","carry8","io","clock","other"};
struct UnionFind {
  std::vector<std::uint32_t> parent;
  std::vector<unsigned char> rank;
  explicit UnionFind(std::size_t size):parent(size),rank(size) {
    std::iota(parent.begin(),parent.end(),0);
  }
  std::uint32_t Find(std::uint32_t cell) {
    while (cell!=parent[cell]) { parent[cell]=parent[parent[cell]]; cell=parent[cell]; }
    return cell;
  }
  void Join(std::uint32_t a,std::uint32_t b) {
    a=Find(a); b=Find(b); if (a==b) return;
    if (rank[a]<rank[b]) std::swap(a,b);
    parent[b]=a; if (rank[a]==rank[b]) ++rank[a];
  }
};
}
void BuildPartitionClusters(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  Check(inputs.size()==2,"primary cluster source coverage is invalid");
  for (const auto& input:inputs) input.RequireUnchanged();
  EmuirView ir(inputs[0]);
  Check(ir.counts().cells<=UINT32_MAX && ir.counts().nets<=UINT32_MAX,
        "primary cluster source exceeds uint32 identity domain");
  const auto count=static_cast<std::uint32_t>(ir.counts().cells);
  BinaryReader request(inputs[1].path());
  ReadAndValidateHeader(request,{0x31515243464d5545ULL,1,"partition-cluster-request-v1"});
  const FileGeneration generation={request.Read<std::uint64_t>(),request.Read<std::uint64_t>(),request.Read<std::uint64_t>(),
    request.Read<std::int64_t>(),request.Read<std::int64_t>(),request.Read<std::int64_t>(),request.Read<std::int64_t>()};
  if (!(generation==inputs[0].generation()))
    throw StageError(ErrorCode::kMixedGeneration,"primary cluster EmuIR generation changed");
  Check(request.Read<std::uint64_t>()==count && request.Read<std::uint64_t>()==ir.counts().nets,
        "primary cluster source counts disagree");
  const auto bytes=request.Read<std::uint64_t>();
  Check(bytes==(ir.counts().nets+7)/8 && bytes<=request.remaining(),"invalid primary cluster eligible bitmap");
  std::vector<unsigned char> released(bytes); request.ReadBytes(released.data(),released.size());
  if (ir.counts().nets%8 && bytes)
    Check((released.back()>>(ir.counts().nets%8))==0,"invalid primary cluster eligible padding");
  SqliteDatabase source(inputs[0].path(),SqliteDatabase::Mode::kImmutableReadOnly);
  auto logical=source.Prepare("SELECT value FROM metadata WHERE key='logical_schema'");
  Check(logical.Step() && logical.Text(0)=="emuflow.emuir/v1","invalid primary cluster logical schema");
  std::vector<std::uint32_t> seq_to_ordinal(count,missing);
  auto identities=source.Prepare("SELECT seq,id FROM cell_records ORDER BY id");
  std::uint64_t observed=0; std::string previous;
  while (identities.Step()) {
    const auto seq=identities.TypedInteger(0); auto identity=identities.TypedText(1);
    Check(observed<count && seq>=0 && static_cast<std::uint64_t>(seq)<count && seq_to_ordinal[seq]==missing &&
          !identity.empty() && (observed==0 || previous<identity),"invalid primary cluster cell identity/order");
    seq_to_ordinal[seq]=observed++; previous=std::move(identity);
  }
  Check(observed==count,"incomplete primary cluster cell sequence");
  UnionFind union_find(count);
  const auto groups=request.Read<std::uint64_t>();
  Check(groups<=request.remaining()/8,"invalid primary cluster group count");
  for (std::uint64_t group=0;group<groups;++group) {
    const auto members=request.Read<std::uint64_t>();
    Check(members && members<=count && members<=request.remaining()/4,"invalid primary cluster group membership");
    std::uint32_t first=missing;
    for (std::uint64_t i=0;i<members;++i) {
      const auto seq=request.Read<std::uint32_t>(); Check(seq<count,"unknown primary cluster group cell");
      if (i==0) first=seq_to_ordinal[seq]; else union_find.Join(first,seq_to_ordinal[seq]);
    }
  }
  request.RequireEnd();
  auto nets=source.Prepare("SELECT seq,cut_class FROM net_records ORDER BY seq");
  bool present=nets.Step(); std::uint64_t net_seq=0;
  auto join_net=[&]() {
    Check(present && net_seq<ir.counts().nets && nets.TypedInteger(0)==static_cast<std::int64_t>(net_seq),"invalid primary cluster net order");
    const auto cut=nets.TypedText(1);
    const bool legal=cut=="register_output" || cut=="register_input" || cut=="primary_input" || cut=="clock" || cut=="reset";
    return !legal && !(released[net_seq>>3]&(1U<<(net_seq&7)));
  };
  bool join=present ? join_net():false; std::uint32_t first=missing;
  // Stream and immediately union endpoints; even a high-fanout net needs no
  // temporary membership set/vector. Source validation remains independent.
  ir.VisitConnectivity([&](const EmuirEndpoint& endpoint) {
    Check(endpoint.net.sequence()==net_seq,"primary cluster endpoint/net mismatch");
    if (join && endpoint.cell) {
      const auto ordinal=seq_to_ordinal[endpoint.cell->sequence()];
      if (first==missing) first=ordinal; else union_find.Join(first,ordinal);
    }
  },[&](EmuirNetId net) {
    Check(net.sequence()==net_seq && present,"primary cluster net coverage disagrees");
    ++net_seq; present=nets.Step(); first=missing; join=present ? join_net():false;
  });
  Check(!present && net_seq==ir.counts().nets,"incomplete primary cluster net sequence");
  released.clear(); released.shrink_to_fit();
  std::vector<std::uint32_t> root_to_cluster(count,missing),source_owner(count),lexical_owner(count),member_counts;
  std::vector<std::uint64_t> name_offsets{0}; std::string names;
  std::array<std::vector<std::uint32_t>,12> resources;
  // Bound the resource-payload cache independently of design size. Repeated
  // interned resources avoid JSON parsing without a second graph-sized map.
  std::int64_t cached_payload=-1; std::array<std::uint32_t,12> amounts{};
  std::map<std::int64_t,std::array<std::uint32_t,12>> resource_cache;
  auto payload=source.Prepare("SELECT value,json_type(value) FROM payloads WHERE seq=?");
  auto resource=source.Prepare("SELECT key,value,type FROM json_each(?)");
  auto cells=source.Prepare("SELECT seq,id,resources_seq FROM cell_records ORDER BY id");
  observed=0;
  while (cells.Step()) {
    const auto seq=cells.TypedInteger(0); const auto name=cells.TypedText(1);
    Check(observed<count && seq>=0 && static_cast<std::uint64_t>(seq)<count && seq_to_ordinal[seq]==observed,
          "primary cluster resource cell sequence disagrees");
    const auto root=union_find.Find(observed); auto cluster=root_to_cluster[root];
    if (cluster==missing) {
      cluster=member_counts.size(); root_to_cluster[root]=cluster; member_counts.push_back(0);
      for (auto& column:resources) column.push_back(0);
    }
    lexical_owner[observed++]=cluster; source_owner[seq]=cluster; ++member_counts[cluster];
    names+=name; name_offsets.push_back(names.size());
    const auto resource_seq=cells.TypedInteger(2); Check(resource_seq>=0,"invalid primary cluster resource reference");
    const auto cached=resource_cache.find(resource_seq);
    if (cached!=resource_cache.end()) { amounts=cached->second; cached_payload=resource_seq; }
    if (resource_seq!=cached_payload) {
      payload.BindInteger(1,resource_seq);
      Check(payload.Step() && payload.Text(1)=="object","missing or invalid primary cluster resources");
      const auto json=payload.TypedText(0); payload.Reset();
      resource.BindText(1,json); amounts.fill(0); std::array<bool,12> seen{};
      while (resource.Step()) {
        const auto key=resource.TypedText(0),type=resource.Text(2);
        const auto field=std::find_if(fields.begin(),fields.end(),[&](auto name) { return key==name; });
        Check(field!=fields.end(),"unknown primary cluster resource");
        const auto index=field-fields.begin(); const auto amount=resource.Integer(1);
        Check(!seen[index] && type=="integer" && amount>=0 && static_cast<std::uint64_t>(amount)<=UINT32_MAX,
              "invalid primary cluster resource amount");
        seen[index]=true; amounts[index]=amount;
      }
      resource.Reset(); cached_payload=resource_seq;
      if (resource_cache.size()==4096) resource_cache.clear();
      resource_cache.emplace(resource_seq,amounts);
    }
    for (std::size_t field=0;field<fields.size();++field) {
      const auto total=static_cast<std::uint64_t>(resources[field][cluster])+amounts[field];
      Check(total<UINT64_C(4294967296),"primary cluster resource exceeds uint32");
      resources[field][cluster]=total;
    }
  }
  Check(observed==count,"incomplete primary cluster resource sequence");
  std::vector<std::uint32_t> offsets{0};
  for (auto size:member_counts) offsets.push_back(offsets.back()+size);
  auto next=offsets; std::vector<std::uint32_t> members(count);
  for (std::uint32_t ordinal=0;ordinal<count;++ordinal) members[next[lexical_owner[ordinal]]++]=ordinal;
  AtomicBinaryWriter writer(output); std::uint32_t crc=0;
  auto put=[&](const void* data,std::size_t size) {
    writer.WriteBytes(data,size); UpdateCrc32(crc,static_cast<const unsigned char*>(data),size);
  };
  auto scalar=[&](auto value) { put(&value,sizeof value); };
  auto column=[&](const auto& values) { put(values.data(),values.size()*sizeof(values[0])); };
  scalar(UINT64_C(0x314c4350464d5545)); scalar(std::uint32_t{1}); scalar(std::uint64_t{count});
  scalar(std::uint64_t{member_counts.size()}); scalar(std::uint64_t{names.size()});
  column(name_offsets); column(offsets); column(members); column(source_owner);
  for (const auto& values:resources) column(values);
  put(names.data(),names.size()); writer.Write(crc);
  for (const auto& input:inputs) input.RequireUnchanged();
  writer.Commit();
}
}  // namespace emuflow::core
