#include "partition/partition_semantics.hpp"
#include "common/artifact_io.hpp"
#include "partition/candidate_ids.hpp"
#include "ir/emuir_view.hpp"
#include "partition/exact_captures.hpp"
#include "common/hash.hpp"
#include "common/json_record.hpp"
#include "flow/native_stages.hpp"
#include "common/ordered_record_table.hpp"
#include "partition/primary_cluster_view.hpp"
#include <algorithm>
#include <cmath>
#include <filesystem>
#include <map>
#include <optional>
#include <set>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs=std::filesystem;
using U=std::uint64_t;
using Object=std::map<std::string,JsonRecord>;
void Check(bool valid,const std::string& text) {
  if (!valid) throw StageError(ErrorCode::kInvalidArtifact,text);
}
struct Workspace {
  fs::path root;
  explicit Workspace(const fs::path& parent) {
    auto path=(parent/".partition-semantics-XXXXXX").string();
    Check(mkdtemp(path.data())!=nullptr,"cannot create semantic workspace");root=path;
  }
  ~Workspace() { std::error_code error;fs::remove_all(root,error); }
};
void Text(AtomicBinaryWriter& writer,const std::string& text) {
  Check(text.size()<UINT32_MAX,"semantic identity exceeds protocol bounds");
  writer.Write(std::uint32_t(text.size()));writer.WriteBytes(text.data(),text.size());
}
struct Config { unsigned kind=0;U depth=0;std::vector<std::string> parts; };
Config ReadConfig(const std::vector<ArtifactRef>& inputs) {
  Check(inputs.size()>=4 && inputs.size()<=6,"semantic source coverage disagrees");
  Check(inputs[0].schema()=="emuir-compact-v2" && inputs[1].schema()=="partition-cluster-columns-v2" &&
        inputs[2].schema()=="candidate-policy-request-v1" && inputs[3].schema()=="partition-finalization-request-v1",
        "semantic source schemas disagree");
  CheckedRequestReader r(inputs[3].path(),4*1024*1024);
  Check(r.Read<U>()==0x31514546464d5545ULL && r.Read<std::uint32_t>()==1,"unsupported semantic request");
  Config c;c.kind=r.Read<std::uint32_t>();
  Check(c.kind<=2 && inputs.size()==4+(c.kind==1?1U:c.kind==2?2U:0U),"semantic owner kind disagrees");
  if(c.kind)Check(inputs[4].schema()==(c.kind==1?"patron-initial-columns-v1":"patron-output-v1"),"semantic ownership schema disagrees");
  if(c.kind==2)Check(inputs[5].schema()=="patron-cluster-order-v1","semantic coordinate schema disagrees");
  for(unsigned i=0;i<inputs.size();++i)if(i!=3)
    if(!(ReadFileGeneration(r)==inputs[i].generation()))
      throw StageError(ErrorCode::kMixedGeneration,"semantic original source generation changed");
  auto parts=r.Read<std::uint32_t>();Check(parts && parts<=65536,"invalid semantic part count");
  std::set<std::string> unique;
  for(unsigned i=0;i<parts;++i) {
    auto part=r.ReadString32();JsonQuote(part);
    Check(part.find('\0')==std::string::npos && unique.insert(part).second,"invalid semantic part identity");
    c.parts.push_back(std::move(part));
  }
  c.depth=r.Read<U>();r.RequireEnd();
  Check(c.kind || c.parts.size()==1,"constant ownership requires exactly one part");return c;
}
using Ownership=SelectedPartitionOwnership;
Ownership ReadOwnership(const std::vector<ArtifactRef>& owners,const std::vector<std::string>& parts,const PrimaryClusterView& primary) {
  Config c;c.parts=parts;c.kind=owners.size()==2?2:owners.size()==1?1:0;
  Check(owners.size()<=2 && !parts.empty() && parts.size()<=65536,"selected ownership sources/catalog disagree");
  Check(c.kind || parts.size()==1,"constant ownership requires exactly one part");
  std::set<std::string> unique;
  for(const auto& part:parts) {
    JsonQuote(part);Check(!part.empty() && part.find('\0')==std::string::npos && unique.insert(part).second,"invalid selected part identity");
  }
  for(const auto& owner:owners)owner.RequireUnchanged();
  if(c.kind)Check(owners[0].schema()==(c.kind==1?"patron-initial-columns-v1":"patron-output-v1"),"selected ownership schema disagrees");
  if(c.kind==2)Check(owners[1].schema()=="patron-cluster-order-v1","selected ownership coordinate schema disagrees");
  Check(primary.clusters()<=UINT32_MAX && primary.cells()<=UINT32_MAX,"semantic ownership exceeds integer domain");
  Ownership result;result.cluster_parts.resize(primary.clusters());
  if(c.kind) {
    CheckedRequestReader r(owners[0].path(),owners[0].generation().bytes);
    const auto magic=r.Read<U>();const auto version=r.Read<std::uint32_t>();
    if(c.kind==1) {
      Check(magic==0x315450494e415045ULL && version==1 && r.Read<U>()==primary.clusters() &&
            r.Read<U>()==primary.cells(),"initial semantic ownership envelope disagrees");
      for(auto& part:result.cluster_parts) { part=r.Read<std::uint32_t>();Check(part<c.parts.size(),"invalid initial semantic part"); }
      auto canonical=c.parts;std::sort(canonical.begin(),canonical.end());
      const auto fixes=r.Read<U>();Check(fixes<=primary.clusters() && fixes<=r.remaining()/8,"invalid semantic fixed count");
      std::set<std::uint32_t> seen;
      for(U i=0;i<fixes;++i) {
        const auto cluster=r.Read<std::uint32_t>(),part=r.Read<std::uint32_t>();
        Check(cluster<primary.clusters() && part<canonical.size() && seen.insert(cluster).second &&
              c.parts[result.cluster_parts[cluster]]==canonical[part],"semantic fixed ownership disagrees");
      }
    } else {
      Check(magic==0x3154554f54415045ULL && version==1 && r.Read<std::uint32_t>()==14 &&
            r.ReadString32()=="endpoint-exact-critical-flow-v14","PATRON semantic ownership envelope disagrees");
      for(unsigned i=0;i<16;++i)Check(std::isfinite(r.Read<double>()),"invalid PATRON semantic objective");
      for(unsigned i=0;i<8;++i)Check(r.Read<std::int64_t>()>=0,"invalid PATRON semantic counters");
      for(unsigned i=0;i<3;++i)r.Read<U>();
      Check(r.Read<std::uint32_t>()==0 && r.Read<U>()==primary.clusters(),"PATRON semantic dimensions disagree");
      CheckedRequestReader order(owners[1].path(),owners[1].generation().bytes);
      Check(order.Read<U>()==0x3152444f54415045ULL && order.Read<std::uint32_t>()==1 &&
            order.Read<U>()==primary.clusters(),"PATRON semantic coordinates disagree");
      std::vector<unsigned char> seen(primary.clusters());
      for(U i=0;i<primary.clusters();++i) {
        const auto coordinate=order.Read<std::uint32_t>(),part=r.Read<std::uint32_t>();
        Check(coordinate<primary.clusters() && !seen[coordinate] && part<c.parts.size(),"invalid PATRON semantic coordinate/part");
        seen[coordinate]=1;result.cluster_parts[coordinate]=part;
      }
      order.RequireEnd();
    }
    r.RequireEnd();
  }
  std::vector<bool> used(c.parts.size());for(auto p:result.cluster_parts)used[p]=true;
  for(unsigned p=0;p<c.parts.size();++p)if(used[p])result.parts.push_back(c.parts[p]);
  std::sort(result.parts.begin(),result.parts.end());Check(!result.parts.empty(),"semantic assignment has no parts");
  std::vector<std::uint32_t> translate(c.parts.size());
  for(unsigned p=0;p<c.parts.size();++p)if(used[p])
    translate[p]=std::lower_bound(result.parts.begin(),result.parts.end(),c.parts[p])-result.parts.begin();
  for(auto& part:result.cluster_parts)part=translate[part];return result;
}
struct Projection { U cuts=0,sinks=0,remote_endpoints=0,globals=0,replicated_inputs=0,register_inputs=0; };
Projection ProjectCuts(EmuirView& source,const PrimaryClusterView& primary,const Ownership& ownership,
                       const CandidateEligibility& eligible,SqliteDatabase& spool) {
  spool.Execute("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE;"
    "CREATE TABLE cuts(net_seq INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE,kind TEXT NOT NULL,source INTEGER NOT NULL,endpoints INTEGER NOT NULL);"
    "CREATE TABLE sinks(net_seq INTEGER NOT NULL,part INTEGER NOT NULL,PRIMARY KEY(net_seq,part)) WITHOUT ROWID");
  SqliteTransaction transaction(spool);Projection result;
  auto insert=spool.Prepare("INSERT INTO cuts VALUES(?,?,?,?,?)");
  auto sink=spool.Prepare("INSERT INTO sinks VALUES(?,?)");
  auto nets=source.store().database().Prepare("SELECT seq,id,cut_class FROM net_records ORDER BY seq");
  bool have=nets.Step();U observed=0;std::set<std::uint32_t> drivers;std::map<std::uint32_t,U> sinks;
  source.VisitConnectivity([&](const EmuirEndpoint& endpoint) {
    Check(have && endpoint.net.sequence()==observed,"semantic endpoint/net order disagrees");
    if(endpoint.role==EndpointRole::kSink)++result.sinks;
    if(endpoint.cell) {
      auto part=ownership.cluster_parts.at(primary.Owner(endpoint.cell->sequence()));
      if(endpoint.role==EndpointRole::kDriver)drivers.insert(part);else ++sinks[part];
    }
  },[&](EmuirNetId net) {
    Check(have && net.sequence()==observed && nets.TypedInteger(0)==std::int64_t(observed),"semantic net coverage disagrees");
    auto name=nets.TypedText(1),kind=nets.TypedText(2);JsonQuote(name);
    Check(!name.empty(),"empty semantic net identity");
    if(kind=="clock" || kind=="reset") {
      auto parts=drivers;for(const auto& item:sinks)parts.insert(item.first);result.globals+=parts.size()>1;
    } else if(drivers.empty() && (kind=="undriven" || kind=="primary_input")) {
      if(kind=="primary_input")result.replicated_inputs+=sinks.size()>1;
    } else {
      U endpoints=0;for(const auto& item:sinks)if(!drivers.count(item.first))endpoints+=item.second;
      if(endpoints) {
        Check(drivers.size()==1,"exact semantic cut must have one source FPGA");
        Check(kind!="combinational" || (eligible.bits[observed>>3]&(1U<<(observed&7))),"combinational cut is not independently eligible");
        insert.BindInteger(1,observed);insert.BindText(2,name);insert.BindText(3,kind);
        insert.BindInteger(4,*drivers.begin());insert.BindInteger(5,endpoints);insert.Execute();insert.Reset();
        for(const auto& item:sinks)if(!drivers.count(item.first)) {
          sink.BindInteger(1,observed);sink.BindInteger(2,item.first);sink.Execute();sink.Reset();
        }
        ++result.cuts;result.remote_endpoints+=endpoints;result.register_inputs+=kind=="register_input";
      }
    }
    ++observed;have=nets.Step();drivers.clear();sinks.clear();
  });
  Check(!have && observed==source.counts().nets && result.cuts<=UINT32_MAX && result.sinks<=UINT32_MAX,
        "semantic projection dimensions disagree");transaction.Commit();return result;
}
void WriteAnalysisInputs(const fs::path& request,const fs::path& parts,EmuirView& source,
                         const PrimaryClusterView& primary,const Ownership& ownership,
                         SqliteDatabase& spool,const Projection& projection,const std::vector<ArtifactRef>& inputs) {
  AtomicBinaryWriter writer(request.string(),inputs);
  writer.Write(U(0x31584553464d5545ULL));writer.Write(std::uint32_t(4));
  for(auto value:{source.counts().cells,source.counts().nets,projection.cuts,U(ownership.parts.size()),projection.sinks})writer.Write(std::uint32_t(value));
  for(U cell=0;cell<primary.cells();++cell)writer.Write(ownership.cluster_parts[primary.Owner(cell)]);
  auto cuts=spool.Prepare("SELECT net_seq,source FROM cuts ORDER BY name");
  auto sinks=spool.Prepare("SELECT part FROM sinks WHERE net_seq=? ORDER BY part");
  while(cuts.Step()) {
    std::vector<std::uint32_t> parts;sinks.BindInteger(1,cuts.TypedInteger(0));
    while(sinks.Step())parts.push_back(sinks.TypedInteger(0));sinks.Reset();
    writer.Write(std::uint32_t(cuts.TypedInteger(0)));writer.Write(std::uint32_t(cuts.TypedInteger(1)));
    writer.Write(std::uint32_t(parts.size()));for(auto part:parts)writer.Write(part);
  }
  writer.Commit();
  AtomicBinaryWriter catalog(parts.string(),inputs);catalog.Write(U(0x31504553464d5545ULL));
  catalog.Write(std::uint32_t(1));catalog.Write(std::uint32_t(ownership.parts.size()));
  for(const auto& part:ownership.parts)Text(catalog,part);catalog.Commit();
}
std::string AssignmentHash(EmuirView& source,const PrimaryClusterView& primary,const Ownership& ownership) {
  Sha256 digest;
  const auto put=[&](const std::string& text){digest.update(reinterpret_cast<const unsigned char*>(text.data()),text.size());};
  put("{\n  \"instance_assignment\": {");U count=0;
  auto rows=source.store().database().Prepare("SELECT seq,id FROM cell_records ORDER BY id");
  while(rows.Step()) {
    auto seq=rows.TypedInteger(0);Check(seq>=0 && U(seq)<primary.cells(),"semantic hash source identity disagrees");
    put(count++?",\n":"\n");put("    ");put(JsonQuote(rows.TypedText(1)));put(": ");
    put(JsonQuote(ownership.parts[ownership.cluster_parts[primary.Owner(seq)]]));
  }
  Check(count==primary.cells(),"semantic hash source coverage disagrees");put(count?"\n  }":"}");put("\n}\n");
  const auto bytes=digest.finish();const char* digits="0123456789abcdef";std::string hex;
  for(auto byte:bytes){hex+=digits[byte>>4];hex+=digits[byte&15];}return hex;
}
void WriteIdentities(const fs::path& output,SqliteDatabase& spool,const Ownership& ownership,
                     const ArtifactRef& captures,U count,const std::vector<ArtifactRef>& inputs) {
  OrderedRecordTable records(captures.path(),0x31434553464d5545ULL,1);
  std::optional<ExactCaptureRecord> capture;U consumed=0;
  const auto advance=[&]{if(consumed<records.count){capture=ReadExactCaptureRecord(records);++consumed;}else capture.reset();};advance();
  AtomicBinaryWriter writer(output.string(),inputs);writer.Write(U(0x314d4553464d5545ULL));
  writer.Write(std::uint32_t(1));writer.Write(std::uint32_t(count));
  auto cuts=spool.Prepare("SELECT net_seq,name,kind,source FROM cuts ORDER BY name");
  auto sinks=spool.Prepare("SELECT part FROM sinks WHERE net_seq=? ORDER BY part");
  while(cuts.Step()) {
    const auto name=cuts.TypedText(1);std::uint32_t count=0;
    while(capture && capture->net==name){Check(count<UINT32_MAX,"semantic capture count exceeds integer domain");++count;advance();}
    Check(!capture || capture->net>name,"semantic capture identity is orphaned");
    Text(writer,name);Text(writer,cuts.TypedText(2));Text(writer,ownership.parts.at(cuts.TypedInteger(3)));
    std::vector<std::uint32_t> parts;sinks.BindInteger(1,cuts.TypedInteger(0));
    while(sinks.Step())parts.push_back(sinks.TypedInteger(0));sinks.Reset();
    writer.Write(std::uint32_t(parts.size()));for(auto part:parts)Text(writer,ownership.parts.at(part));writer.Write(count);
  }
  Check(!capture && consumed==records.count,"semantic captures exceed cut coverage");writer.Commit();
}
std::string Segment(U number) {
  auto digits=std::to_string(number);return "segment"+std::string(digits.size()<6?6-digits.size():0,'0')+digits;
}
struct SemanticSummary { U cuts=0,combinational=0,edges=0,launches=0,captures=0,level=0,depth=0; };
SemanticSummary AuditSemantics(const ArtifactRef& nodes,const ArtifactRef& captures,const ArtifactRef& topology,
                               SqliteDatabase& spool,const Ownership& ownership,EmuirView& source,const Config& config) {
  OrderedRecordTable rows(nodes.path(),0x314e4553464d5545ULL,2);
  OrderedRecordTable capture_rows(captures.path(),0x31434553464d5545ULL,1);
  BinaryReader raw(topology.path());ReadAndValidateHeader(raw,{0x315845534f4d5545ULL,3,"semantic topology"});
  SemanticSummary result;result.cuts=rows.count;result.captures=capture_rows.count;
  Check(raw.Read<std::uint32_t>()==rows.count,"semantic topology/node coverage disagrees");const auto coordinates=raw.Read<U>();
  std::vector<std::uint32_t> nets,levels,depths;nets.reserve(rows.count);levels.reserve(rows.count);depths.reserve(rows.count);
  auto expected=spool.Prepare("SELECT net_seq,name,kind,source FROM cuts ORDER BY name");
  auto sinks=spool.Prepare("SELECT part FROM sinks WHERE net_seq=? ORDER BY part");
  for(U index=0;index<rows.count;++index) {
    Check(expected.Step(),"semantic node has no projected source cut");const auto node=ReadExactNodeRecord(rows);
    nets.push_back(expected.TypedInteger(0));levels.push_back(node.level);depths.push_back(node.depth);
    Check(node.net==expected.TypedText(1) && node.kind==expected.TypedText(2) &&
          node.source==ownership.parts.at(expected.TypedInteger(3)),"semantic node identity disagrees with source");
    std::vector<std::string> parts;sinks.BindInteger(1,expected.TypedInteger(0));
    while(sinks.Step())parts.push_back(ownership.parts.at(sinks.TypedInteger(0)));sinks.Reset();
    Check(parts==node.sinks,"semantic sink ownership disagrees with source");
    const auto launch=raw.Read<std::uint8_t>();const auto edges=raw.Read<std::uint32_t>();
    Check(launch<=1 && edges==node.predecessors.size() && node.flag==(launch?1:edges?0:2),"semantic topology flags disagree");
    for(auto predecessor:node.predecessor_indices)Check(raw.Read<std::uint32_t>()==predecessor,"semantic predecessor coordinate disagrees");
    result.edges+=edges;result.launches+=node.flag!=0;result.combinational+=node.kind=="combinational";
    result.level=std::max(result.level,U(node.level));result.depth=std::max(result.depth,U(node.depth));
  }
  Check(!expected.Step() && raw.remaining()==coordinates*16,"semantic node/topology extent disagrees");
  Check(!config.depth || result.depth<=config.depth,"exact combinational-cut dependency depth exceeds the configured limit");
  rows.Reset();U launches=0,edges=result.launches,capture_index=0,seen_captures=0;
  std::optional<ExactCaptureRecord> capture;
  const auto advance=[&]{if(seen_captures<capture_rows.count){capture=ReadExactCaptureRecord(capture_rows);++seen_captures;}else capture.reset();};advance();
  auto name=source.store().database().Prepare("SELECT id FROM net_records WHERE seq=?");
  for(U index=0;index<rows.count;++index) {
    const auto node=ReadExactNodeRecord(rows);U required_level=1,prior_depth=0;std::vector<std::string> sources;
    if(node.flag)sources.push_back(Segment(launches++));
    for(unsigned edge=0;edge<node.predecessor_indices.size();++edge) {
      auto predecessor=node.predecessor_indices[edge];name.BindInteger(1,nets.at(predecessor));
      Check(name.Step() && name.TypedText(0)==node.predecessors[edge],"semantic predecessor identity disagrees with source");name.Reset();
      required_level=std::max(required_level,U(levels[predecessor])+1);prior_depth=std::max(prior_depth,U(depths[predecessor]));
      sources.push_back(Segment(edges++));
    }
    // These local recurrences exclude cycles and independently verify exact
    // levels/depths without retaining a second graph or producer traversal.
    Check(node.level==required_level && node.depth==prior_depth+U(node.kind=="combinational"),"semantic dependency level/depth recurrence disagrees");
    Check(node.sources==sources,"semantic source segment numbering disagrees");std::vector<std::string> segments;
    while(capture && capture->net==node.net) {
      Check(std::binary_search(node.sinks.begin(),node.sinks.end(),capture->fpga),"semantic capture belongs to another sink");
      segments.push_back(Segment(result.launches+result.edges+capture_index++));advance();
    }
    Check(!capture || capture->net>node.net,"semantic capture has no node");std::sort(segments.begin(),segments.end());
    Check(segments==node.captures,"semantic capture numbering/coverage disagrees");
  }
  Check(launches==result.launches && edges==result.launches+result.edges &&
        capture_index==result.captures && !capture,"semantic summary coverage disagrees");return result;
}
void WriteCounts(const fs::path& output,SqliteDatabase& spool,U cuts,const std::vector<ArtifactRef>& inputs) {
  AtomicBinaryWriter writer(output.string(),inputs);std::uint32_t crc=0;
  const auto put=[&](auto value){writer.Write(value);UpdateCrc32(crc,reinterpret_cast<const unsigned char*>(&value),sizeof value);};
  put(U(0x31544350464d5545ULL));put(std::uint32_t(1));put(cuts);auto rows=spool.Prepare("SELECT endpoints FROM cuts ORDER BY name");U count=0;
  while(rows.Step()){Check(rows.TypedInteger(0)>0,"invalid semantic endpoint count");put(U(rows.Integer(0)));++count;}
  Check(count==cuts,"semantic endpoint coverage disagrees");writer.Write(crc);writer.Commit();
}
}
SelectedPartitionOwnership ReadSelectedPartitionOwnership(const PrimaryClusterView& primary,
    const std::vector<ArtifactRef>& owners,const std::vector<std::string>& parts) {
  return ReadOwnership(owners,parts,primary);
}
StageResult FinalizePartitionSemantics(const std::vector<ArtifactRef>& inputs,const std::string& directory) {
  return RunStage("phase3.finalize-assignment-semantics",[&]{for(const auto& source:inputs)source.RequireUnchanged();return inputs;},[&](const auto& refs) {
    const auto config=ReadConfig(refs);const auto destination=fs::absolute(directory);
    Check(!fs::exists(destination) && !fs::is_symlink(destination),"semantic output directory must be absent");
    fs::create_directories(destination.parent_path());Workspace workspace(destination.parent_path());
    const auto selected=workspace.root/"selected";fs::create_directory(selected);
    // The paired view validates its source namespace and ownership envelope.
    // This consumer uses only source-sequence owners, not the name/member pool.
    EmuirView source(refs[0]);PrimaryClusterView primary(refs[1],refs[0]);
    auto ownership=ReadSelectedPartitionOwnership(primary,{refs.begin()+4,refs.end()},config.parts);auto eligible=BuildCandidateEligibility({refs[0],refs[2]});
    SqliteDatabase spool((workspace.root/"cuts.sqlite").string(),SqliteDatabase::Mode::kCreateExclusive);
    const auto projection=ProjectCuts(source,primary,ownership,eligible,spool);std::vector<unsigned char>().swap(eligible.bits);
    const auto request=workspace.root/"analysis.bin",parts=workspace.root/"parts.bin",topology=workspace.root/"topology.bin";
    WriteAnalysisInputs(request,parts,source,primary,ownership,spool,projection,refs);
    const auto assignment_hash=AssignmentHash(source,primary,ownership);
    const auto request_ref=ArtifactRef::Bind(request.string(),"exact-indexed-request-v4");
    emuflow::native::AnalyzeStaticExact({refs[0],request_ref},topology.string());
    const auto topology_ref=ArtifactRef::Bind(topology.string(),"exact-topology-v3");
    const auto parts_ref=ArtifactRef::Bind(parts.string(),"exact-parts-v1");
    BuildExactCaptures({refs[0],request_ref,topology_ref,parts_ref},(selected/"captures.bin").string());
    const auto captures=ArtifactRef::Bind((selected/"captures.bin").string(),"exact-captures-v1");
    const auto identities=workspace.root/"identities.bin";WriteIdentities(identities,spool,ownership,captures,projection.cuts,refs);
    emuflow::native::FinalizeStaticExact({topology_ref,ArtifactRef::Bind(identities.string(),"exact-identities-v1")},(selected/"nodes.bin").string());
    const auto nodes=ArtifactRef::Bind((selected/"nodes.bin").string(),"exact-nodes-v2");
    const auto summary=AuditSemantics(nodes,captures,topology_ref,spool,ownership,source,config);
    WriteCounts(selected/"counts.bin",spool,projection.cuts,refs);
    Object cut_metrics{{"cut_nets",projection.cuts},{"cut_sink_endpoints",projection.remote_endpoints},
      {"global_nets",projection.globals},{"replicated_primary_inputs",projection.replicated_inputs},
      {"combinational_cut_nets",summary.combinational},{"maximum_combinational_dependency_depth",summary.depth},
      {"transport_ordering_policy","path-local-dependency-dag-v1"}};
    if(projection.register_inputs)cut_metrics["register_input_cut_nets"]=projection.register_inputs;
    Object metrics{{"transported_cut_nets",summary.cuts},{"combinational_cut_nets",summary.combinational},
      {"dependency_edges",summary.edges},{"maximum_dependency_level",summary.level},
      {"maximum_combinational_dependency_depth",summary.depth},{"logic_segments",summary.launches+summary.edges+summary.captures},
      {"capture_requirements",summary.captures}};
    Object report{{"schema","emuflow.partition-final-semantics/v1"},{"status","pass"},
      {"implementation_id","native-selected-partition-semantics-v1"},{"instances",primary.cells()},{"clusters",primary.clusters()},
      {"instance_assignment_sha256",assignment_hash},{"cut_metrics",cut_metrics},{"metrics",metrics},{"launches",summary.launches},
      {"validation",Object{{"status","pass"},{"scope","source-bound-cuts-independent-captures-and-topology-node-recurrences"}}}};
    std::vector<JsonRecord> catalog;for(const auto& part:ownership.parts)catalog.emplace_back(part);report["parts"]=std::move(catalog);
    PublishTextArtifact((selected/"summary.json").string(),refs,[&](std::ostream& out){out<<JsonRecord(report).EncodeUtf8()<<'\n';});
    // All outputs are closed; private projection and analyzer intermediates
    // remain workspace-owned and never enter the downstream artifact set.
    const auto ready=destination.parent_path()/(workspace.root.filename().string()+"-publish");
    fs::rename(selected,ready);
    try { PublishNewDirectory(ready.string(),destination.string(),refs); }
    catch(...) { std::error_code error;fs::remove_all(ready,error);throw; }
  },[&] {
    return std::vector<ArtifactRef>{ArtifactRef::Bind((fs::path(directory)/"nodes.bin").string(),"exact-nodes-v2"),
      ArtifactRef::Bind((fs::path(directory)/"captures.bin").string(),"exact-captures-v1"),
      ArtifactRef::Bind((fs::path(directory)/"counts.bin").string(),"partition-cut-counts-v1"),
      ArtifactRef::Bind((fs::path(directory)/"summary.json").string(),"partition-finalization-summary-v1")};
  });
}
}
