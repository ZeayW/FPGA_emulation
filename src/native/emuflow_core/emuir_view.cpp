#include "emuflow_core/emuir_view.hpp"

#include <limits>
#include <set>

namespace emuflow::core {
namespace {
const std::string& CheckedPath(const ArtifactRef& artifact) {
  if (artifact.schema() != "emuir-compact-v2" || artifact.is_pipe())
    throw StageError(ErrorCode::kInvalidArtifact, "native EmuIR view requires compact v2 storage");
  artifact.RequireUnchanged();
  return artifact.path();
}
[[noreturn]] void Invalid(const char* message) {
  throw StageError(ErrorCode::kInvalidArtifact, message);
}
}  // namespace
EmuirView::EmuirView(const ArtifactRef& artifact)
    : artifact_(std::make_shared<const ArtifactRef>(artifact)),
      database_(CheckedPath(artifact), SqliteDatabase::Mode::kReadOnly) {
  auto schema = database_.Prepare("SELECT value FROM metadata WHERE key='storage_schema'");
  auto counts = database_.Prepare(
      "SELECT json_extract(value,'$.cell_records'),json_extract(value,'$.net_records'),"
      "json_extract(value,'$.endpoint_records'),"
      "json_type(value,'$.cell_records')='integer' AND "
      "json_type(value,'$.net_records')='integer' AND "
      "json_type(value,'$.endpoint_records')='integer' "
      "FROM metadata WHERE key='record_counts_json'");
  if (!schema.Step() || schema.Text(0) != "emuflow.emuir-sqlite/v2" ||
      !counts.Step() || !counts.Integer(3) || counts.Integer(0) < 0 ||
      counts.Integer(1) < 0 || counts.Integer(2) < 0)
    Invalid("native compact EmuIR identity/counts disagree");
  counts_ = {static_cast<std::uint64_t>(counts.Integer(0)),
             static_cast<std::uint64_t>(counts.Integer(1)),
             static_cast<std::uint64_t>(counts.Integer(2))};
  artifact_->RequireUnchanged();
}
EmuirNetId EmuirView::Net(std::uint64_t sequence) const {
  if (sequence >= counts_.nets) Invalid("native compact EmuIR net identity is out of bounds");
  return EmuirNetId(artifact_, sequence);
}
std::string EmuirView::NetName(const EmuirNetId& net) {
  // Equal local sequence numbers from distinct artifacts are never aliases.
  if (net.owner_->path() != artifact_->path() || net.owner_->schema() != artifact_->schema() ||
      !(net.owner_->generation() == artifact_->generation()))
    throw StageError(ErrorCode::kMixedGeneration, "native EmuIR identity belongs to another artifact");
  artifact_->RequireUnchanged();
  auto name = database_.Prepare("SELECT id FROM net_records WHERE seq=?");
  name.BindInteger(1, static_cast<std::int64_t>(net.sequence()));
  if (!name.Step() || name.Text(0).empty()) Invalid("native compact EmuIR net identity is missing");
  auto result = name.Text(0);
  artifact_->RequireUnchanged();
  return result;
}
void EmuirView::VisitCellStates(const std::function<void(EmuirCellId, bool)>& visitor) {
  artifact_->RequireUnchanged();
  auto cells = database_.Prepare(
      "SELECT c.seq,json_type(p.value),"
      "coalesce(json_extract(p.value,'$.ff'),0)!=0 OR "
      "coalesce(json_extract(p.value,'$.bram'),0)!=0 OR "
      "coalesce(json_extract(p.value,'$.bram18k'),0)!=0 OR "
      "coalesce(json_extract(p.value,'$.uram288'),0)!=0 "
      "FROM cell_records c JOIN payloads p ON p.seq=c.resources_seq ORDER BY c.seq");
  std::uint64_t observed = 0;
  while (cells.Step()) {
    if (observed >= counts_.cells || cells.Integer(0) != static_cast<std::int64_t>(observed) ||
        cells.Text(1) != "object") Invalid("native compact EmuIR cell sequence is invalid");
    visitor(EmuirCellId(artifact_, observed++), cells.Integer(2) != 0);
  }
  if (observed != counts_.cells) Invalid("native compact EmuIR cell count disagrees");
  artifact_->RequireUnchanged();
}
void EmuirView::VisitConnectivity(
    const std::function<void(const EmuirEndpoint&)>& endpoint,
    const std::function<void(EmuirNetId)>& net_end, bool validate_pins) {
  artifact_->RequireUnchanged();
  std::set<std::string> top_ports;
  if (validate_pins) {
    auto ports = database_.Prepare("SELECT seq,json_extract(record_json,'$.id'),"
        "json_type(record_json,'$.id') FROM port_records ORDER BY seq");
    std::uint64_t ordinal = 0;
    while (ports.Step()) {
      if (ports.TypedInteger(0) != static_cast<std::int64_t>(ordinal++) ||
          ports.Text(2) != "text" || ports.Text(1).empty() || !top_ports.insert(ports.Text(1)).second)
        Invalid("native compact EmuIR port sequence is invalid");
    }
  }
  auto nets = database_.Prepare("SELECT seq FROM net_records ORDER BY seq");
  auto endpoints = database_.Prepare(
      std::string("SELECT e.net_seq,e.role,e.ordinal,coalesce(e.instance_seq,-1),"
      "typeof(e.net_seq)='integer' AND typeof(e.role)='integer' AND "
      "typeof(e.ordinal)='integer' AND (e.instance_seq IS NULL OR "
      "(typeof(e.instance_seq)='integer' AND e.instance_seq>=0)) ") +
      (validate_pins ? ",p.name,e.bit,typeof(e.bit)='integer' FROM endpoint_records e "
                      "LEFT JOIN pin_names p ON p.seq=e.port_seq " : "FROM endpoint_records e ") +
      "ORDER BY e.net_seq,e.role,e.ordinal");
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
      if (owner >= 0) cell = EmuirCellId(artifact_, static_cast<std::uint64_t>(owner));
      endpoint({net, role == 0 ? EndpointRole::kDriver : EndpointRole::kSink,
                static_cast<std::uint64_t>(ordinal), std::move(cell)});
      ++observed_endpoints;
      previous_role = role; previous_ordinal = ordinal;
      have_endpoint = endpoints.Step();
    }
    net_end(net);
    ++observed_nets;
  }
  if (have_endpoint || observed_nets != counts_.nets || observed_endpoints != counts_.endpoints)
    Invalid("native compact EmuIR endpoint/net coverage disagrees");
  artifact_->RequireUnchanged();
}
}  // namespace emuflow::core
