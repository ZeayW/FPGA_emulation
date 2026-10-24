#include "emuflow_core/emuir_view.hpp"

#include <limits>
#include <set>

namespace emuflow::core {
namespace {
const ArtifactRef& CheckedArtifact(const ArtifactRef& artifact) {
  if ((artifact.schema() != "emuir-compact-v2" && artifact.schema()!="emuir-indexed-v1") || artifact.is_pipe())
    throw StageError(ErrorCode::kInvalidArtifact, "native EmuIR view requires registered v1/v2 storage");
  artifact.RequireUnchanged();
  return artifact;
}
[[noreturn]] void Invalid(const char* message) {
  throw StageError(ErrorCode::kInvalidArtifact, message);
}
}  // namespace
EmuirView::EmuirView(const ArtifactRef& artifact)
    : store_(CheckedArtifact(artifact)),database_(store_.database()) {
  compact_=artifact.schema()=="emuir-compact-v2";
  if (!compact_) {
    auto counts=database_.Prepare("SELECT (SELECT count(*) FROM instances),(SELECT count(*) FROM nets),"
                                   "(SELECT count(*) FROM endpoints)");
    if (!counts.Step()) Invalid("legacy EmuIR counts are missing");
    counts_={static_cast<std::uint64_t>(counts.TypedInteger(0)),
             static_cast<std::uint64_t>(counts.TypedInteger(1)),static_cast<std::uint64_t>(counts.TypedInteger(2))};
    store_.RequireUnchanged();
    return;
  }
  auto counts = database_.Prepare(
      "SELECT json_extract(value,'$.cell_records'),json_extract(value,'$.net_records'),"
      "json_extract(value,'$.endpoint_records'),"
      "json_type(value,'$.cell_records')='integer' AND "
      "json_type(value,'$.net_records')='integer' AND "
      "json_type(value,'$.endpoint_records')='integer' "
      "FROM metadata WHERE key='record_counts_json'");
  if (!counts.Step() || !counts.Integer(3) || counts.Integer(0) < 0 ||
      counts.Integer(1) < 0 || counts.Integer(2) < 0)
    Invalid("native compact EmuIR identity/counts disagree");
  counts_ = {static_cast<std::uint64_t>(counts.Integer(0)),
             static_cast<std::uint64_t>(counts.Integer(1)),
             static_cast<std::uint64_t>(counts.Integer(2))};
  store_.RequireUnchanged();
}
EmuirNetId EmuirView::Net(std::uint64_t sequence) const {
  if (sequence >= counts_.nets) Invalid("native compact EmuIR net identity is out of bounds");
  return store_.Id<EmuirNetTag>(sequence);
}
std::string EmuirView::NetName(const EmuirNetId& net) {
  store_.Require(net);
  if (net.sequence()>=counts_.nets) Invalid("native EmuIR net identity is out of bounds");
  auto name = database_.Prepare(compact_ ? "SELECT id FROM net_records WHERE seq=?" : "SELECT id FROM nets WHERE seq=?");
  name.BindInteger(1, static_cast<std::int64_t>(net.sequence()));
  if (!name.Step() || name.Text(0).empty()) Invalid("native compact EmuIR net identity is missing");
  auto result = name.Text(0);
  store_.RequireUnchanged();
  return result;
}
void EmuirView::VisitCellStates(const std::function<void(EmuirCellId, bool)>& visitor) {
  store_.RequireUnchanged();
  auto cells = database_.Prepare(compact_ ?
      "SELECT c.seq,json_type(p.value),"
      "coalesce(json_extract(p.value,'$.ff'),0)!=0 OR "
      "coalesce(json_extract(p.value,'$.bram'),0)!=0 OR "
      "coalesce(json_extract(p.value,'$.bram18k'),0)!=0 OR "
      "coalesce(json_extract(p.value,'$.uram288'),0)!=0 "
      "FROM cell_records c JOIN payloads p ON p.seq=c.resources_seq ORDER BY c.seq" :
      "SELECT c.seq,'object',coalesce(max(r.resource IN ('ff','bram','bram18k','uram288') AND r.amount!=0),0) "
      "FROM instances c LEFT JOIN instance_resources r ON r.instance_seq=c.seq GROUP BY c.seq ORDER BY c.seq");
  std::uint64_t observed = 0;
  while (cells.Step()) {
    if (observed >= counts_.cells || cells.Integer(0) != static_cast<std::int64_t>(observed) ||
        cells.Text(1) != "object") Invalid("native compact EmuIR cell sequence is invalid");
    visitor(store_.Id<EmuirCellTag>(observed++), cells.Integer(2) != 0);
  }
  if (observed != counts_.cells) Invalid("native compact EmuIR cell count disagrees");
  store_.RequireUnchanged();
}
void EmuirView::VisitConnectivity(
    const std::function<void(const EmuirEndpoint&)>& endpoint,
    const std::function<void(EmuirNetId)>& net_end, bool validate_pins) {
  store_.RequireUnchanged();
  std::set<std::string> top_ports;
  if (validate_pins) {
    auto ports = database_.Prepare(compact_ ? "SELECT seq,json_extract(record_json,'$.id'),"
        "json_type(record_json,'$.id') FROM port_records ORDER BY seq" :
        "SELECT seq,id,typeof(id) FROM ports ORDER BY seq");
    std::uint64_t ordinal = 0;
    while (ports.Step()) {
      if (ports.TypedInteger(0) != static_cast<std::int64_t>(ordinal++) ||
          ports.Text(2) != "text" || ports.Text(1).empty() || !top_ports.insert(ports.Text(1)).second)
        Invalid("native compact EmuIR port sequence is invalid");
    }
  }
  auto nets = database_.Prepare(compact_ ? "SELECT seq FROM net_records ORDER BY seq" : "SELECT seq FROM nets ORDER BY seq");
  auto endpoints = database_.Prepare(
      compact_ ? std::string("SELECT e.net_seq,e.role,e.ordinal,coalesce(e.instance_seq,-1),"
      "typeof(e.net_seq)='integer' AND typeof(e.role)='integer' AND "
      "typeof(e.ordinal)='integer' AND (e.instance_seq IS NULL OR "
      "(typeof(e.instance_seq)='integer' AND e.instance_seq>=0)) ") +
      (validate_pins ? ",p.name,e.bit,typeof(e.bit)='integer',e.port_seq FROM endpoint_records e "
                      "LEFT JOIN pin_names p ON p.seq=e.port_seq " : "FROM endpoint_records e ") +
      "ORDER BY e.net_seq,e.role,e.ordinal" :
      std::string("SELECT e.net_seq,e.role,e.ordinal,CASE WHEN e.instance_id IS NULL THEN -1 WHEN i.seq IS NULL THEN -2 ELSE i.seq END,"
      "typeof(e.net_seq)='integer' AND typeof(e.role)='integer' AND typeof(e.ordinal)='integer' ")+
      (validate_pins ? ",e.port,e.bit,typeof(e.bit)='integer',NULL " : " ")+
      "FROM endpoints e LEFT JOIN instances i ON i.id=e.instance_id ORDER BY e.net_seq,e.role,e.ordinal");
  bool have_endpoint = endpoints.Step();
  std::uint64_t observed_nets = 0, observed_endpoints = 0;
  while (nets.Step()) {
    if (observed_nets >= counts_.nets || nets.Integer(0) != static_cast<std::int64_t>(observed_nets))
      Invalid("native compact EmuIR net sequence is invalid");
    const auto net = Net(observed_nets);
    std::int64_t previous_role = -1, previous_ordinal = -1;
    while (have_endpoint && endpoints.Integer(0) == static_cast<std::int64_t>(observed_nets)) {
      const auto role = endpoints.Integer(1), ordinal = endpoints.Integer(2), owner = endpoints.Integer(3);
      if (!endpoints.Integer(4) || (role != 0 && role != 1) || ordinal < 0 ||
          role < previous_role || (role == previous_role && ordinal <= previous_ordinal) ||
          owner < -1 || (owner >= 0 && static_cast<std::uint64_t>(owner) >= counts_.cells))
        Invalid("native compact EmuIR endpoint is invalid");
      if (validate_pins && (endpoints.IsNull(5) || endpoints.TypedText(5).empty() ||
          !endpoints.Integer(7) || endpoints.TypedInteger(6) < 0 ||
          (owner < 0 && !top_ports.count(endpoints.Text(5)))))
        Invalid("native compact EmuIR endpoint pin is invalid");
      std::optional<EmuirCellId> cell;
      if (owner >= 0) cell = store_.Id<EmuirCellTag>(static_cast<std::uint64_t>(owner));
      std::optional<EmuirPinId> pin;
      if (validate_pins && compact_) {
        if (endpoints.TypedInteger(8)<1) Invalid("native compact EmuIR pin identity is invalid");
        pin=store_.Id<EmuirPinTag>(static_cast<std::uint64_t>(endpoints.TypedInteger(8)));
      }
      endpoint({net, role == 0 ? EndpointRole::kDriver : EndpointRole::kSink,
                static_cast<std::uint64_t>(ordinal), std::move(cell), std::move(pin),
                validate_pins ? endpoints.Text(5) : "",validate_pins ? static_cast<std::uint64_t>(endpoints.TypedInteger(6)) : 0});
      ++observed_endpoints;
      previous_role = role; previous_ordinal = ordinal;
      have_endpoint = endpoints.Step();
    }
    net_end(net);
    ++observed_nets;
  }
  if (have_endpoint || observed_nets != counts_.nets || observed_endpoints != counts_.endpoints)
    Invalid("native compact EmuIR endpoint/net coverage disagrees");
  store_.RequireUnchanged();
}
}  // namespace emuflow::core
