#include "flow/compile.hpp"
#include "flow/route_policy.hpp"
#include "flow/finalize.hpp"
#include "flow/signoff.hpp"
#include "flow/comparison.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/json_record.hpp"
#include "storage/sqlite.hpp"
#include "scheduling/phase5.hpp"
#include "timing/phase2.hpp"
#include "partition/phase3.hpp"
#include "physical/flow/controller.hpp"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <unistd.h>
#include <fcntl.h>
#include <optional>

namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
constexpr const char* stages[] = {"preflight","frontend","timing-policy","timing","partition-policy","partition","route-policy",
    "route","tdm","split","physical","signoff","finalize"};
void Require(bool condition, const std::string& message) {
  if (!condition) throw StageError(ErrorCode::kInvalidRequest,message);
}
// JSON is bounded control-plane configuration, never a graph store. SQLite's
// existing JSON reader avoids adding an unrelated parser/dependency closure.
class ControlJson {
 public:
  explicit ControlJson(const std::string& path, std::size_t limit = 8 * 1024 * 1024)
      : db_(":memory:",SqliteDatabase::Mode::kReadOnly) {
    const auto size = fs::file_size(path);
    Require(size <= limit,"native compiler control file exceeds bound");
    std::ifstream stream(path,std::ios::binary);
    data_.resize(size); stream.read(data_.data(),size);
    Require(bool(stream),"cannot read native compiler control file");
    (void)JsonQuote(data_);
    auto valid = db_.Prepare("SELECT json_valid(?1),json_type(?1)"); valid.BindText(1,data_);
    Require(valid.Step() && valid.Integer(0) && valid.Text(1) == "object","invalid native compiler JSON object");
    auto duplicates = db_.Prepare("SELECT 1 FROM json_tree(?1) WHERE parent IS NOT NULL "
        "GROUP BY parent,key HAVING count(*) > 1 LIMIT 1"); duplicates.BindText(1,data_);
    Require(!duplicates.Step(),"duplicate native compiler JSON key");
  }
  std::string String(const std::string& path) {
    auto field = db_.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");
    field.BindText(1,data_); field.BindText(2,path);
    Require(field.Step() && field.Text(0) == "text","native compiler requires string field " + path);
    auto value = field.Text(1);
    Require(value.find('\0') == std::string::npos,"NUL in native compiler field");
    return value;
  }
  std::uint64_t Unsigned(const std::string& path) {
    auto row=db_.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");row.BindText(1,data_);row.BindText(2,path);
    Require(row.Step() && row.Text(0)=="integer" && row.TypedInteger(1)>=0,"native compiler requires nonnegative count "+path);return row.Integer(1);
  }
  bool Has(const std::string& path) {
    auto row=db_.Prepare("SELECT json_type(?1,?2)");row.BindText(1,data_);row.BindText(2,path);
    return row.Step() && !row.IsNull(0) && row.Text(0)!="null";
  }
  bool Boolean(const std::string& path) {
    auto row=db_.Prepare("SELECT json_type(?1,?2)");row.BindText(1,data_);row.BindText(2,path);
    Require(row.Step() && (row.Text(0)=="true" || row.Text(0)=="false"),"native compiler requires boolean "+path);return row.Text(0)=="true";
  }
  ArtifactRef Phase2Policy() {
    auto result=ArtifactRef::Bind(StatePath("native_phase2_policy"),"phase2-policy-v1");
    const auto& g=result.generation();const std::string fields[]={std::to_string(g.device),std::to_string(g.inode),std::to_string(g.bytes),std::to_string(g.modified_seconds),std::to_string(g.modified_nanoseconds),std::to_string(g.changed_seconds),std::to_string(g.changed_nanoseconds)};
    Require(String("$.state[1].native_phase2_generation[0]")=="tuple","native timing policy binding is invalid");
    for(unsigned i=0;i<7;++i)Require(String("$.state[1].native_phase2_generation[1]["+std::to_string(i)+"]")==fields[i],"native timing policy generation changed");return result;
  }
  ArtifactRef Phase2Configuration() {
    auto result=ArtifactRef::Bind(StatePath("native_phase2_configuration"),"phase2-configuration-request-v1");
    const auto& g=result.generation();const std::string fields[]={std::to_string(g.device),std::to_string(g.inode),std::to_string(g.bytes),std::to_string(g.modified_seconds),std::to_string(g.modified_nanoseconds),std::to_string(g.changed_seconds),std::to_string(g.changed_nanoseconds)};
    Require(String("$.state[1].native_phase2_configuration_generation[0]")=="tuple","native timing configuration binding is invalid");
    for(unsigned i=0;i<7;++i)Require(String("$.state[1].native_phase2_configuration_generation[1]["+std::to_string(i)+"]")==fields[i],"native timing configuration generation changed");return result;
  }
  std::string TimingPolicyState(const fs::path& root,const std::optional<ArtifactRef>& policy) {
    const auto path=[](const fs::path& p){return JsonRecord(std::vector<JsonRecord>{"path",p.string()}).Encode();};
    auto row=db_.Prepare("SELECT json_remove(json_set(?1,'$.stage','timing-policy',"
        "'$.state[1].timing_root',json(?2),'$.state[1].path_database_path',json(?3),"
        "'$.state[1].net_weights_path',json(?4),'$.state[1].cut_segment_qualification_path',NULL,"
        "'$.state[1].timing_report',CASE WHEN json_type(?1,'$.state[1].prepared_timing')!='null' "
        "THEN json_extract(?1,'$.state[1].timing_report') ELSE NULL END),"
        "'$.state[1].native_phase2_configuration','$.state[1].native_phase2_configuration_generation')");
    row.BindText(1,data_);row.BindText(2,path(root/"timing"));row.BindText(3,path(root/"timing/path-database.json"));row.BindText(4,path(root/"timing/partition-net-weights.json"));
    Require(row.Step(),"cannot construct native timing configuration state");const auto state=row.TypedText(0);
    if(!policy)return state;
    const auto& g=policy->generation();std::vector<JsonRecord> generation;
    for(const auto& value:{std::to_string(g.device),std::to_string(g.inode),std::to_string(g.bytes),std::to_string(g.modified_seconds),std::to_string(g.modified_nanoseconds),std::to_string(g.changed_seconds),std::to_string(g.changed_nanoseconds)})generation.emplace_back(value);
    auto bind=db_.Prepare("SELECT json_set(?1,'$.state[1].native_phase2_policy',json(?2),'$.state[1].native_phase2_generation',json(?3))");
    bind.BindText(1,state);bind.BindText(2,path(policy->path()));bind.BindText(3,JsonRecord(std::vector<JsonRecord>{"tuple",generation}).Encode());
    Require(bind.Step(),"cannot bind native timing policy state");return bind.TypedText(0);
  }
  std::string TimingState(const std::string& reference) {
    auto row=db_.Prepare("SELECT json_remove(json_set(?1,'$.stage','timing','$.state[1].timing_report',json(?2)),'$.state[1].native_phase2_policy','$.state[1].native_phase2_generation')");
    row.BindText(1,data_);row.BindText(2,reference);Require(row.Step(),"cannot construct native timing compiler state");return row.TypedText(0);
  }
  ArtifactRef FrontendRequest() {
    auto result=ArtifactRef::Bind(StatePath("native_frontend_request"),"frontend-request-v1");
    const auto& g=result.generation();const std::string fields[]={std::to_string(g.device),std::to_string(g.inode),std::to_string(g.bytes),std::to_string(g.modified_seconds),std::to_string(g.modified_nanoseconds),std::to_string(g.changed_seconds),std::to_string(g.changed_nanoseconds)};
    Require(String("$.state[1].native_frontend_generation[0]")=="tuple","native frontend request binding is invalid");
    for(unsigned i=0;i<7;++i)Require(String("$.state[1].native_frontend_generation[1]["+std::to_string(i)+"]")==fields[i],"native frontend request generation changed");
    return result;
  }
  ArtifactRef FrontendInputRequest() {
    auto result=ArtifactRef::Bind(StatePath("native_frontend_input_request"),"frontend-input-request-v1");
    const auto& g=result.generation();const std::string fields[]={std::to_string(g.device),std::to_string(g.inode),std::to_string(g.bytes),std::to_string(g.modified_seconds),std::to_string(g.modified_nanoseconds),std::to_string(g.changed_seconds),std::to_string(g.changed_nanoseconds)};
    Require(String("$.state[1].native_frontend_input_generation[0]")=="tuple","native input request binding is invalid");
    for(unsigned i=0;i<7;++i)Require(String("$.state[1].native_frontend_input_generation[1]["+std::to_string(i)+"]")==fields[i],"native input request generation changed");
    return result;
  }
  ArtifactRef PreparedTimingSummary() {
    auto result=ArtifactRef::Bind(StatePath("native_prepared_timing_summary"),"prepared-timing-summary-v1");
    const auto& g=result.generation();const std::uint64_t fields[]={g.device,g.inode,g.bytes,std::uint64_t(g.modified_seconds),std::uint64_t(g.modified_nanoseconds),std::uint64_t(g.changed_seconds),std::uint64_t(g.changed_nanoseconds)};
    Require(String("$.state[1].native_prepared_timing_generation[0]")=="tuple","native prepared timing binding is invalid");
    for(unsigned i=0;i<7;++i)Require(Unsigned("$.state[1].native_prepared_timing_generation[1]["+std::to_string(i)+"]")==fields[i],"prepared timing report generation changed");
    return result;
  }
  ArtifactRef Phase3Configuration() {
    auto result = ArtifactRef::Bind(StatePath("native_phase3_configuration"),
                                    "phase3-configuration-request-v1");
    const auto &g = result.generation();
    const std::string values[] = {std::to_string(g.device),
                                  std::to_string(g.inode),
                                  std::to_string(g.bytes),
                                  std::to_string(g.modified_seconds),
                                  std::to_string(g.modified_nanoseconds),
                                  std::to_string(g.changed_seconds),
                                  std::to_string(g.changed_nanoseconds)};
    Require(String("$.state[1].native_phase3_configuration_generation[0]") ==
                "tuple",
            "native partition configuration binding is invalid");
    for (unsigned i = 0; i < 7; ++i)
      Require(String("$.state[1].native_phase3_configuration_generation[1][" +
                     std::to_string(i) + "]") == values[i],
              "native partition configuration source generation changed");
    return result;
  }
  std::string
  PartitionPolicyState(const fs::path &root, const ArtifactRef &policy,
                       const ArtifactRef &summary,
                       const std::map<std::string, ArtifactRef> &runtime,
                       std::vector<ArtifactRef> &bindings) {
    using D = BoundedJson;
    using O = std::map<std::string, std::string>;
    D doc(data_);
    auto top = doc.Fields();
    auto envelope = D::Array(doc.Token("state"), 2);
    Require(envelope.size() == 2 && envelope[0] == "\"map\"",
            "invalid compile state");
    auto state = D(envelope[1]).Fields();
    const auto path = [](const fs::path &p) {
      return "[\"path\"," + JsonQuote(p.string()) + "]";
    };
    const auto pack = [&](const auto &self,
                          const std::string &value) -> std::string {
      if (value[0] == '{') {
        O fields;
        for (const auto &f : D(value).Fields())
          fields[f.first] = self(self, f.second);
        return "[\"map\"," + D::Encode(fields) + "]";
      }
      if (value[0] == '[') {
        std::string list = "[";
        for (const auto &t : D::Array(value)) {
          if (list.size() > 1)
            list += ',';
          list += self(self, t);
        }
        return "[\"list\"," + list + "]]";
      }
      return value;
    };
    D projection(D::Read(summary));
    projection.Keys({"schema", "effective_route_constraints",
                     "link_timing_report", "board_link_timing"});
    Require(projection.Text("schema") ==
                "emuflow.phase3-configuration-summary/v1",
            "partition configuration summary identity disagrees");
    state["path_database_payload"] =
        runtime.count("timing") ? path(runtime.at("timing").path()) : "null";
    if (!runtime.count("timing") &&
        Boolean("$.state[1].internal_timing_database")) {
      auto selector = ArtifactRef::Bind(StatePath("path_database_path"),
                                        "sta-path-selector-v1");
      D manifest(D::Read(selector));
      D payload(D(manifest.Token("payloads")).Token("paths"));
      const auto relative = payload.Text("path");
      Require(fs::path(relative).filename() == relative,
              "timing payload escapes selector");
      auto payload_ref = ArtifactRef::Bind(
          (fs::path(selector.path()).parent_path() / relative).string(),
          "timing-path-store-v1");
      bindings.push_back(selector);
      bindings.push_back(payload_ref);
      state["path_database_payload"] = path(payload_ref.path());
    }
    state["net_weights_payload"] =
        runtime.count("weights") ? path(runtime.at("weights").path()) : "null";
    if (!runtime.count("weights") &&
        fs::is_regular_file(StatePath("net_weights_path"))) {
      auto selector = ArtifactRef::Bind(StatePath("net_weights_path"),
                                        "net-weight-selector-v2");
      D manifest(D::Read(selector));
      D payload(D(manifest.Token("payloads")).Token("weights"));
      const auto relative = payload.Text("path");
      Require(fs::path(relative).filename() == relative,
              "weight payload escapes selector");
      auto payload_ref = ArtifactRef::Bind(
          (fs::path(selector.path()).parent_path() / relative).string(),
          "net-weight-store-v1");
      bindings.push_back(selector);
      bindings.push_back(payload_ref);
      state["net_weights_payload"] = path(payload_ref.path());
    }
    const bool has_link = projection.Token("link_timing_report") != "null";
    state["effective_route_constraints"] =
        has_link ? path(root / "timing/board-link-route-constraints.json")
                 : state.at("route_constraints");
    state["effective_route_constraints_path"] =
        has_link ? state["effective_route_constraints"] : "null";
    state["copied_link_timing_path"] =
        has_link ? path(root / "timing/board-link-timing.json") : "null";
    state["link_timing_report"] =
        pack(pack, projection.Token("link_timing_report"));
    if (has_link) {
      for (const auto &item :
           std::vector<std::pair<const char *, const char *>>{
               {"board-link-route-constraints.json",
                "effective_route_constraints"},
               {"board-link-timing.json", "board_link_timing"},
               {"board-link-timing-report.json", "link_timing_report"}}) {
        const auto target = root / "timing" / item.first;
        fs::create_directories(target.parent_path());
        PublishTextArtifact(target.string(), bindings,
                            [&](std::ostream &stream) {
                              stream << projection.Token(item.second) << '\n';
                            });
        bindings.push_back(ArtifactRef::Bind(target.string(),
                                             "phase3-configuration-source-v1"));
      }
    }
    state["native_effective_route_constraints_generation"]="null";
    if(has_link){const auto& g=bindings[bindings.size()-3].generation();
      std::vector<JsonRecord> numbers;for(const auto& n:{std::to_string(g.device),std::to_string(g.inode),std::to_string(g.bytes),std::to_string(g.modified_seconds),std::to_string(g.modified_nanoseconds),std::to_string(g.changed_seconds),std::to_string(g.changed_nanoseconds)})numbers.emplace_back(n);
      state["native_effective_route_constraints_generation"]=JsonRecord(std::vector<JsonRecord>{"tuple",numbers}).Encode();}
    if (state.count("timing_report") && state.at("timing_report") != "null") {
      auto timing = D::Array(state.at("timing_report"), 2);
      Require(timing.size() == 2, "invalid timing report envelope");
      const auto kind = DecodeJsonString(timing[0]);
      O report;
      if (kind == "map")
        report = D(timing[1]).Fields();
      else {
        Require(kind == "timing-report" || kind == "prepared-timing-report",
                "unknown timing report owner");
        D receipt(timing[1]);
        const auto schema = kind == "timing-report"
                                ? "phase2-report-v1"
                                : "prepared-timing-summary-v1";
        Require(receipt.Text("schema") == schema,
                "timing report schema disagrees");
        auto ref = ArtifactRef::Bind(receipt.Text("path"), schema);
        const auto &g = ref.generation();
        RequireEquivalentJson(
            db_, receipt.Token("generation"),
            JsonRecord(std::vector<JsonRecord>{
                           g.device, g.inode, g.bytes, g.modified_seconds,
                           g.modified_nanoseconds, g.changed_seconds,
                           g.changed_nanoseconds})
                .Encode(),
            "timing report generation changed");
        bindings.push_back(ref);
        D actual(D::Read(ref, 262144));
        Require(actual.Text("status") == "pass", "timing report did not pass");
        if (kind == "timing-report")
          Require(actual.Text("schema") == "emuflow.phase2-report/v1" &&
                      actual.Text("stage_implementation_id") ==
                          "native-phase2-v1",
                  "native timing report identity disagrees");
        else
          Require(actual.Text("schema") ==
                          "emuflow.prepared-timing-summary/v1" &&
                      actual.Text("implementation_id") ==
                          "native-frontend-input-v1",
                  "prepared timing report identity disagrees");
        // pack() wraps an object. Keep its map body while adding annotations.
        auto packed = D::Array(pack(pack, kind == "timing-report"
                                              ? actual.text()
                                              : actual.Token("summary")),
                               2);
        report = D(packed[1]).Fields();
      }
      const bool provider = Boolean("$.state[1].timing_driven");
      D raw(projection.Token("effective_route_constraints") == "null"
                ? "{}"
                : projection.Token("effective_route_constraints"));
      const bool hop = provider && raw.Has("max_route_hops") &&
                       raw.Token("max_route_hops") != "null";
      std::vector<JsonRecord> consumers;
      if (provider)
        consumers.emplace_back("phase3-provider");
      if (hop)
        consumers.emplace_back("phase3-hop-refinement");
      report["partition_weights_applied"] = provider || hop ? "true" : "false";
      report["partition_provider_weights_applied"] =
          provider ? "true" : "false";
      report["hop_refinement_weights_applied"] = hop ? "true" : "false";
      report["partition_weight_consumers"] =
          "[\"list\"," + JsonRecord(consumers).Encode() + "]";
      state["timing_report"] = "[\"map\"," + D::Encode(report) + "]";
    }
    state["phase3_root"] = path(root / "partition");
    state["native_phase3_request"] = path(policy.path());
    const auto &g = policy.generation();
    std::vector<JsonRecord> values;
    for (const auto &v :
         {std::to_string(g.device), std::to_string(g.inode),
          std::to_string(g.bytes), std::to_string(g.modified_seconds),
          std::to_string(g.modified_nanoseconds),
          std::to_string(g.changed_seconds),
          std::to_string(g.changed_nanoseconds)})
      values.emplace_back(v);
    state["native_phase3_generation"] =
        JsonRecord(std::vector<JsonRecord>{"tuple", values}).Encode();
    state.erase("native_phase3_configuration");
    state.erase("native_phase3_configuration_generation");
    state.erase("checked_link_timing");
    state.erase("checked_link_timing_validation");
    top["state"] = "[\"map\"," + D::Encode(state) + "]";
    top["stage"] = JsonQuote("partition-policy");
    return D::Encode(top);
  }
  ArtifactRef PartitionRequest() {
    auto result=ArtifactRef::Bind(StatePath("native_phase3_request"),"phase3-stage-request-v1");
    const auto& g=result.generation();const std::string fields[]={std::to_string(g.device),std::to_string(g.inode),std::to_string(g.bytes),std::to_string(g.modified_seconds),std::to_string(g.modified_nanoseconds),std::to_string(g.changed_seconds),std::to_string(g.changed_nanoseconds)};
    Require(String("$.state[1].native_phase3_generation[0]")=="tuple","native partition request binding is invalid");
    for(unsigned i=0;i<7;++i)Require(String("$.state[1].native_phase3_generation[1]["+std::to_string(i)+"]")==fields[i],"native partition request generation changed");
    return result;
  }
  ArtifactRef Phase4Policy() {
    auto result=ArtifactRef::Bind(StatePath("native_phase4_policy"),"phase4-policy-v1");
    const auto& g=result.generation();const std::string fields[]={std::to_string(g.device),std::to_string(g.inode),std::to_string(g.bytes),std::to_string(g.modified_seconds),std::to_string(g.modified_nanoseconds),std::to_string(g.changed_seconds),std::to_string(g.changed_nanoseconds)};
    Require(String("$.state[1].native_phase4_generation[0]")=="tuple","native Phase 4 policy binding is invalid");
    for(unsigned i=0;i<7;++i)Require(String("$.state[1].native_phase4_generation[1]["+std::to_string(i)+"]")==fields[i],"native Phase 4 policy generation changed");
    return result;
  }
  std::string Phase4State(const std::string& reference) {
    auto row=db_.Prepare("SELECT json_remove(json_set(?1,'$.stage','route','$.state[1].phase4_report',json(?2)),'$.state[1].native_phase4_policy','$.state[1].native_phase4_generation')");
    row.BindText(1,data_);row.BindText(2,reference);Require(row.Step(),"cannot construct native Phase 4 compiler state");return row.TypedText(0);
  }
  ArtifactRef Phase6Policy() {
    auto result=ArtifactRef::Bind(StatePath("native_phase6_policy"),"phase6-compile-policy-v1");
    const auto& g=result.generation();const std::string fields[]={std::to_string(g.device),std::to_string(g.inode),std::to_string(g.bytes),std::to_string(g.modified_seconds),std::to_string(g.modified_nanoseconds),std::to_string(g.changed_seconds),std::to_string(g.changed_nanoseconds)};
    Require(String("$.state[1].native_phase6_generation[0]")=="tuple","native Phase 6 policy binding is invalid");
    for(unsigned i=0;i<7;++i)Require(String("$.state[1].native_phase6_generation[1]["+std::to_string(i)+"]")==fields[i],"native Phase 6 policy generation changed");
    return result;
  }
  ArtifactRef PhysicalPolicy() {
    auto result=ArtifactRef::Bind(StatePath("native_physical_policy"),"physical-flow-policy-v1");
    const auto& g=result.generation();const std::string fields[]={std::to_string(g.device),std::to_string(g.inode),std::to_string(g.bytes),std::to_string(g.modified_seconds),std::to_string(g.modified_nanoseconds),std::to_string(g.changed_seconds),std::to_string(g.changed_nanoseconds)};
    Require(String("$.state[1].native_physical_generation[0]")=="tuple","native physical policy binding is invalid");
    for(unsigned i=0;i<7;++i)Require(String("$.state[1].native_physical_generation[1]["+std::to_string(i)+"]")==fields[i],"native physical policy generation changed");
    return result;
  }
  std::string PhysicalState(const std::string& reference,const fs::path& root) {
    auto q=db_.Prepare("SELECT json_remove(json_set(?1,'$.stage','physical','$.state[1].physical_report',json(?2),'$.state[1].physical_summary_path',json(?3)),'$.state[1].native_physical_policy','$.state[1].native_physical_generation')");
    q.BindText(1,data_);q.BindText(2,reference);q.BindText(3,"[\"path\","+JsonQuote((root/"physical-summary.json").string())+"]");
    Require(q.Step(),"cannot construct native physical compiler state");return q.TypedText(0);
  }
  std::string PartitionState(const std::string& reference,const fs::path& root,std::uint64_t cuts) {
    auto option=db_.Prepare("SELECT json_type(?1,'$.state[1].optimize_frame_slots')");option.BindText(1,data_);Require(option.Step() && (option.Text(0)=="true" || option.Text(0)=="false"),"invalid frame search policy");
    const auto frame=cuts && option.Text(0)=="true"?"true":"false";
    auto row=db_.Prepare("SELECT json_remove(json_set(?1,'$.stage','partition','$.state[1].phase3_report',json(?2),'$.state[1].phase3_root',json(?3),'$.state[1].assignment_path',json(?4),'$.state[1].has_inter_fpga_cuts',json(?5),'$.state[1].effective_optimize_frame_slots',json(?6)),'$.state[1].native_phase3_request','$.state[1].native_phase3_generation')");
    row.BindText(1,data_);row.BindText(2,reference);row.BindText(3,"[\"path\","+JsonQuote(root.string())+"]");row.BindText(4,"[\"path\","+JsonQuote((root/"assignment.json").string())+"]");row.BindText(5,cuts?"true":"false");row.BindText(6,frame);Require(row.Step(),"cannot construct native partition compiler state");return row.TypedText(0);
  }
  std::string FrontendState(const std::string& reference,const fs::path& root,const std::string& timing="null") {
    auto row=db_.Prepare("SELECT json_remove(json_set(?1,'$.stage','frontend','$.state[1].frontend_report',json(?2),'$.state[1].phase1_root',json(?3),'$.state[1].ir_path',json(?4),'$.state[1].timing_report',json(?5),'$.state[1].native_prepared_timing_summary',json(CASE WHEN ?5='null' THEN 'null' ELSE json_array('path',json_extract(?5,'$[1].path')) END),'$.state[1].native_prepared_timing_generation',json(CASE WHEN ?5='null' THEN 'null' ELSE json_array('tuple',json_extract(?5,'$[1].generation')) END)),'$.state[1].native_frontend_request','$.state[1].native_frontend_generation','$.state[1].native_frontend_input_request','$.state[1].native_frontend_input_generation')");
    row.BindText(1,data_);row.BindText(2,reference);
    row.BindText(3,"[\"path\","+JsonQuote((root/"phase1").string())+"]");
    row.BindText(4,"[\"path\","+JsonQuote((root/"phase1/design.emuir.sqlite").string())+"]");
    row.BindText(5,timing);Require(row.Step(),"cannot construct native frontend compiler state");return row.TypedText(0);
  }
  void Envelope() {
    auto fields = db_.Prepare("SELECT count(*),sum(key IN ('schema','python_executable',"
        "'python_source','working_directory','options')) FROM json_each(?1)"); fields.BindText(1,data_);
    Require(fields.Step() && fields.Integer(0) == 5 && fields.Integer(1) == 5,
            "unknown native compiler request fields");
    Require(String("$.schema") == "emuflow.native-compile/v1","unsupported native compiler request schema");
  }
  bool Generation(const FileGeneration& expected) {
    const std::string fields[] = {std::to_string(expected.device),std::to_string(expected.inode),
      std::to_string(expected.bytes),std::to_string(expected.modified_seconds),std::to_string(expected.modified_nanoseconds),
      std::to_string(expected.changed_seconds),std::to_string(expected.changed_nanoseconds)};
    auto size = db_.Prepare("SELECT json_array_length(?1,'$.request_generation')"); size.BindText(1,data_);
    if (!size.Step() || size.Integer(0) != 7) return false;
    for (unsigned i = 0; i < 7; ++i) {
      if (String("$.request_generation[" + std::to_string(i) + "]") != fields[i]) return false;
    }
    return true;
  }
  bool StateShape() {
    auto shape = db_.Prepare("SELECT json_type(?1,'$.state'),json_array_length(?1,'$.state'),"
        "json_extract(?1,'$.state[0]'),json_type(?1,'$.state[1]')"); shape.BindText(1,data_);
    return shape.Step() && shape.Text(0) == "array" && shape.Integer(1) == 2 &&
        shape.Text(2) == "map" && shape.Text(3) == "object";
  }
  std::string Object(const std::string& path) {
    auto value = db_.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");
    value.BindText(1,data_); value.BindText(2,path);
    Require(value.Step() && value.Text(0) == "object","native compiler requires object field " + path);
    return value.Text(1);
  }
  std::string StatePath(const std::string& name) {
    const auto path="$.state[1]."+name;
    Require(String(path+"[0]")=="path","native compiler requires a path binding for "+name);
    const auto value=String(path+"[1]");Require(fs::path(value).is_absolute(),"native compiler state path is relative");return value;
  }
  bool OutputGeneration(const ArtifactRef& output, std::size_t index=0) {
    const auto prefix="$.outputs["+std::to_string(index)+"]";
    if(String(prefix+".schema")!=output.schema() || String(prefix+".path")!=output.path())return false;
    const auto& g=output.generation();
    const std::pair<const char*,std::int64_t> values[]={{"device",std::int64_t(g.device)},{"inode",std::int64_t(g.inode)},{"bytes",std::int64_t(g.bytes)},{"mtime_seconds",g.modified_seconds},{"mtime_nanoseconds",g.modified_nanoseconds},{"ctime_seconds",g.changed_seconds},{"ctime_nanoseconds",g.changed_nanoseconds}};
    for(const auto& item:values) {
      auto row=db_.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");row.BindText(1,data_);row.BindText(2,(prefix+".generation.")+item.first);
      if(!row.Step() || row.Text(0)!="integer" || row.TypedInteger(1)!=item.second)return false;
    }return true;
  }
  ArtifactRef ComparisonInput() {
    auto result=ArtifactRef::Bind(StatePath("native_phase6_comparison_input"),"phase6-comparison-input-v1");
    const auto& g=result.generation();const std::string fields[]={std::to_string(g.device),std::to_string(g.inode),std::to_string(g.bytes),std::to_string(g.modified_seconds),std::to_string(g.modified_nanoseconds),std::to_string(g.changed_seconds),std::to_string(g.changed_nanoseconds)};
    Require(String("$.state[1].native_phase6_comparison_generation[0]")=="tuple","native comparison input binding is invalid");
    for(unsigned i=0;i<7;++i)Require(String("$.state[1].native_phase6_comparison_generation[1]["+std::to_string(i)+"]")==fields[i],"native comparison input generation changed");return result;
  }
  std::string Phase5State(const std::string& reference) {
    auto row=db_.Prepare("SELECT json_set(?1,'$.stage','tdm','$.state[1].phase5_report',json(?2))");row.BindText(1,data_);row.BindText(2,reference);Require(row.Step(),"cannot construct native Phase 5 compiler state");return row.TypedText(0);
  }
  std::string Phase6State(const std::string& reference,const fs::path& root) {
    const auto path=[](const fs::path& value){return "[\"path\","+JsonQuote(value.string())+"]";};
    auto q=db_.Prepare("SELECT json_remove(json_set(?1,'$.stage','split','$.state[1].phase6_report',json(?2),'$.state[1].phase6_root',json(?3),'$.state[1].routes_path',json(?4),'$.state[1].schedule_path',json(?5),'$.state[1].baseline_physical_report',NULL,'$.state[1].baseline_runtime_report',NULL,'$.state[1].phase6_comparison',NULL,'$.state[1].effective_physical_architecture',json_extract(?1,'$.state[1].physical_architecture')),'$.state[1].native_phase6_policy','$.state[1].native_phase6_generation')");
    q.BindText(1,data_);q.BindText(2,reference);q.BindText(3,path(root));
    q.BindText(4,path(fs::path(StatePath("phase4_root"))/"routes.json"));
    q.BindText(5,path(fs::path(StatePath("phase5_root"))/"schedule.json"));
    Require(q.Step(),"cannot construct native Phase 6 compiler state");return q.TypedText(0);
  }
  std::string SignoffState(const std::string& reference,const fs::path& root) {
    const auto path=[](const fs::path& value){return "[\"path\","+JsonQuote(value.string())+"]";};
    auto q=db_.Prepare("SELECT json_remove(json_set(?1,'$.stage','signoff','$.state[1].runtime_report',json(?2),'$.state[1].runtime_root',json(?3)),'$.state[1].native_phase6_comparison_input','$.state[1].native_phase6_comparison_generation','$.state[1].phase6_comparison','$.state[1].baseline_physical_report','$.state[1].baseline_runtime_report')");
    q.BindText(1,data_);q.BindText(2,reference);q.BindText(3,path(root));
    Require(q.Step(),"cannot construct native Phase 7C compiler state");return q.TypedText(0);
  }
  std::string AdvanceState(const std::string& stage) {
    auto q=db_.Prepare("SELECT json_set(?1,'$.stage',?2)");q.BindText(1,data_);q.BindText(2,stage);
    Require(q.Step(),"cannot advance native compiler state");
    if(stage=="timing") {
      auto clean=db_.Prepare("SELECT json_remove(?,'$.state[1].native_prepared_timing_summary','$.state[1].native_prepared_timing_generation')");clean.BindText(1,q.TypedText(0));Require(clean.Step(),"cannot retire native prepared timing binding");return clean.TypedText(0);
    }
    return q.TypedText(0);
  }
  void RequirePublishedPaths(const fs::path& temporary) {
    const auto root = fs::weakly_canonical(temporary);
    const auto check = [&](const fs::path& path) {
      const auto relative = path.lexically_relative(root);
      Require(relative.empty() || *relative.begin() == "..",
              "native compiler state references a phase temporary path");
    };
    auto paths = db_.Prepare("SELECT atom FROM json_tree(?1,'$.state') WHERE type='text'");
    paths.BindText(1,data_);
    while (paths.Step()) {
      const fs::path path(paths.Text(0));
      if (!path.is_absolute()) continue;
      check(path.lexically_normal());
      std::error_code error;
      const auto canonical = fs::weakly_canonical(path,error);
      // Control strings also include diagnostic text, not only file paths.
      // Do not treat an unresolvable string as a filesystem lookup failure.
      if (!error) check(canonical);
    }
  }
 private:
  SqliteDatabase db_;
  std::string data_;
};
struct Scratch {
  fs::path root;
  explicit Scratch(const fs::path& parent) {
    root = parent / (".emuflow-compile-" + std::to_string(getpid()) + "-" +
        std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
    Require(fs::create_directory(root),"native compiler scratch already exists");
    fs::create_directory(root / "tmp");
  }
  ~Scratch() { std::error_code error; fs::remove_all(root,error); }
};
void ClaimOutput(const fs::path& output, CompileResult& result) {
  const auto owner = (output / ".native-compile-owner").string();
  const int fd = open(owner.c_str(),O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC,0600);
  Require(fd >= 0,"native compiler output is already owned");
  const auto count = write(fd,result.run_id.data(),result.run_id.size());
  const auto synced = fsync(fd); close(fd);
  if (count != static_cast<ssize_t>(result.run_id.size()) || synced != 0) {
    fs::remove(owner);
    throw StageError(ErrorCode::kPublicationFailed,"cannot claim native compiler output");
  }
  result.owns_output = true;
}
void RequireOwner(const CompileResult& result) {
  std::ifstream owner(fs::path(result.output_directory) / ".native-compile-owner");
  std::string run_id; owner >> run_id;
  Require(run_id == result.run_id,"native compiler output owner changed");
}
}  // namespace

