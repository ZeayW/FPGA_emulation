#include "emuflow_core/candidate_ids.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/emuir_view.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/integer_graph.hpp"
#include "emuflow_core/resource_view.hpp"
#include <algorithm>
#include <map>
#include <set>

namespace emuflow::core {
namespace {
void Check(bool condition,const char* message) {
  if (!condition) throw StageError(ErrorCode::kInvalidArtifact,message);
}
std::vector<std::string> Catalog(BinaryReader& input,bool empty=false) {
  const auto count=input.Read<std::uint32_t>();
  Check((count || empty) && count<=256 && count<=input.remaining()/4,"invalid candidate policy catalog");
  std::vector<std::string> result; std::set<std::string> unique;
  for (std::uint32_t i=0;i<count;++i) {
    auto value=input.ReadString32(); Check(unique.insert(value).second,"duplicate candidate policy identity");
    result.push_back(std::move(value));
  }
  return result;
}
void RequireUniqueIdentity(SqliteDatabase& source,const std::string& table) {
  auto indexes=source.Prepare("SELECT name FROM pragma_index_list(?) WHERE \"unique\"=1 AND partial=0");
  indexes.BindText(1,table);
  auto columns=source.Prepare("SELECT name FROM pragma_index_info(?) ORDER BY seqno");
  bool valid=false;
  while (indexes.Step()) {
    columns.BindText(1,indexes.TypedText(0));
    const bool first=columns.Step() && !columns.IsNull(0) && columns.Text(0)=="id";
    if (first && !columns.Step()) valid=true;
    columns.Reset();
  }
  Check(valid,"compact candidate identity lacks complete unique index");
}
struct Arc { std::uint32_t source,sink,net; };
struct NetFacts {
  std::uint32_t source=UINT32_MAX;
  unsigned char drivers=0,instance_drivers=0;
  bool sink=false,reason=false,combinational=false;
};
}
void BuildCandidateIds(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  Check(inputs.size()==2,"candidate source coverage is invalid");
  for (const auto& input:inputs) input.RequireUnchanged();
  EmuirView ir(inputs[0]);
  Check(ir.counts().cells<=UINT32_MAX && ir.counts().nets<=UINT32_MAX,"candidate graph exceeds uint32 identities");
  const auto cells=static_cast<std::uint32_t>(ir.counts().cells),nets=static_cast<std::uint32_t>(ir.counts().nets);
  BinaryReader request(inputs[1].path());
  Check(request.size()<=65536,"candidate policy exceeds bound");
  ReadAndValidateHeader(request,{0x31514543464d5545ULL,1,"candidate-policy-request-v1"});
  const FileGeneration expected={request.Read<std::uint64_t>(),request.Read<std::uint64_t>(),request.Read<std::uint64_t>(),
    request.Read<std::int64_t>(),request.Read<std::int64_t>(),request.Read<std::int64_t>(),request.Read<std::int64_t>()};
  if (!(expected==inputs[0].generation()))
    throw StageError(ErrorCode::kMixedGeneration,"candidate source generation changed");
  Check(request.Read<std::uint64_t>()==cells && request.Read<std::uint64_t>()==nets,"candidate source counts disagree");
  const auto fields=Catalog(request);
  Check(fields.size()==kResourceFields.size() && std::equal(fields.begin(),fields.end(),kResourceFields.begin()),
        "candidate resource field contract disagrees");
  const auto state=request.Read<std::uint64_t>(),hard=request.Read<std::uint64_t>();
  const auto lut=request.Read<std::uint32_t>();
  Check(state && hard && !(state&hard) && ((state|hard)>>fields.size())==0 && lut<fields.size() &&
        !((state|hard)&(UINT64_C(1)<<lut)),"invalid candidate resource policy");
  const auto prefix=request.ReadString32();
  std::map<std::string,std::uint64_t> port_flags;
  auto mask=[&](const auto& names) {
    std::uint64_t result=0;
    for (const auto& name:names) {
      auto found=port_flags.find(name);
      if (found==port_flags.end()) {
        Check(port_flags.size()<64,"candidate input policy exceeds port capacity");
        found=port_flags.emplace(name,UINT64_C(1)<<port_flags.size()).first;
      }
      result|=found->second;
    }
    return result;
  };
  const auto prefix_mask=mask(Catalog(request));
  std::vector<std::uint64_t> policies{0,prefix_mask};
  const auto types=request.Read<std::uint32_t>(); Check(types<=254,"candidate type policy exceeds capacity");
  std::map<std::string,unsigned char> type_policies;
  for (std::uint32_t i=0;i<types;++i) {
    const auto type=request.ReadString32(); const auto ports=Catalog(request,true);
    Check(type_policies.emplace(type,policies.size()).second,"duplicate candidate type policy");
    policies.push_back(mask(ports));
  }
  const auto cut_catalog=Catalog(request); const std::set<std::string> cut_classes(cut_catalog.begin(),cut_catalog.end());
  Check(cut_classes.count("combinational") && cut_classes.count("clock") && cut_classes.count("reset"),"incomplete candidate cut policy");
  request.RequireEnd();
  SqliteDatabase source(inputs[0].path(),SqliteDatabase::Mode::kImmutableReadOnly);
  auto identity=source.Prepare("SELECT (SELECT value FROM metadata WHERE key='logical_schema'),"
      "(SELECT count(*) FROM json_each((SELECT value FROM metadata WHERE key='record_counts_json')))");
  Check(identity.Step() && identity.Text(0)=="emuflow.emuir/v1" && identity.TypedInteger(1)==3,"invalid candidate logical/count schema");
  RequireUniqueIdentity(source,"cell_records"); RequireUniqueIdentity(source,"net_records");
  std::vector<unsigned char> classes(cells),input_policy(cells);
  ResourceView resources(source);
  auto cell_rows=source.Prepare("SELECT seq,id,type,resources_seq FROM cell_records ORDER BY seq");
  std::uint64_t observed=0;
  while (cell_rows.Step()) {
    Check(observed<cells && cell_rows.TypedInteger(0)==static_cast<std::int64_t>(observed) && !cell_rows.TypedText(1).empty(),
          "invalid candidate cell identity/sequence");
    const auto type=cell_rows.TypedText(2); Check(!type.empty(),"invalid candidate cell type");
    const auto present=resources.Get(cell_rows.TypedInteger(3)).present;
    const auto code=(present&state) ? 0 : (present&hard) ? 2 : present==(UINT64_C(1)<<lut) ? 1 : 3;
    classes[observed]=code;
    if (code==0) {
      const auto policy=type_policies.find(type);
      input_policy[observed]=type.compare(0,prefix.size(),prefix)==0 ? 1 : policy==type_policies.end() ? 0 : policy->second;
    }
    ++observed;
  }
  Check(observed==cells,"incomplete candidate cell sequence");
  std::vector<std::uint64_t> pin_flags{0};
  auto pins=source.Prepare("SELECT seq,name FROM pin_names ORDER BY seq");
  while (pins.Step()) {
    const auto name=pins.TypedText(1);
    Check(pins.TypedInteger(0)==static_cast<std::int64_t>(pin_flags.size()) && !name.empty(),"invalid candidate pin catalog");
    const auto found=port_flags.find(name); pin_flags.push_back(found==port_flags.end() ? 0 : found->second);
  }
  std::vector<NetFacts> facts(nets); std::vector<Arc> arcs;
  auto metadata=source.Prepare("SELECT seq,id,cut_class FROM net_records ORDER BY seq");
  bool have=metadata.Step(); std::uint64_t net_seq=0;
  bool graph_net=false; std::uint32_t driver=UINT32_MAX; bool multiple=false;
  auto begin_net=[&]() {
    Check(have && net_seq<nets && metadata.TypedInteger(0)==static_cast<std::int64_t>(net_seq) &&
          !metadata.TypedText(1).empty(),"invalid candidate net identity/sequence");
    const auto cut=metadata.TypedText(2); Check(cut_classes.count(cut),"invalid candidate cut class");
    facts[net_seq].combinational=cut=="combinational"; graph_net=cut!="clock" && cut!="reset";
    driver=UINT32_MAX; multiple=false;
  };
  if (have) begin_net();
  ir.VisitConnectivity([&](const EmuirEndpoint& endpoint) {
    Check(endpoint.net.sequence()==net_seq && endpoint.pin && endpoint.pin->sequence()<pin_flags.size(),"invalid candidate endpoint identity");
    auto& fact=facts[net_seq];
    if (endpoint.role==EndpointRole::kDriver) {
      fact.drivers=std::min(2,static_cast<int>(fact.drivers)+1);
      if (endpoint.cell) {
        const auto owner=static_cast<std::uint32_t>(endpoint.cell->sequence());
        fact.instance_drivers=std::min(2,static_cast<int>(fact.instance_drivers)+1); fact.source=owner;
        if (driver==UINT32_MAX) driver=owner; else if (driver!=owner) multiple=true;
      }
    } else {
      fact.sink=true;
      if (endpoint.cell) {
        const auto owner=static_cast<std::uint32_t>(endpoint.cell->sequence());
        if (fact.combinational && (classes[owner]==0 ? !(policies[input_policy[owner]]&pin_flags[endpoint.pin->sequence()]) : classes[owner]!=1))
          fact.reason=true;
        if (graph_net && driver!=UINT32_MAX && !multiple && classes[driver]!=0 && classes[owner]!=0)
          arcs.push_back({driver,owner,static_cast<std::uint32_t>(net_seq)});
      }
    }
  },[&](EmuirNetId net) {
    Check(have && net.sequence()==net_seq,"candidate net coverage disagrees");
    ++net_seq; have=metadata.Step(); if (have) begin_net();
  },true);
  Check(!have && net_seq==nets,"incomplete candidate net sequence");
  const auto cyclic=CyclicVertices(cells,arcs);
  for (const auto& arc:arcs) if (facts[arc.net].combinational && cyclic[arc.sink]) facts[arc.net].reason=true;
  std::vector<unsigned char> bits((static_cast<std::uint64_t>(nets)+7)/8);
  std::uint64_t eligible=0;
  for (std::uint32_t net=0;net<nets;++net) {
    const auto& fact=facts[net];
    if (fact.combinational && fact.drivers==1 && fact.instance_drivers==1 && fact.sink && !fact.reason &&
        fact.source!=UINT32_MAX && classes[fact.source]==1 && !cyclic[fact.source]) {
      bits[net>>3]|=1U<<(net&7); ++eligible;
    }
  }
  std::vector<Arc>().swap(arcs); std::vector<NetFacts>().swap(facts);
  std::vector<unsigned char>().swap(classes); std::vector<unsigned char>().swap(input_policy);
  // A single lexical scan streams the name pool. Offsets and its byte length
  // follow it, so no full name pool, spool or second name scan is required.
  AtomicBinaryWriter writer(output); std::uint32_t crc=0;
  auto put=[&](const void* data,std::size_t bytes) { writer.WriteBytes(data,bytes); UpdateCrc32(crc,static_cast<const unsigned char*>(data),bytes); };
  auto scalar=[&](auto value) { put(&value,sizeof value); };
  scalar(UINT64_C(0x31544343464d5545)); scalar(std::uint32_t{1}); scalar(std::uint64_t{nets}); scalar(eligible);
  put(bits.data(),bits.size());
  std::vector<std::uint64_t> offsets{0}; std::string previous;
  auto names=source.Prepare("SELECT seq,id FROM net_records ORDER BY id"); observed=0;
  while (names.Step()) {
    const auto seq=names.TypedInteger(0); const auto name=names.TypedText(1);
    Check(seq>=0 && static_cast<std::uint64_t>(seq)<nets && !name.empty() && (!observed || previous<name),"invalid candidate net catalog");
    previous=name; ++observed;
    if (bits[seq>>3]&(1U<<(seq&7))) { put(name.data(),name.size()); offsets.push_back(offsets.back()+name.size()); }
  }
  Check(observed==nets && offsets.size()==eligible+1,"candidate name coverage disagrees");
  put(offsets.data(),offsets.size()*sizeof(offsets[0])); scalar(offsets.back()); writer.Write(crc);
  for (const auto& input:inputs) input.RequireUnchanged();
  writer.Commit();
}
}
