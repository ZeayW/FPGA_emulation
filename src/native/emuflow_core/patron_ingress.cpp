#include "emuflow_core/store.hpp"
#include "emuflow_core/patron_ingress.hpp"
#include "emuflow_core/primary_cluster_view.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/emuir_view.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/sqlite.hpp"
#include <algorithm>
#include <array>
#include <cmath>
#include <functional>
#include <map>
#include <numeric>
#include <set>
#include <filesystem>
#include <unistd.h>

namespace emuflow::core {
namespace {
void Check(bool valid, const char* message) {
  if (!valid) throw StageError(ErrorCode::kInvalidArtifact,message);
}
class Reader {
 public:
  explicit Reader(const std::string& path) : input(path) {}
  template<class T> T get() { T value{}; bytes(&value,sizeof value); return value; }
  void bytes(void* value, std::size_t size) {
    input.ReadBytes(value,size); UpdateCrc32(crc,static_cast<const unsigned char*>(value),size);
  }
  template<class T> std::vector<T> vector(std::size_t count) {
    Check(count <= input.remaining()/sizeof(T),"PATRON columns are truncated");
    std::vector<T> result(count); bytes(result.data(),count*sizeof(T)); return result;
  }
  double number() { auto value=get<double>(); Check(std::isfinite(value),"non-finite PATRON request"); return value; }
  std::string text() {
    auto count=get<std::uint32_t>(); Check(count && count<=input.remaining(),"invalid PATRON catalog string");
    std::string result(count,'\0'); bytes(result.data(),count);
    Check(result.find('\0')==std::string::npos,"NUL in PATRON catalog"); return result;
  }
  void finish() { Check(input.Read<std::uint32_t>()==crc,"PATRON source checksum disagrees"); input.RequireEnd(); }
  BinaryReader input;
  std::uint32_t crc=0;
};
std::vector<std::string> Catalog(Reader& reader) {
  const auto count=reader.get<std::uint32_t>();
  Check(count && count<=65536 && count<=reader.input.remaining()/4,"invalid PATRON catalog count");
  std::vector<std::string> values; std::set<std::string> unique;
  for (std::uint32_t i=0;i<count;++i) {
    auto value=reader.text(); Check(unique.insert(value).second,"duplicate PATRON catalog identity");
    values.push_back(std::move(value));
  }
  return values;
}
FileGeneration Generation(Reader& reader) {
  return {reader.get<std::uint64_t>(),reader.get<std::uint64_t>(),reader.get<std::uint64_t>(),
          reader.get<std::int64_t>(),reader.get<std::int64_t>(),reader.get<std::int64_t>(),reader.get<std::int64_t>()};
}
void SealOrder(const std::string& output, const std::vector<std::uint32_t>& order) {
  AtomicBinaryWriter writer(output); std::uint32_t crc=0;
  auto put=[&](const void* bytes, std::size_t size) {
    writer.WriteBytes(bytes,size); UpdateCrc32(crc,static_cast<const unsigned char*>(bytes),size);
  };
  const std::uint64_t magic=0x3152444f54415045ULL, count=order.size(); const std::uint32_t version=1;
  put(&magic,sizeof magic); put(&version,sizeof version); put(&count,sizeof count);
  put(order.data(),order.size()*sizeof(std::uint32_t)); writer.Write(crc); writer.Commit();
}

}

std::vector<std::uint32_t> CanonicalPatronClusterOrder(std::uint32_t count) {
  std::vector<std::uint32_t> order; order.reserve(count);
  // c000000..c999999 are six-digit roots. Longer canonical identities are
  // descendants of the non-zero-leading roots, not a separate numeric block.
  std::function<void(std::uint64_t)> append=[&](std::uint64_t value) {
    if (value>=count) return;
    order.push_back(static_cast<std::uint32_t>(value));
    for (unsigned digit=0;digit<10 && value*10+digit<count;++digit) append(value*10+digit);
  };
  for (std::uint32_t root=0;root<std::min(count,1000000U);++root) {
    order.push_back(root);
    if (root>=100000) for (unsigned digit=0;digit<10 && static_cast<std::uint64_t>(root)*10+digit<count;++digit)
      append(static_cast<std::uint64_t>(root)*10+digit);
  }
  Check(order.size()==count,"PATRON canonical cluster order is incomplete"); return order;
}

patron::Model BuildPatronModel(const std::vector<ArtifactRef>& inputs, PatronBuildContext& context, bool read_paths) {
  Check(inputs.size()==(read_paths?5U:4U),"PATRON primary store input coverage is invalid");
  for (const auto& input:inputs) input.RequireUnchanged();
  Reader request(inputs.back().path());
  Check(request.input.size()<=1024*1024 && request.get<std::uint64_t>()==(read_paths?0x3151455254415045ULL:0x3151505354415045ULL) &&
        request.get<std::uint32_t>()==(read_paths?2U:1U),"unsupported PATRON board request");
  for (unsigned i=0;i<inputs.size()-1;++i)
    if (!(Generation(request)==inputs[i].generation()))
      throw StageError(ErrorCode::kMixedGeneration,"PATRON source generation changed");
  const auto parts=Catalog(request), initial_parts=Catalog(request);
  Check(std::is_sorted(parts.begin(),parts.end()),"PATRON part catalog is not ordered");
  const auto transported=Catalog(request);
  const std::set<std::string> classes={"clock","reset","primary_input","register_output","register_input","combinational","multi_driver","undriven"};
  for (const auto& value:transported) Check(classes.count(value),"unknown PATRON cut class");
  const auto expected_paths=read_paths?request.get<std::uint64_t>():0U;
  patron::Model model; model.parts=parts.size();
  model.max_hops=request.get<std::int32_t>(); model.max_ratio=request.get<std::int32_t>();
  model.ratio_quantum=request.get<std::int32_t>(); model.min_used_parts=request.get<std::int32_t>();
  model.max_moves=request.get<std::int32_t>(); model.max_sweeps=request.get<std::int32_t>();
  model.ejection_critical_limit=request.get<std::int32_t>(); model.ejection_donor_limit=request.get<std::int32_t>();
  model.max_ejections=request.get<std::int32_t>(); model.min_ratio=request.get<std::int32_t>();
  model.positive_scale=request.number(); model.negative_scale=request.number(); model.max_period=request.number();
  model.boundary_fanout_penalty_scale_ns=request.number(); const double reserved_hop_guard_scale=request.number();
  model.flow_corridor_distance=request.get<std::int32_t>(); model.flow_piercing_strategy=request.get<std::int32_t>();
  model.flow_max_legal_candidates=request.get<std::int32_t>(); model.flow_max_polish_moves=request.get<std::int32_t>();
  model.flow_max_frontier_paths=request.get<std::int32_t>(); model.flow_max_tail_moves=request.get<std::int32_t>();
  Check(model.max_hops>=-1 && model.max_ratio>0 && model.ratio_quantum>0 && model.min_ratio>0 &&
        model.min_used_parts>0 && model.min_used_parts<=model.parts && model.max_moves>=0 && model.max_sweeps>0 &&
        model.ejection_critical_limit>=0 && model.ejection_donor_limit>=0 && model.max_ejections>=0 &&
        model.positive_scale>0 && model.negative_scale>0 && model.max_period>0 && model.boundary_fanout_penalty_scale_ns>=0 &&
        reserved_hop_guard_scale==0 && model.flow_corridor_distance>=0 && model.flow_piercing_strategy>=0 &&
        model.flow_piercing_strategy<=3 && model.flow_max_legal_candidates>0 && model.flow_max_polish_moves>=0 &&
        model.flow_max_frontier_paths>0 && model.flow_max_tail_moves>=0,"invalid PATRON model configuration");
  std::array<bool,13> balanced{}; std::array<double,13> tolerance{};
  for (unsigned field=0;field<13;++field) {
    const auto flag=request.get<std::uint8_t>(); tolerance[field]=request.number(); balanced[field]=flag;
    Check(flag<=1 && tolerance[field]>=0,"invalid PATRON balance policy");
  }
  std::vector<std::array<std::uint64_t,13>> physical(model.parts),planning(model.parts);
  for (int part=0;part<model.parts;++part) for (unsigned field=0;field<13;++field) {
    physical[part][field]=request.get<std::uint64_t>(); planning[part][field]=request.get<std::uint64_t>();
    Check(planning[part][field]<=physical[part][field],"invalid PATRON reserved capacity");
  }
  const auto domains=request.get<std::uint32_t>(); Check(domains && domains<=65536,"invalid PATRON domain count");
  model.domains=domains; model.domain.resize(domains);
  for (auto& domain:model.domain) {
    domain.lanes=request.get<std::int32_t>(); const auto sll=request.get<std::uint8_t>(); domain.is_sll=sll;
    domain.ratio_delay_slope_ns=request.number(); domain.opposite=request.get<std::int32_t>();
    domain.banks=request.get<std::int32_t>(); domain.wires=request.get<std::int32_t>(); domain.capacity_bits=request.get<std::int64_t>();
    Check(domain.lanes>0 && sll<=1 && domain.ratio_delay_slope_ns>=0 && domain.banks>=0 && domain.wires>=0 &&
          ((domain.banks==0)==(domain.wires==0)) && domain.capacity_bits>0,"invalid PATRON domain");
  }
  for (int i=0;i<model.domains;++i) if (model.domain[i].banks) {
    const auto& domain=model.domain[i]; Check(domain.opposite>=0 && domain.opposite<model.domains && domain.opposite!=i,"invalid PATRON opposite domain");
    const auto& other=model.domain[domain.opposite];
    Check(other.opposite==i && other.banks==domain.banks && other.wires==domain.wires,"asymmetric PATRON banks");
  }
  Check(static_cast<std::uint64_t>(model.parts)*model.parts<=request.input.remaining()/4,"PATRON route matrix is truncated");
  model.route.assign(model.parts,std::vector<patron::Route>(model.parts));
  for (auto& row:model.route) for (auto& route:row) {
    const auto count=request.get<std::int32_t>(); Check(count>=-1 && count<=65536,"invalid PATRON route"); route.reachable=count>=0;
    for (int i=0;i<count;++i) {
      const auto domain=request.get<std::uint32_t>(); const auto delay=request.number();
      Check(domain<domains && delay>=0,"invalid PATRON route arc"); route.arcs.push_back({static_cast<int>(domain),delay});
    }
  }
  request.finish();
  EmuirView ir(inputs[0]);
  Reader columns(inputs[read_paths?3:2].path());
  PrimaryClusterView primary_view(inputs[read_paths?2:1],inputs[0]);
  Check(columns.get<std::uint64_t>()==0x315450494e415045ULL && columns.get<std::uint32_t>()==1,"unsupported PATRON initial columns");
  const auto clusters=columns.get<std::uint64_t>(), cells=columns.get<std::uint64_t>();
  Check(clusters==primary_view.clusters() && cells==primary_view.cells(),"PATRON primary dimensions disagree");
  Check(clusters && clusters<=INT32_MAX && cells==ir.counts().cells && clusters<=cells && ir.counts().nets<=INT32_MAX,
        "PATRON source dimensions disagree");
  model.clusters=clusters; model.flow_max_clusters=clusters;
  Check(model.flow_corridor_distance<=model.clusters,"PATRON corridor exceeds cluster count");
  const auto totals=primary_view.totals();
  auto initial=columns.vector<std::uint32_t>(clusters); std::vector<int> fixed(clusters,-1);
  const auto fixes=columns.get<std::uint64_t>(); Check(fixes<=clusters,"invalid PATRON fixed count");
  for (std::uint64_t i=0;i<fixes;++i) {
    auto cluster=columns.get<std::uint32_t>(), part=columns.get<std::uint32_t>();
    Check(cluster<clusters && part<parts.size() && fixed[cluster]<0,"invalid PATRON fixed ownership"); fixed[cluster]=part;
  }
  columns.finish();
  context.native_to_artifact=CanonicalPatronClusterOrder(clusters);
  std::vector<std::uint32_t> native(clusters);
  for (std::size_t c=0;c<clusters;++c) native[context.native_to_artifact[c]]=c;
  std::vector<unsigned> dimensions{0};
  for (unsigned field=1;field<13;++field) if (totals[field] && std::all_of(physical.begin(),physical.end(),[&](const auto& cap) { return cap[field]>0; })) dimensions.push_back(field);
  model.dimensions=dimensions.size(); model.cluster.resize(clusters); model.cluster_weights.resize(clusters*dimensions.size());
  std::vector<int> initial_map(initial_parts.size(),-1);
  for (std::size_t i=0;i<initial_parts.size();++i) {
    const auto found=std::lower_bound(parts.begin(),parts.end(),initial_parts[i]);
    if (found!=parts.end() && *found==initial_parts[i]) initial_map[i]=found-parts.begin();
  }
  std::vector<std::vector<std::uint64_t>> loads(model.parts,std::vector<std::uint64_t>(dimensions.size()));
  for (std::size_t c=0;c<clusters;++c) {
    const auto artifact=context.native_to_artifact[c];
    Check(initial[artifact]<initial_map.size() && initial_map[initial[artifact]]>=0,"unknown PATRON initial FPGA");
    const int part=initial_map[initial[artifact]];
    Check(fixed[artifact]<0 || fixed[artifact]==part,"PATRON initial fixed owner disagrees");
    model.cluster[c]={part,fixed[artifact]};
    for (std::size_t d=0;d<dimensions.size();++d) {
      const auto weight=primary_view.Weight(artifact,dimensions[d]); model.cluster_weights[c*dimensions.size()+d]=weight; loads[part][d]+=weight;
    }
  }
  model.hard_capacity.assign(model.parts,std::vector<double>(dimensions.size())); model.balance_capacity=model.hard_capacity;
  for (std::size_t d=0;d<dimensions.size();++d) {
    const auto field=dimensions[d]; double capacity_total=0;
    for (const auto& part:physical) capacity_total+=part[field];
    for (int part=0;part<model.parts;++part) {
      const double hard=field ? planning[part][field] : totals[0]; double allowed=hard;
      if (balanced[field]) {
        const double share=field ? physical[part][field]/capacity_total : 1.0/model.parts;
        volatile double fraction=static_cast<double>(totals[field])*share;
        volatile double scaled=fraction*(1.0+tolerance[field]);
        allowed=std::floor(scaled+1e-9); if (field) allowed=std::min(allowed,hard);
      }
      Check(loads[part][d]<=allowed,"PATRON initial assignment violates strict balance/capacity");
      model.hard_capacity[part][d]=hard; model.balance_capacity[part][d]=allowed;
    }
  }
  if(!read_paths) {
    context.source_cell_cluster.resize(cells);
    for(std::uint64_t cell=0;cell<cells;++cell)context.source_cell_cluster[cell]=native.at(primary_view.Owner(cell));
  }
  primary_view.ReleasePages();
  const auto owner_of=[&](std::uint64_t cell) { return primary_view.Owner(cell); };
  initial.clear(); initial.shrink_to_fit(); fixed.clear();
  auto& source=ir.store().database();
  auto schema=source.Prepare("SELECT value FROM metadata WHERE key='logical_schema'");
  Check(schema.Step() && schema.Text(0)=="emuflow.emuir/v1","PATRON EmuIR logical schema disagrees");
  auto count_shape=source.Prepare("SELECT (SELECT COUNT(*) FROM json_each(m.value)) FROM metadata m WHERE key='record_counts_json'");
  Check(count_shape.Step() && count_shape.Integer(0)==3,"PATRON source count shape disagrees");
  auto cell_rows=source.Prepare("SELECT seq,id FROM cell_records ORDER BY seq"); std::uint64_t count_cells=0;
  while (cell_rows.Step()) Check(cell_rows.TypedInteger(0)==static_cast<std::int64_t>(count_cells++) && !cell_rows.TypedText(1).empty(),"PATRON cell sequence is invalid");
  Check(count_cells==cells,"PATRON cell coverage disagrees");
  std::vector<int> pressure(ir.counts().nets,-1); auto metadata=source.Prepare("SELECT seq,cut_class FROM net_records ORDER BY seq");
  bool has_meta=metadata.Step();
  std::size_t member_begin=0, sink_begin=std::numeric_limits<std::size_t>::max();
  auto selected=[&] { return std::find(transported.begin(),transported.end(),metadata.Text(1))!=transported.end(); };
  ir.VisitConnectivity([&](const EmuirEndpoint& endpoint) {
    Check(has_meta && metadata.Integer(0)==static_cast<std::int64_t>(endpoint.net.sequence()),"PATRON net metadata disagrees");
    if (selected() && endpoint.cell) {
      const auto cluster=native[owner_of(endpoint.cell->sequence())];
      if (endpoint.role==EndpointRole::kSink && sink_begin==std::numeric_limits<std::size_t>::max())
        sink_begin=model.net_members.size();
      model.net_members.push_back(cluster);
    }
  },[&](EmuirNetId net) {
    Check(has_meta && metadata.TypedInteger(0)==static_cast<std::int64_t>(net.sequence()) && classes.count(metadata.TypedText(1)),"PATRON net metadata is invalid");
    const auto middle=sink_begin==std::numeric_limits<std::size_t>::max() ? model.net_members.size() : sink_begin;
    if (middle>member_begin && middle<model.net_members.size()) {
      auto begin=model.net_members.begin()+member_begin, sink=model.net_members.begin()+middle, end=model.net_members.end();
      std::sort(begin,sink); const auto drivers=std::unique(begin,sink);
      std::sort(sink,end); const auto sinks=std::unique(sink,end);
      const auto compact_middle=static_cast<std::size_t>(drivers-model.net_members.begin());
      const auto compact_end=compact_middle+static_cast<std::size_t>(sinks-sink);
      std::move(sink,sinks,drivers); model.net_members.resize(compact_end);
      patron::Net record; record.drivers.set_offsets(member_begin,compact_middle);
      record.sinks.set_offsets(compact_middle,compact_end);
      pressure[net.sequence()]=model.net.size(); model.net.push_back(record);
    } else model.net_members.resize(member_begin);
    member_begin=model.net_members.size(); sink_begin=std::numeric_limits<std::size_t>::max(); has_meta=metadata.Step();
  },true);
  Check(!has_meta,"PATRON orphan net metadata");
  std::vector<patron::Net> ordered; ordered.reserve(model.net.size());
  auto net_names=source.Prepare("SELECT seq,id FROM net_records ORDER BY id"); std::string previous; std::uint64_t seen_nets=0;
  while (net_names.Step()) {
    const auto seq=net_names.TypedInteger(0); const auto name=net_names.TypedText(1);
    Check(seq>=0 && static_cast<std::uint64_t>(seq)<pressure.size() && !name.empty() && (seen_nets==0 || name>previous),"PATRON net identity is invalid");
    previous=name; ++seen_nets;
    if (pressure[seq]>=0) { ordered.push_back(model.net[pressure[seq]]); pressure[seq]=ordered.size()-1; }
  }
  Check(seen_nets==ir.counts().nets,"PATRON net identity count disagrees");
  model.net=std::move(ordered); model.nets=model.net.size();
  Check(model.nets>0,"PATRON pressure graph has no transport nets"); patron::BindMembers(model);
  if(!read_paths) {
    context.source_net_native=std::move(pressure);
    for(const auto& input:inputs)input.RequireUnchanged();
    return model;
  }
  SqliteStore timing_store(inputs[1]);
  auto& timing=timing_store.database();
  auto attach=timing.Prepare("ATTACH DATABASE ? AS emuir"); attach.BindText(1,SqliteImmutableUri(inputs[0].path())); attach.Execute();
  auto paths=timing.Prepare("SELECT p.ordinal,p.path_id,p.clock_period_ns,p.slack_ns,l.seq,r.seq,p.clock_domain,"
      "typeof(p.clock_period_ns) IN ('integer','real') AND typeof(p.slack_ns) IN ('integer','real') "
      "FROM paths p LEFT JOIN emuir.cell_records l ON l.id=p.start_instance LEFT JOIN emuir.cell_records r ON r.id=p.end_instance ORDER BY p.ordinal");
  auto path_nets=timing.Prepare("SELECT p.path_ordinal,p.net_ordinal,n.seq FROM path_nets p LEFT JOIN emuir.net_records n ON n.id=p.net_id ORDER BY p.path_ordinal,p.net_ordinal");
  bool have_net=path_nets.Step();
  while (paths.Step()) {
    const auto ordinal=model.path.size();
    Check(paths.TypedInteger(0)==static_cast<std::int64_t>(ordinal) && !paths.TypedText(1).empty(),"PATRON path identity is invalid");
    (void)paths.TypedText(6);
    patron::Path path; path.period_ns=paths.Real(2); path.slack_ns=paths.Real(3);
    Check(paths.Integer(7) && std::isfinite(path.period_ns) && path.period_ns>0 && std::isfinite(path.slack_ns),"PATRON path timing is invalid");
    const bool endpoints=!paths.IsNull(4) && !paths.IsNull(5); bool single_driver=true;
    if (endpoints) {
      const auto l=paths.TypedInteger(4), r=paths.TypedInteger(5);
      Check(l>=0 && r>=0 && static_cast<std::uint64_t>(l)<cells && static_cast<std::uint64_t>(r)<cells,"PATRON path endpoint is invalid");
      path.start_cluster=native[owner_of(l)]; path.end_cluster=native[owner_of(r)];
    }
    const auto begin=model.path_nets.size(); std::uint64_t net_position=0;
    while (have_net && path_nets.TypedInteger(0)==static_cast<std::int64_t>(ordinal)) {
      Check(path_nets.TypedInteger(1)==static_cast<std::int64_t>(net_position++) && !path_nets.IsNull(2),"PATRON path net order/identity is invalid");
      const auto source_net=path_nets.TypedInteger(2);
      Check(source_net>=0 && static_cast<std::uint64_t>(source_net)<pressure.size(),"PATRON path references unknown net");
      if (pressure[source_net]>=0) { const auto net=pressure[source_net]; model.path_nets.push_back(net); single_driver=single_driver && model.net[net].drivers.size()==1; }
      have_net=path_nets.Step();
    }
    Check(net_position>0,"PATRON path has no nets");
    if (!single_driver) path.start_cluster=path.end_cluster=-1;
    path.nets.set_offsets(begin,model.path_nets.size()); model.path.push_back(path);
  }
  Check(!have_net && !model.path.empty() && model.path.size()==expected_paths && model.path.size()<=INT32_MAX,"PATRON path coverage disagrees");
  model.paths=model.path.size(); patron::BindMembers(model);
  for (const auto& input:inputs) input.RequireUnchanged();
  return model;
}

void RefinePatronFromPrimary(const std::vector<ArtifactRef>& inputs, const std::string& output) {
  StagedArtifact staged(output,{".order",".summary.json"});
  PatronBuildContext context; patron::Model model;
  const auto build=RunStage("phase3.model-build",[&]{return inputs;},[&](const auto& refs){model=BuildPatronModel(refs,context);});
  if (!build.ok()) throw StageError(build.error,"PATRON model build: "+build.message);
  const auto search=RunStage("phase3.patron-search",[&]{return inputs;},[&](const auto&){
    emuflow::native::RefinePatronModel(model,staged.path(),true,true,inputs);
  });
  if (!search.ok()) throw StageError(search.error,"PATRON search: "+search.message);
  for (const auto& input:inputs) input.RequireUnchanged();
  SealOrder(staged.path()+".order",context.native_to_artifact);
  auto summary=JsonRecord(std::map<std::string,JsonRecord>{{"schema","emuflow.patron-native-model/v1"},
      {"implementation_id","native-primary-store-model-v2"},{"clusters",model.clusters},{"nets",model.nets},
      {"paths",model.paths},{"dimensions",model.dimensions},{"capacity_domains",model.domains}}).Encode();
  summary.pop_back();
  summary+=",\"model_build\":"+StageResultJson(build)+",\"search\":"+StageResultJson(search)+"}";
  AtomicBinaryWriter writer(staged.path()+".summary.json"); writer.WriteBytes(summary.data(),summary.size()); writer.Commit();
  for (const auto& input:inputs) input.RequireUnchanged();
  PublishTemporaryFile(staged.path()+".order",output+".order",inputs);
  PublishTemporaryFile(staged.path(),output,inputs);
  PublishTemporaryFile(staged.path()+".summary.json",output+".summary.json",inputs);
}
}  // namespace emuflow::core