CompileResult Compile(const ArtifactRef& configuration,const ArtifactRef& native_executable, const volatile std::sig_atomic_t* cancelled) {
  CompileResult result;
  std::string python, python_source, adapter, working_directory;
  result.operation = RunStage("compile", [&] {
    Require(native_executable.schema()=="external-tool-executable-v1","invalid native compiler executable schema");
    Require(configuration.schema() == "native-compile-request-v1","invalid native compiler artifact schema");
    configuration.RequireUnchanged();
    ControlJson request(configuration.path(),1024 * 1024); request.Envelope();
    result.request_configuration = request.Object("$");
    python = request.String("$.python_executable"); python_source = request.String("$.python_source");
    auto output = request.String("$.options.output_dir");
    working_directory = request.String("$.working_directory");
    Require(fs::path(python).is_absolute() && fs::path(python_source).is_absolute() && fs::path(output).is_absolute(),
            "native compiler executable, source and output paths must be absolute");
    Require(fs::is_directory(python_source),"native compiler Python source directory is unavailable");
    Require(fs::path(working_directory).is_absolute() && fs::is_directory(working_directory),
            "native compiler invocation directory is unavailable or not absolute");
    adapter = (fs::path(python_source) / "emuflow/flow/native_compile_adapter.py").string();
    result.output_directory = fs::weakly_canonical(output).string();
    Require(!fs::exists(output) || (fs::is_directory(output) && fs::is_empty(output)),
            "native compiler output must be an empty directory");
    return std::vector<ArtifactRef>{configuration,ArtifactRef::Bind(adapter,"native-compile-adapter-v1"),
                                   ArtifactRef::Bind(python,"external-tool-executable-v1"),
                                   native_executable};
  }, [&](const auto& inputs) {
    const auto output = fs::path(result.output_directory);
    fs::create_directories(output.parent_path());
    Scratch scratch(output.parent_path());
    const auto run_id = scratch.root.filename().string();
    result.run_id = run_id;
    fs::path previous;
    for (const auto* stage : stages) {
      if (cancelled && *cancelled) throw StageError(ErrorCode::kExecutionFailed,"native compilation cancelled");
      if (result.owns_output) RequireOwner(result);
      for (const auto& input : inputs) input.RequireUnchanged();
      Require(fs::canonical(python) == inputs[2].path(),"native compiler executable alias changed");
      if(std::string(stage)=="timing-policy") {
        const auto root=output/".native-frontend-input-request";
        Require(!fs::is_symlink(root),"native frontend input directory was replaced");
        std::error_code error;fs::remove_all(root,error);Require(!error,"cannot retire native frontend input request");
      }
      if(std::string(stage)=="route-policy") {
        const auto private_configuration=output/".native-phase3-request";
        Require(!fs::is_symlink(private_configuration),"native partition configuration directory was replaced");
        std::error_code cleanup_error;fs::remove_all(private_configuration,cleanup_error);Require(!cleanup_error,"cannot retire native partition configuration");
      }
      if(std::string(stage)=="partition-policy") {
        const auto root=output/".native-phase2-request";Require(!fs::is_symlink(root),"native Phase 2 policy directory was replaced");
        std::error_code error;fs::remove_all(root,error);Require(!error,"cannot retire native Phase 2 policy");
      }
      if(std::string(stage)=="tdm") {
        const auto root=output/".native-phase4-request";Require(!fs::is_symlink(root),"native Phase 4 policy directory was replaced");
        std::error_code error;fs::remove_all(root,error);Require(!error,"cannot retire native Phase 4 policy");
      }
      if(std::string(stage)=="physical") {
        const auto policy_root=output/".native-phase6-request";
        Require(!fs::is_symlink(policy_root),"native Phase 6 policy directory was replaced");
        std::error_code error;fs::remove_all(policy_root,error);Require(!error,"cannot retire native Phase 6 policy");
      }
      if(std::string(stage)=="timing-policy") {
        ControlJson prior(previous.string());
        Require(prior.String("$.stage")=="frontend","invalid native timing configuration predecessor");
        auto predecessor=ArtifactRef::Bind(previous.string(),"native-compile-state-v1");
        const bool prepare=prior.Boolean("$.state[1].internal_timing_database") && !prior.Has("$.state[1].prepared_timing");
        std::optional<ArtifactRef> settings,emuir,policy;
        const auto next=scratch.root/"timing-policy.json",policy_root=output/".native-phase2-request";
        const auto step=RunStage("timing-policy",[&]{
            auto refs=std::vector<ArtifactRef>{predecessor};
            if(prepare){
              Require(prior.Has("$.state[1].native_phase2_configuration"),"native timing configuration policy is missing; Python fallback is forbidden");
              settings=prior.Phase2Configuration();
              Require(fs::path(settings->path())==fs::weakly_canonical(output/".native-phase2-configuration/request.json"),"native timing configuration escapes compiler ownership");
              emuir=ArtifactRef::Bind(prior.StatePath("ir_path"),"emuir-compact-v2");
              Require(fs::path(prior.StatePath("ir_path"))==output/"frontend/phase1/design.emuir.sqlite","native timing IR escapes frontend ownership");
              refs.push_back(*settings);refs.push_back(*emuir);
            }else Require(!prior.Has("$.state[1].native_phase2_configuration"),"unexpected timing configuration for adopted/external timing");
            return refs;
          },[&](const auto& sources){
            if(prepare){
              Require(!fs::exists(policy_root) && !fs::is_symlink(policy_root),"native timing policy output is not fresh");fs::create_directory(policy_root);
              const auto made=PreparePhase2Policy(*settings,(policy_root/"phase2-policy.json").string(),cancelled,&*emuir);
              if(!made.ok()){std::error_code cleanup;fs::remove_all(policy_root,cleanup);throw StageError(made.error,made.message);}
              Require(made.stage=="phase2.prepare-policy" && made.outputs.size()==1,"native timing configuration producer identity disagrees");
              policy=ArtifactRef::Bind((policy_root/"phase2-policy.json").string(),"phase2-policy-v1");
              Require(made.outputs[0].path()==policy->path() && made.outputs[0].generation()==policy->generation(),"native timing configuration output generation changed");
            }
            auto bindings=sources;if(policy)bindings.push_back(*policy);
            const auto state=prior.TimingPolicyState(output,policy);
            PublishTextArtifact(next.string(),bindings,[&](std::ostream& stream){stream<<state<<'\n';});
          },[&]{return std::vector<ArtifactRef>{ArtifactRef::Bind(next.string(),"native-compile-state-v1")};});
        result.steps.push_back({stage,step.ok(),prepare?"native-phase2-configuration-v1":"native-timing-policy-boundary-v1",step.metrics.wall_seconds,step.metrics.user_cpu_seconds,step.metrics.system_cpu_seconds,step.metrics.process_peak_rss_kib});
        if(!step.ok())throw StageError(step.error,"native compile timing configuration: "+step.message);
        ControlJson state(next.string());
        Require(state.String("$.stage")=="timing-policy" && state.String("$.run_id")==run_id && state.String("$.status")=="pass" && state.StateShape() && state.Generation(configuration.generation()),"invalid native timing configuration state");
        const auto config_root=output/".native-phase2-configuration";Require(!fs::is_symlink(config_root),"native timing configuration directory was replaced");
        std::error_code error;fs::remove_all(config_root,error);Require(!error,"cannot retire native timing configuration");
        fs::remove(previous);previous=next;
        std::cout<<"COMPILE_STAGE_COMPLETE timing-policy "<<step.metrics.wall_seconds<<"s\n"<<std::flush;
        continue;
      }
      if(std::string(stage)=="partition-policy") {
        ControlJson prior(previous.string());Require(prior.String("$.stage")=="timing","invalid native partition configuration predecessor");
        const auto predecessor=ArtifactRef::Bind(previous.string(),"native-compile-state-v1");
        std::optional<ArtifactRef> settings;
        std::map<std::string,ArtifactRef> runtime;
        const auto next=scratch.root/"partition-policy.json",policy_root=output/".native-phase3-request";
        const auto step=RunStage("partition-policy",[&]{
          Require(prior.Has("$.state[1].native_phase3_configuration"),"native partition configuration policy is missing; Python fallback is forbidden");settings=prior.Phase3Configuration();Require(fs::path(settings->path())==fs::weakly_canonical(output/".native-phase3-configuration/request.json"),"native partition configuration escapes compiler ownership");
          Require(fs::path(prior.StatePath("ir_path"))==output/"frontend/phase1/design.emuir.sqlite","native partition IR escapes frontend ownership");runtime.emplace("source",ArtifactRef::Bind(prior.StatePath("ir_path"),"emuir-compact-v2"));
          const auto payload=[&](const char* state,const char* role,const char* schema,const char* payload_role,const char* payload_schema,const char* member){
            auto selector=ArtifactRef::Bind(prior.StatePath(state),schema);ControlJson manifest(selector.path());const auto relative=manifest.String(std::string("$.payloads.")+member+".path");Require(fs::path(relative).filename()==relative,"native partition payload escapes selector");runtime.emplace(role,selector);runtime.emplace(payload_role,ArtifactRef::Bind((fs::path(selector.path()).parent_path()/relative).string(),payload_schema));
          };
          if(prior.String("$.state[1].partition_provider")=="patron")payload("path_database_path","timing_selector","sta-path-selector-v1","timing","timing-path-store-v1","paths");
          if(prior.Boolean("$.state[1].timing_driven"))payload("net_weights_path","weight_selector","net-weight-selector-v2","weights","net-weight-store-v1","weights");
          std::vector<ArtifactRef> refs{predecessor,*settings};for(const auto& item:runtime)refs.push_back(item.second);return refs;
        },[&](const auto& sources){
          const auto made=PreparePhase3Policy(*settings,policy_root.string(),cancelled,runtime);if(!made.ok())throw StageError(made.error,made.message);Require(made.stage=="phase3.prepare-policy" && made.outputs.size()==3,"native partition configuration producer identity disagrees");
          Require(fs::path(made.outputs[0].path())==policy_root/"phase3-request.json" && fs::path(made.outputs[2].path())==policy_root/"configuration-summary.json","native partition configuration output escapes ownership");auto bindings=sources;bindings.insert(bindings.end(),made.outputs.begin(),made.outputs.end());const auto state=prior.PartitionPolicyState(output,made.outputs[0],made.outputs[2],runtime,bindings);PublishTextArtifact(next.string(),bindings,[&](std::ostream& stream){stream<<state<<'\n';});
        },[&]{return std::vector<ArtifactRef>{ArtifactRef::Bind(next.string(),"native-compile-state-v1")};});
        result.steps.push_back({stage,step.ok(),"native-phase3-configuration-v1",step.metrics.wall_seconds,step.metrics.user_cpu_seconds,step.metrics.system_cpu_seconds,step.metrics.process_peak_rss_kib});if(!step.ok())throw StageError(step.error,"native compile partition configuration: "+step.message);
        ControlJson state(next.string());Require(state.String("$.stage")=="partition-policy" && state.String("$.run_id")==run_id && state.String("$.status")=="pass" && state.StateShape() && state.Generation(configuration.generation()),"invalid native partition configuration state");const auto config_root=output/".native-phase3-configuration";Require(!fs::is_symlink(config_root),"native partition configuration directory was replaced");std::error_code error;fs::remove_all(config_root,error);Require(!error,"cannot retire native partition configuration");fs::remove(previous);previous=next;std::cout<<"COMPILE_STAGE_COMPLETE partition-policy "<<step.metrics.wall_seconds<<"s\n"<<std::flush;continue;
      }
      if(std::string(stage)=="route-policy") {
        const auto next=scratch.root/"route-policy.json";
        const auto step=PrepareRouting(ArtifactRef::Bind(previous.string(),"native-compile-state-v1"),output.string(),next.string(),cancelled);
        result.steps.push_back({stage,step.ok(),"native-routing-preparation-v1",step.metrics.wall_seconds,step.metrics.user_cpu_seconds,step.metrics.system_cpu_seconds,step.metrics.process_peak_rss_kib});
        if(!step.ok())throw StageError(step.error,"native compile route preparation: "+step.message);
        ControlJson state(next.string());Require(state.String("$.stage")=="route-policy" && state.String("$.run_id")==run_id && state.String("$.status")=="pass" && state.StateShape() && state.Generation(configuration.generation()),"invalid native routing preparation state");
        for(const auto* name:{".native-phase4-configuration",".native-phase6-configuration"}){const auto path=output/name;Require(!fs::is_symlink(path),"native routing preparation directory was replaced");std::error_code error;fs::remove_all(path,error);Require(!error,"cannot retire native routing preparation inputs");}
        fs::remove(previous);previous=next;std::cout<<"COMPILE_STAGE_COMPLETE route-policy "<<step.metrics.wall_seconds<<"s\n"<<std::flush;continue;
      }
      if(std::string(stage)=="finalize") {
        auto prior=ArtifactRef::Bind(previous.string(),"native-compile-state-v1");
        const auto step=RunStage("finalize",[&]{return std::vector<ArtifactRef>{prior};},
          [&](const auto& sources){FinalizeFlow(output);for(const auto& source:sources)source.RequireUnchanged();},
          [&]{return std::vector<ArtifactRef>{ArtifactRef::Bind(
              (output/"multi-fpga-flow-report.json").string(),"multi-fpga-flow-report-v2")};});
        result.steps.push_back({stage,step.ok(),"native-flow-finalize-v1",step.metrics.wall_seconds,
          step.metrics.user_cpu_seconds,step.metrics.system_cpu_seconds,step.metrics.process_peak_rss_kib});
        if(!step.ok())throw StageError(step.error,"native compile finalize: "+step.message);
        fs::remove(previous);previous.clear();
        std::cout<<"COMPILE_STAGE_COMPLETE finalize "<<step.metrics.wall_seconds<<"s\n"<<std::flush;
        continue;
      }
      if(std::string(stage)=="physical") {
        ControlJson prior(previous.string());
        const bool native_no_physical=prior.String("$.state[1].phase6_provider")=="baseline" &&
            !prior.Boolean("$.state[1].physical");
        if(native_no_physical) {
          Require(prior.String("$.stage")=="split","invalid native nonphysical predecessor");
          const auto next=scratch.root/"physical.json";auto bound=ArtifactRef::Bind(previous.string(),"native-compile-state-v1");
          const auto step=RunStage("physical",[&]{return std::vector<ArtifactRef>{bound};},
            [&](const auto& sources){for(const auto& source:sources)source.RequireUnchanged();},
            []{return std::vector<ArtifactRef>{};});
          result.steps.push_back({stage,step.ok(),"native-no-physical-stage-v1",step.metrics.wall_seconds,
            step.metrics.user_cpu_seconds,step.metrics.system_cpu_seconds,step.metrics.process_peak_rss_kib});
          if(!step.ok())throw StageError(step.error,"native compile physical boundary: "+step.message);
          const auto state=prior.AdvanceState("physical");
          PublishTextArtifact(next.string(),{bound},[&](std::ostream& stream){stream<<state<<'\n';});
          auto state_ref=ArtifactRef::Bind(next.string(),"native-compile-state-v1");ControlJson state_control(state_ref.path());
          Require(state_control.String("$.stage")=="physical" && state_control.String("$.run_id")==run_id &&
              state_control.String("$.status")=="pass" && state_control.StateShape() && state_control.Generation(configuration.generation()),
              "invalid native nonphysical compiler state");
          fs::remove(previous);previous=next;
          std::cout<<"COMPILE_STAGE_COMPLETE physical "<<step.metrics.wall_seconds<<"s\n"<<std::flush;
          continue;
        }
      }
      if(std::string(stage)=="signoff") {
        const auto policy_root=output/".native-physical-request";Require(!fs::is_symlink(policy_root),"native physical policy directory was replaced");
        std::error_code cleanup;fs::remove_all(policy_root,cleanup);Require(!cleanup,"cannot retire native physical policy");
        ControlJson prior(previous.string());
        const bool physical_signoff=prior.Boolean("$.state[1].physical");
        const auto provider=prior.String("$.state[1].phase6_provider");
        Require(provider=="baseline"||provider=="chimew","unsupported native signoff provider");
        const bool comparison=provider=="chimew";
        {
          Require(prior.String("$.stage")=="physical","invalid native Phase 7C predecessor");
          const auto next=scratch.root/"signoff.json";auto bound=ArtifactRef::Bind(previous.string(),"native-compile-state-v1");
          std::optional<ArtifactRef> comparison_input;
          if(comparison){Require(physical_signoff,"Chimew signoff requires physical results");comparison_input=prior.ComparisonInput();Require(fs::path(comparison_input->path())==output/"phase6-comparison/comparison-input.json","comparison input escapes compiler ownership");}
          const auto step=RunStage("signoff",[&]{auto refs=std::vector<ArtifactRef>{bound};if(comparison_input)refs.push_back(*comparison_input);return refs;},
            [&](const auto& sources){
              if(physical_signoff){
                SignoffPhysical(output,prior.String("$.state[1].global_sta_executable"),cancelled,
                    prior.Has("$.state[1].global_sta_launcher_reference")?prior.StatePath("global_sta_launcher_reference"):"");
                Require(ControlJson((output/"runtime/phase7c_report.json").string()).String("$.status")=="pass",
                    "full physical flow did not close unified Phase 7C system timing");
              }else SignoffNonphysical(output);
              if(comparison_input)FinalizePhase6Comparison(*comparison_input,output);
              for(const auto& source:sources)source.RequireUnchanged();
            },
            [&]{return std::vector<ArtifactRef>{ArtifactRef::Bind(
                (output/"runtime/phase7c_report.json").string(),"exact-sha256-text-v1")};});
          result.steps.push_back({stage,step.ok(),physical_signoff?"native-physical-phase7c-v1":"native-nonphysical-phase7c-v1",step.metrics.wall_seconds,
            step.metrics.user_cpu_seconds,step.metrics.system_cpu_seconds,step.metrics.process_peak_rss_kib});
          if(!step.ok())throw StageError(step.error,"native compile signoff: "+step.message);
          auto published=ArtifactRef::Bind((output/"runtime/phase7c_report.json").string(),"exact-sha256-text-v1");
          const auto& g=published.generation();
          const auto reference=JsonRecord(std::vector<JsonRecord>{JsonRecord("runtime-report"),JsonRecord(std::map<std::string,JsonRecord>{
            {"schema",published.schema()},{"path",published.path()},{"generation",std::vector<JsonRecord>{g.device,g.inode,g.bytes,g.modified_seconds,g.modified_nanoseconds,g.changed_seconds,g.changed_nanoseconds}}})}).Encode();
          auto bindings=std::vector<ArtifactRef>{bound,published};const auto state=prior.SignoffState(reference,output/"runtime");
          PublishTextArtifact(next.string(),bindings,[&](std::ostream& stream){stream<<state<<'\n';});
          auto state_ref=ArtifactRef::Bind(next.string(),"native-compile-state-v1");ControlJson state_control(state_ref.path());
          Require(state_control.String("$.stage")=="signoff" && state_control.String("$.run_id")==run_id &&
              state_control.String("$.status")=="pass" && state_control.StateShape() && state_control.Generation(configuration.generation()),
              "invalid native Phase 7C compiler state");
          const auto metrics=output/"stage-metrics/phase7c.json";fs::create_directories(metrics.parent_path());
          const auto text=JsonRecord(std::map<std::string,JsonRecord>{{"stage","phase7c"},{"status","pass"},
            {"wall_seconds",step.metrics.wall_seconds},{"peak_process_tree_rss_kib",step.metrics.process_peak_rss_kib},
            {"rss_measurement","in-process-native-owner-high-water"}}).Encode();
          PublishTextArtifact(metrics.string(),bindings,[&](std::ostream& stream){stream<<text<<'\n';});
          if(comparison_input)fs::remove(comparison_input->path());
          fs::remove(previous);previous=next;
          std::cout<<"COMPILE_STAGE_COMPLETE signoff "<<step.metrics.wall_seconds<<"s\n"<<std::flush;
          continue;
        }
      }
      const auto next = scratch.root / (std::string(stage) + ".json");
      const auto temporary = scratch.root / "tmp" / stage;
      fs::create_directory(temporary);
      ToolRequest tool;
      const bool native_physical=std::string(stage)=="physical" && ControlJson(previous.string()).Boolean("$.state[1].physical") && ControlJson(previous.string()).String("$.state[1].phase6_provider")=="baseline";
      const bool native_phase4=std::string(stage)=="route";
      const bool native_phase5=std::string(stage)=="tdm";
      const bool native_phase3=std::string(stage)=="partition";
      const bool native_phase2=std::string(stage)=="timing" && ControlJson(previous.string()).Has("$.state[1].native_phase2_policy");
      const bool native_phase6=std::string(stage)=="split" && ControlJson(previous.string()).String("$.state[1].phase6_provider")=="baseline";
      const bool native_frontend=std::string(stage)=="frontend";
      const bool frontend_inputs=native_frontend && ControlJson(previous.string()).Has("$.state[1].native_frontend_input_request");
      if(frontend_inputs)Require(!ControlJson(previous.string()).Has("$.state[1].native_frontend_request"),"multiple native frontend requests");
      if(native_frontend)Require(frontend_inputs || ControlJson(previous.string()).Has("$.state[1].native_frontend_request"),"native frontend request is missing; Python fallback is forbidden");
      if(std::string(stage)=="timing" && !native_phase2) {
        ControlJson prior(previous.string());
        Require(prior.Has("$.state[1].prepared_timing") || !prior.Boolean("$.state[1].internal_timing_database"),"native timing policy is missing; Python fallback is forbidden");
        const auto previous_ref=ArtifactRef::Bind(previous.string(),"native-compile-state-v1");
        std::vector<ArtifactRef> bindings{previous_ref};
        if(prior.Has("$.state[1].prepared_timing")) {
          const auto report=prior.PreparedTimingSummary();
          const auto path=report.path();
          Require(fs::path(path)==output/"timing/prepared-timing-summary.json","prepared timing report escapes compiler ownership");
          ControlJson envelope(path);Require(envelope.String("$.status")=="pass" && envelope.String("$.implementation_id")=="native-frontend-input-v1","prepared native timing was not validated");
          BoundedJson sealed(BoundedJson::Read(report));
          for(const auto& token:BoundedJson::Array(sealed.Token("sources"),16)) {
            BoundedJson row(token);const auto ref=ArtifactRef::Bind(row.Text("path"),row.Text("schema"));
            const auto& g=ref.generation();const auto generation=JsonRecord(std::vector<JsonRecord>{g.device,g.inode,g.bytes,g.modified_seconds,g.modified_nanoseconds,g.changed_seconds,g.changed_nanoseconds}).Encode();
            SqliteDatabase check(":memory:",SqliteDatabase::Mode::kReadOnly);RequireEquivalentJson(check,generation,row.Token("generation"),"prepared timing source generation changed");
            bindings.push_back(ref);
          }
          bindings.push_back(report);
        }
        const auto state=prior.AdvanceState("timing");
        PublishTextArtifact(next.string(),bindings,[&](std::ostream& stream){stream<<state<<'\n';});
        result.steps.push_back({stage,true,"native-timing-boundary-v1",0,0,0,0});
        fs::remove(previous);previous=next;
        std::error_code cleanup;fs::remove_all(temporary,cleanup);Require(!cleanup,"cannot retire timing boundary temporary directory");
        std::cout<<"COMPILE_STAGE_COMPLETE timing source-boundary\n"<<std::flush;
        continue;
      }
      std::string phase4_report,phase5_report,phase3_report,phase6_report,phase2_report,frontend_summary,physical_report;
      std::optional<ArtifactRef> frontend_request,partition_request,phase2_policy,phase4_policy,phase6_policy,physical_policy,physical_request;
      if(native_physical) {
        ControlJson prior(previous.string());Require(prior.String("$.stage")=="split","invalid native physical predecessor");
        physical_policy=prior.PhysicalPolicy();
        Require(fs::path(physical_policy->path())==output/".native-physical-request/physical-policy.json","native physical policy escapes compiler ownership");
        const auto bind=[&](const std::string& role,const std::string& path,const std::string& schema){return std::make_pair(role,ArtifactRef::Bind(path,schema));};
        const auto schedule=fs::path(prior.StatePath("phase5_root"));
        physical_request=PreparePhysicalFlowCompileRequest(*physical_policy,{
          bind("split-manifest",(output/"split/manifest.json").string(),"physical-input-document-v1"),
          bind("board",prior.StatePath("platform_path"),"boarddb-v1"),
          bind("schedule-selector",(schedule/"schedule.json").string(),"tdm-schedule-selector-v4"),
          bind("original",prior.StatePath("ir_path"),"emuir-compact-v2"),
          bind("assignment-selector",prior.StatePath("assignment_path"),"physical-input-document-v1"),
          bind("route-selector",(fs::path(prior.StatePath("phase4_root"))/"routes.json").string(),"system-route-selector-v1"),
          bind("path-selector",prior.StatePath("path_database_path"),"sta-path-selector-v1"),
          bind("schedule-board",(schedule/"schedule-board.bin").string(),"exact-schedule-board-request-v1"),
          bind("phase5-report",(schedule/"phase5_report.json").string(),"phase5-report-v1")},(temporary/"physical-request.json").string());
        physical_report=(output/"physical/multi-fpga-physical-flow-report.json").string();
        tool.argv={inputs[3].path(),"--report",(temporary/"native-result.json").string(),"physical","run-flow",physical_request->path(),(output/"physical").string()};
      } else if(native_frontend) {
        ControlJson prior(previous.string());Require(prior.String("$.stage")=="preflight","invalid native frontend predecessor");
        frontend_request=frontend_inputs?prior.FrontendInputRequest():prior.FrontendRequest();frontend_summary=(output/"frontend/phase1/frontend-summary.json").string();
        if(frontend_inputs)Require(fs::path(frontend_request->path())==output/".native-frontend-input-request/inputs.json","native frontend input request escapes compiler ownership");
        tool.argv={inputs[3].path(),"--report",(temporary/"native-result.json").string(),"phase1",frontend_inputs?"import-inputs":"run",frontend_request->path(),frontend_inputs?output.string():(output/"frontend/phase1").string()};
      } else if(native_phase2) {
        ControlJson prior(previous.string());Require(prior.String("$.stage")=="timing-policy","invalid native timing predecessor");
        phase2_policy=prior.Phase2Policy();
        Require(fs::path(phase2_policy->path())==output/".native-phase2-request/phase2-policy.json","native timing policy escapes compiler ownership");
        Require(fs::weakly_canonical(prior.StatePath("timing_root"))==output/"timing","native timing output escapes compiler ownership");
        phase2_report=(output/"timing/phase2_report.json").string();
        tool.argv={inputs[3].path(),"--report",(temporary/"native-result.json").string(),"phase2","run",prior.StatePath("ir_path"),phase2_policy->path(),(output/"timing").string()};
      } else if(native_phase3) {
        ControlJson prior(previous.string());Require(prior.String("$.stage")=="partition-policy","invalid native partition predecessor");
        partition_request=prior.PartitionRequest();
        Require(fs::path(partition_request->path())==output/".native-phase3-request/phase3-request.json","native partition configuration escapes compiler ownership");
        Require(fs::weakly_canonical(prior.StatePath("phase3_root"))==output/"partition","native partition output escapes compiler ownership");
        phase3_report=(output/"partition/phase3_report.json").string();
        tool.argv={inputs[3].path(),"--report",(temporary/"native-result.json").string(),"phase3","run",partition_request->path(),(output/"partition").string()};
      } else if(native_phase4) {
        ControlJson prior(previous.string());Require(prior.String("$.stage")=="route-policy","invalid native Phase 4 predecessor");
        phase4_policy=prior.Phase4Policy();
        Require(fs::path(phase4_policy->path())==output/".native-phase4-request/phase4-policy.json","native Phase 4 policy escapes compiler ownership");
        const auto root=fs::path(prior.StatePath("phase4_root"));Require(fs::weakly_canonical(root)==output/"system-route","native Phase 4 output escapes compiler ownership");
        phase4_report=(root/"phase4_report.json").string();
        tool.argv={inputs[3].path(),"--report",(temporary/"native-result.json").string(),"phase4","run",
          (fs::path(prior.StatePath("phase3_root"))/"assignment.sqlite").string(),prior.StatePath("platform_path"),phase4_policy->path(),root.string()};
        if(prior.Has("$.state[1].projected_timing_paths"))tool.argv.push_back(prior.StatePath("projected_timing_paths"));
      } else if(native_phase5) {
        ControlJson prior(previous.string());
        Require(prior.String("$.stage")=="route" && prior.String("$.state[1].effective_tdm_provider")=="deterministic-dependency-earliest-slot-v1","invalid native Phase 5 compiler predecessor/provider");
        const auto phase5_root=fs::path(prior.StatePath("phase5_root"));
        Require(fs::weakly_canonical(phase5_root)==output/"tdm","native Phase 5 output escapes compiler ownership");
        phase5_report=(phase5_root/"phase5_report.json").string();
        tool.argv={inputs[3].path(),"--report",(temporary/"native-result.json").string(),"phase5","compile",
            (fs::path(prior.StatePath("phase4_root"))/"routes.json").string(),
            (fs::path(prior.StatePath("phase3_root"))/"assignment.sqlite").string(),
            (fs::path(prior.StatePath("phase1_root"))/"platform.normalized.json").string(),phase5_report};
      } else if(native_phase6) {
        ControlJson prior(previous.string());Require(prior.String("$.stage")=="tdm","invalid native Phase 6 predecessor");
        phase6_policy=prior.Phase6Policy();
        Require(fs::path(phase6_policy->path())==output/".native-phase6-request/phase6-policy.json","native Phase 6 policy escapes compiler ownership");
        phase6_report=(output/"split/phase6_report.json").string();
        tool.argv={inputs[3].path(),"--report",(temporary/"native-result.json").string(),"phase6","compile",
          prior.StatePath("ir_path"),(fs::path(prior.StatePath("phase3_root"))/"assignment.sqlite").string(),
          (fs::path(prior.StatePath("phase5_root"))/"schedule.json").string(),prior.StatePath("platform_path"),
          phase6_policy->path(),(output/"split").string()};
      } else {
        tool.argv = {python,"-m","emuflow.flow.native_compile_adapter","--stage",stage,
                     "--request",configuration.path(),"--output",next.string(),"--run-id",run_id};
        if (!previous.empty()) { tool.argv.push_back("--previous"); tool.argv.push_back(previous.string()); }
      }
      tool.working_directory = working_directory;
      tool.environment = {{"PYTHONPATH",python_source},{"TMPDIR",temporary.string()},
          {"TMP",temporary.string()},{"TEMP",temporary.string()},{"SQLITE_TMPDIR",temporary.string()},
          {"XDG_CACHE_HOME",(temporary / "cache").string()}};
      tool.tail_bytes = 32768;
      // Adapters unwind nested stream/tool owners before leaving their group.
      tool.termination_grace_ms = 10000;
      auto step_inputs = inputs;
      if (!previous.empty()) step_inputs.push_back(ArtifactRef::Bind(previous.string(),"native-compile-state-v1"));
      if(frontend_request)step_inputs.push_back(*frontend_request);
      if(partition_request)step_inputs.push_back(*partition_request);
      if(phase2_policy)step_inputs.push_back(*phase2_policy);
      if(phase4_policy)step_inputs.push_back(*phase4_policy);
      if(phase6_policy)step_inputs.push_back(*phase6_policy);
      if(physical_policy)step_inputs.push_back(*physical_policy);
      if(physical_request)step_inputs.push_back(*physical_request);
      const auto step = RunExternalTool(tool,cancelled,step_inputs);
      result.steps.push_back({stage,step.operation.ok(),native_physical?"native-multi-fpga-physical-v1":native_phase6?"native-baseline-phase6-v1":native_phase4?"native-phase4-v1":native_phase5?"native-exact-phase5-v1":native_phase3?"native-phase3-v1":native_phase2?"native-phase2-v1":native_frontend?(frontend_inputs?"native-frontend-input-v1":"native-rtl-frontend-v1"):"isolated-python-adapter-v1",step.wall_seconds,step.user_cpu_seconds,
                             step.system_cpu_seconds,step.maximum_process_rss_kib});
      if (!step.operation.ok())
        throw StageError(ErrorCode::kExecutionFailed,"native compile " + std::string(stage) + ": " +
                         step.operation.message + "\n" + step.diagnostic_tail);
      struct Acceptance { bool& passed;bool accepted=false;~Acceptance(){if(!accepted)passed=false;} } acceptance{result.steps.back().passed};
      if(native_physical) {
        ControlJson producer((temporary/"native-result.json").string());auto summary=ArtifactRef::Bind(physical_report,"physical-flow-report-v1");
        Require(producer.String("$.stage")=="physical.run-flow" && producer.String("$.status")=="pass" && producer.OutputGeneration(summary),"native physical producer binding disagrees");
        ControlJson report(physical_report);Require(report.String("$.implementation_id")=="native-multi-fpga-physical-v1" && report.String("$.status")=="pass","native physical report identity disagrees");
        const auto& g=summary.generation();const auto reference=JsonRecord(std::vector<JsonRecord>{"physical-report",std::map<std::string,JsonRecord>{{"schema",summary.schema()},{"path",summary.path()},{"generation",std::vector<JsonRecord>{g.device,g.inode,g.bytes,g.modified_seconds,g.modified_nanoseconds,g.changed_seconds,g.changed_nanoseconds}}}}).Encode();
        ControlJson prior(previous.string());auto bindings=step_inputs;bindings.push_back(summary);
        auto physical_summary=ArtifactRef::Bind((output/"physical/physical-summary.json").string(),"physical-summary-v1");
        Require(producer.OutputGeneration(physical_summary,1),"native physical summary producer binding disagrees");bindings.push_back(physical_summary);
        const auto state=prior.PhysicalState(reference,output/"physical");PublishTextArtifact(next.string(),bindings,[&](std::ostream& stream){stream<<state<<'\n';});
        const auto metrics=output/"stage-metrics/phase7.json";fs::create_directories(metrics.parent_path());
        const auto text=JsonRecord(std::map<std::string,JsonRecord>{{"stage","phase7"},{"status","pass"},{"wall_seconds",step.wall_seconds},{"peak_process_tree_rss_kib",step.maximum_process_rss_kib},{"rss_measurement","wait4-native-owner-high-water"}}).Encode();
        PublishTextArtifact(metrics.string(),bindings,[&](std::ostream& stream){stream<<text<<'\n';});
      } else if(native_phase2) {
        ControlJson producer((temporary/"native-result.json").string());auto summary=ArtifactRef::Bind(phase2_report,"phase2-report-v1");
        Require(producer.String("$.schema")=="emuflow.native-stage-result/v1" && producer.String("$.stage")=="phase2.run" && producer.String("$.status")=="pass" && producer.OutputGeneration(summary),"native timing producer binding disagrees");
        ControlJson report(phase2_report,262144);Require(report.String("$.status")=="pass" && report.String("$.stage_implementation_id")=="native-phase2-v1","native timing report identity disagrees");
        const auto& g=summary.generation();std::vector<JsonRecord> generation{g.device,g.inode,g.bytes,g.modified_seconds,g.modified_nanoseconds,g.changed_seconds,g.changed_nanoseconds};
        const auto reference=JsonRecord(std::vector<JsonRecord>{JsonRecord("timing-report"),JsonRecord(std::map<std::string,JsonRecord>{{"schema",summary.schema()},{"path",summary.path()},{"generation",generation}})}).Encode();
        ControlJson prior(previous.string());auto bindings=step_inputs;bindings.push_back(summary);
        const auto state=prior.TimingState(reference);PublishTextArtifact(next.string(),bindings,[&](std::ostream& stream){stream<<state<<'\n';});
        const auto metrics=output/"stage-metrics/phase2.json";fs::create_directories(metrics.parent_path());
        const auto text=JsonRecord(std::map<std::string,JsonRecord>{{"stage","phase2"},{"status","pass"},{"wall_seconds",step.wall_seconds},{"peak_process_tree_rss_kib",step.maximum_process_rss_kib},{"rss_measurement","wait4-native-owner-high-water"}}).Encode();
        PublishTextArtifact(metrics.string(),bindings,[&](std::ostream& stream){stream<<text<<'\n';});
      } else if(native_phase3) {
        ControlJson producer((temporary/"native-result.json").string());auto summary=ArtifactRef::Bind(phase3_report,"phase3-report-v1");
        Require(producer.String("$.schema")=="emuflow.native-stage-result/v1" && producer.String("$.stage")=="phase3.run" && producer.String("$.status")=="pass" && producer.OutputGeneration(summary),"native partition producer binding disagrees");
        ControlJson report(phase3_report);Require(report.String("$.stage_implementation_id")=="native-phase3-v1" && report.String("$.status")=="pass","native partition report identity disagrees");
        const auto& g=summary.generation();std::vector<JsonRecord> generation{g.device,g.inode,g.bytes,g.modified_seconds,g.modified_nanoseconds,g.changed_seconds,g.changed_nanoseconds};
        const auto reference=JsonRecord(std::vector<JsonRecord>{JsonRecord("partition-report"),JsonRecord(std::map<std::string,JsonRecord>{{"schema",summary.schema()},{"path",summary.path()},{"generation",generation}})}).Encode();
        ControlJson prior(previous.string());auto bindings=step_inputs;bindings.push_back(summary);
        const auto state=prior.PartitionState(reference,output/"partition",report.Unsigned("$.validation.cut_nets"));
        PublishTextArtifact(next.string(),bindings,[&](std::ostream& out){out<<state<<'\n';});partition_request->RequireUnchanged();
      } else if(native_frontend) {
        ControlJson producer((temporary/"native-result.json").string());auto summary=ArtifactRef::Bind(frontend_summary,"phase1-summary-v1");
        Require(producer.String("$.schema")=="emuflow.native-stage-result/v1" && producer.String("$.stage")== (frontend_inputs?"phase1.import-inputs":"phase1.run") && producer.String("$.status")=="pass" && producer.OutputGeneration(summary),"native frontend producer binding disagrees");
        const auto& g=summary.generation();std::vector<JsonRecord> generation;
        for(const auto& item:{std::to_string(g.device),std::to_string(g.inode),std::to_string(g.bytes),std::to_string(g.modified_seconds),std::to_string(g.modified_nanoseconds),std::to_string(g.changed_seconds),std::to_string(g.changed_nanoseconds)})generation.emplace_back(item);
        const auto reference=JsonRecord(std::vector<JsonRecord>{JsonRecord("frontend-report"),JsonRecord(std::map<std::string,JsonRecord>{{"schema",summary.schema()},{"path",summary.path()},{"generation",std::move(generation)}})}).Encode();
        ControlJson prior(previous.string());auto bindings=step_inputs;bindings.push_back(summary);
        auto ir=ArtifactRef::Bind((output/"frontend/phase1/design.emuir.sqlite").string(),"emuir-compact-v2");
        Require(producer.OutputGeneration(ir,1),"native frontend IR producer binding disagrees");bindings.push_back(ir);
        std::string timing="null";
        if(frontend_inputs && prior.Has("$.state[1].prepared_timing")) {
          auto report=ArtifactRef::Bind((output/"timing/prepared-timing-summary.json").string(),"prepared-timing-summary-v1");
          Require(producer.OutputGeneration(report,2),"native prepared timing producer binding disagrees");bindings.push_back(report);
          const auto& g=report.generation();
          timing=JsonRecord(std::vector<JsonRecord>{"prepared-timing-report",std::map<std::string,JsonRecord>{{"schema",report.schema()},{"path",report.path()},{"generation",std::vector<JsonRecord>{g.device,g.inode,g.bytes,g.modified_seconds,g.modified_nanoseconds,g.changed_seconds,g.changed_nanoseconds}}}}).Encode();
        }
        const auto state=prior.FrontendState(reference,output/"frontend",timing);
        PublishTextArtifact(next.string(),bindings,[&](std::ostream& stream){stream<<state<<'\n';});
      }
      if(native_phase4) {
        ControlJson producer((temporary/"native-result.json").string());auto summary=ArtifactRef::Bind(phase4_report,"phase4-report-v1");
        Require(producer.String("$.schema")=="emuflow.native-stage-result/v1" && producer.String("$.stage")=="phase4.run" && producer.String("$.status")=="pass" && producer.OutputGeneration(summary),"native Phase 4 producer binding disagrees");
        ControlJson report(phase4_report);Require(report.String("$.stage_implementation_id")=="native-phase4-v1" && report.String("$.status")=="pass","native Phase 4 report identity disagrees");
        const auto& g=summary.generation();std::vector<JsonRecord> generation{g.device,g.inode,g.bytes,g.modified_seconds,g.modified_nanoseconds,g.changed_seconds,g.changed_nanoseconds};
        const auto reference=JsonRecord(std::vector<JsonRecord>{JsonRecord("route-report"),JsonRecord(std::map<std::string,JsonRecord>{{"schema",summary.schema()},{"path",summary.path()},{"generation",generation}})}).Encode();
        ControlJson prior(previous.string());auto bindings=step_inputs;bindings.push_back(summary);const auto state=prior.Phase4State(reference);
        PublishTextArtifact(next.string(),bindings,[&](std::ostream& out){out<<state<<'\n';});phase4_policy->RequireUnchanged();
      }
      if(native_phase5) {
        ControlJson producer((temporary/"native-result.json").string());
        auto published=ArtifactRef::Bind(phase5_report,"phase5-report-v1");
        Require(producer.String("$.schema")=="emuflow.native-stage-result/v1" && producer.String("$.stage")=="phase5.compile" && producer.String("$.status")=="pass" && producer.OutputGeneration(published),"native Phase 5 producer result binding disagrees");
        ControlJson prior(previous.string());const auto state=prior.Phase5State(Phase5ReportControlReference(published));
        auto bindings=step_inputs;bindings.push_back(published);
        PublishTextArtifact(next.string(),bindings,[&](std::ostream& stream){stream<<state<<'\n';});
        const auto metrics=(output/"stage-metrics"/"phase5.json");fs::create_directories(metrics.parent_path());
        const auto text=JsonRecord(std::map<std::string,JsonRecord>{{"stage","phase5"},{"status","pass"},{"wall_seconds",step.wall_seconds},{"peak_process_tree_rss_kib",step.maximum_process_rss_kib},{"rss_measurement","wait4-single-native-process-high-water"}}).Encode();
        PublishTextArtifact(metrics.string(),bindings,[&](std::ostream& stream){stream<<text<<'\n';});
      }
      if(native_phase6) {
        ControlJson producer((temporary/"native-result.json").string());
        auto published=ArtifactRef::Bind(phase6_report,"phase6-report-v1");
        Require(producer.String("$.schema")=="emuflow.native-stage-result/v1" && producer.String("$.stage")=="phase6.compile" && producer.String("$.status")=="pass" && producer.OutputGeneration(published),"native Phase 6 producer result binding disagrees");
        ControlJson report(published.path());Require(report.String("$.status")=="pass" && report.String("$.stage_implementation_id")=="native-baseline-phase6-v1","native Phase 6 report identity disagrees");
        const auto& g=published.generation();
        const auto reference=JsonRecord(std::vector<JsonRecord>{JsonRecord("split-report"),JsonRecord(std::map<std::string,JsonRecord>{
          {"schema",published.schema()},{"path",published.path()},{"generation",std::vector<JsonRecord>{g.device,g.inode,g.bytes,g.modified_seconds,g.modified_nanoseconds,g.changed_seconds,g.changed_nanoseconds}}})}).Encode();
        ControlJson prior(previous.string());const auto state=prior.Phase6State(reference,output/"split");
        auto bindings=step_inputs;bindings.push_back(published);
        PublishTextArtifact(next.string(),bindings,[&](std::ostream& stream){stream<<state<<'\n';});
        const auto metrics=output/"stage-metrics/phase6.json";fs::create_directories(metrics.parent_path());
        const auto text=JsonRecord(std::map<std::string,JsonRecord>{{"stage","phase6"},{"status","pass"},
          {"wall_seconds",step.wall_seconds},{"peak_process_tree_rss_kib",step.maximum_process_rss_kib},
          {"rss_measurement","wait4-native-owner-high-water"}}).Encode();
        PublishTextArtifact(metrics.string(),bindings,[&](std::ostream& stream){stream<<text<<'\n';});
      }
      try {
        auto state_ref = ArtifactRef::Bind(next.string(),"native-compile-state-v1");
        ControlJson state(state_ref.path());
        Require(state.String("$.schema") == "emuflow.native-compile-state/v1" &&
                state.String("$.stage") == stage && state.String("$.run_id") == run_id &&
                state.String("$.status") == "pass" && state.StateShape() && state.Generation(configuration.generation()),
                "mixed or invalid native compiler phase result");
        if(std::string(stage)=="physical" && state.Boolean("$.state[1].physical")) {
          const auto summary=fs::path(state.StatePath("physical_summary_path"));
          Require(fs::weakly_canonical(summary)==output/"physical/physical-summary.json",
                  "physical summary escapes compiler ownership");
        }
        state.RequirePublishedPaths(temporary);
        state_ref.RequireUnchanged();
        if (std::string(stage) == "preflight") result.effective_configuration = state.Object("$.effective_options");
      } catch (const std::exception& error) {
        result.steps.back().passed = false;
        throw StageError(ErrorCode::kInvalidArtifact,"native compile " + std::string(stage) + ": " + error.what());
      }
      if (std::string(stage) == "preflight") ClaimOutput(output,result);
      try {
        // Adapter execution and native group cleanup have completed. No phase
        // may carry a temporary graph/cache into the next process through state.
        fs::remove_all(temporary);
      } catch (const std::exception& error) {
        result.steps.back().passed = false;
        throw StageError(ErrorCode::kPublicationFailed,
                         "cannot retire native phase temporary files: " + std::string(error.what()));
      }
      if(frontend_request)fs::remove(frontend_request->path());
      if (!previous.empty()) fs::remove(previous);
      previous = next;
      acceptance.accepted=true;
      std::cout << "COMPILE_STAGE_COMPLETE " << stage << " " << step.wall_seconds << "s\n" << std::flush;
    }
    ControlJson report((output / "multi-fpga-flow-report.json").string());
    Require(report.String("$.schema") == "emuflow.multi-fpga-flow/v2" && report.String("$.status") == "pass",
            "native compiler final flow report is invalid");
  }, [&] {
    return std::vector<ArtifactRef>{ArtifactRef::Bind(
        (fs::path(result.output_directory) / "multi-fpga-flow-report.json").string(),"multi-fpga-flow-report-v2")};
  });
  return result;
}
std::string CompileResultJson(const CompileResult& result) {
  std::ostringstream out; out << std::setprecision(17);
  out << "{\"schema\":\"emuflow.native-compile-result/v2\",\"implementation_id\":\"native-orchestration-v1\","
      << "\"phase_implementation\":\"native-frontend-timing-partition-routing-preparation-phase2-phase3-phase4-phase5-baseline-phase6-physical-signoff-and-isolated-python-adapters-v11\",\"native_data_plane_complete\":false,"
      << "\"operation\":" << StageResultJson(result.operation) << ",\"output_directory\":" << JsonQuote(result.output_directory)
      << ",\"request_configuration\":" << (result.request_configuration.empty() ? "null" : result.request_configuration)
      << ",\"effective_configuration\":" << (result.effective_configuration.empty() ? "null" : result.effective_configuration)
      << ",\"stages\":[";
  bool first = true;
  for (const auto& stage : result.steps) {
    if (!first) out << ',';
    first = false;
    out << "{\"stage\":" << JsonQuote(stage.stage) << ",\"implementation_id\":" << JsonQuote(stage.implementation_id) << ",\"status\":\"" << (stage.passed ? "pass" : "fail")
        << "\",\"wall_seconds\":" << stage.wall_seconds << ",\"user_cpu_seconds\":" << stage.user_cpu_seconds
        << ",\"system_cpu_seconds\":" << stage.system_cpu_seconds << ",\"maximum_process_rss_kib\":" << stage.maximum_process_rss_kib
        << ",\"cpu_scope\":\"wait4-child-and-reaped-descendants\",\"rss_scope\":\"wait4-maximum-process-high-water\"}";
  }
  out << "]}\n"; return out.str();
}
void PublishCompileResult(const CompileResult& result) {
  if (!result.owns_output) return;
  RequireOwner(result);
  const auto text = CompileResultJson(result);
  AtomicBinaryWriter writer((fs::path(result.output_directory) / "native-compile-result.json").string());
  writer.WriteBytes(text.data(),text.size()); writer.Commit();
  fs::remove(fs::path(result.output_directory) / ".native-compile-owner");
}
}  // namespace emuflow::core
