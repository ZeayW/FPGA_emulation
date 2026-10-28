#include "flow/signoff.hpp"

#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/json_record.hpp"
#include "transport/runtime_calendar.hpp"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <map>
#include <set>
#include <sstream>
#include <string>
#include <vector>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using Raw = std::map<std::string,std::string>;

void Check(bool value,const std::string& why) {
  if(!value)throw StageError(ErrorCode::kInvalidArtifact,"Phase 7C signoff: "+why);
}
struct Workspace {
  fs::path root;
  explicit Workspace(const fs::path& parent) {
    auto pattern=(parent/".phase7c-stage-XXXXXX").string();
    Check(mkdtemp(pattern.data())!=nullptr,"cannot create private workspace");root=pattern;
  }
  ~Workspace(){if(!root.empty()){std::error_code error;fs::remove_all(root,error);}}
};
ArtifactRef Bind(const fs::path& path,const std::string& schema="exact-sha256-text-v1") {
  Check(path.is_absolute() && fs::is_regular_file(path),"source is unavailable: "+path.string());
  return ArtifactRef::Bind(path.string(),schema);
}
BoundedJson Read(const ArtifactRef& source,std::size_t maximum=8*1024*1024) {
  return BoundedJson(BoundedJson::Read(source,maximum),maximum);
}
Raw Fields(const std::string& token){return BoundedJson(token).Fields();}
std::string Get(const Raw& fields,const std::string& name) {
  const auto found=fields.find(name);Check(found!=fields.end(),"missing field "+name);return found->second;
}
std::string Text(const Raw& fields,const std::string& name){return DecodeJsonString(Get(fields,name));}
std::int64_t Integer(const Raw& fields,const std::string& name){return BoundedJson::IntegerToken(Get(fields,name));}
double Number(const Raw& fields,const std::string& name){return ParseFiniteDecimal(Get(fields,name),"Phase 7C "+name);}
std::string Object(const Raw& fields){return BoundedJson::Encode(fields);}
std::string Number(double value){return JsonRecord(value).Encode();}
std::string Boolean(bool value){return value?"true":"false";}
void Write(const fs::path& path,const std::string& text) {
  std::ofstream stream(path,std::ios::binary|std::ios::trunc);stream<<text;
  Check(bool(stream),"cannot write "+path.string());
}
std::string ResolveSchedule(const ArtifactRef& selector,const fs::path& root) {
  auto document=Read(selector);const fs::path artifact(document.Text("artifact"));
  Check(!artifact.empty() && artifact==artifact.filename() && artifact!="." && artifact!="..",
        "schedule selector escapes its owner");return (root/artifact).string();
}
void Pass(BoundedJson& report,const std::string& owner,const std::string& design,const std::string& platform) {
  Check(report.Text("status")=="pass",owner+" did not pass");
  Check(report.Text("design")==design,owner+" design identity disagrees");
  Check(report.Text("platform")==platform,owner+" platform identity disagrees");
  const auto validation=Fields(report.Token("validation"));
  Check(Text(validation,"status")=="pass",owner+" validation did not pass");
}
std::string Fixed(double value) {
  std::ostringstream out;out.imbue(std::locale::classic());out<<std::fixed<<std::setprecision(9)<<value;return out.str();
}
std::vector<std::string> Array(const std::string& token){return BoundedJson::Array(token);}

std::string RuntimeXdc(const Raw& runtime) {
  const auto fabric=Fields(Get(runtime,"fabric_clock")),timing=Fields(Get(runtime,"timing_model"));
  const auto clocks=Fields(Get(runtime,"virtual_dut_clocks"));
  std::ostringstream out;
  out<<"# EmuFlow board-independent pausible multi-clock timing contract.\n"
     <<"# Original root-clock identities and relative rates are preserved.\n"
     <<"create_clock -name emuflow_fabric_clk -period "<<Fixed(Number(fabric,"period_ns"))
     <<" [get_ports {"<<Text(timing,"fabric_clock_port")<<"}]\n";
  std::size_t index=0;
  for(const auto& item:clocks){const auto clock=Fields(item.second);const auto name="emuflow_dut_clk_"+std::to_string(index++);
    out<<"create_clock -name "<<name<<" -period "<<Fixed(Number(clock,"nominal_period_ns"))
       <<" [get_ports {"<<item.first<<"}]\n"
       <<"set_max_delay -datapath_only "<<Fixed(Number(timing,"fabric_to_dut_max_delay_ns"))
       <<" -from [get_clocks {emuflow_fabric_clk}] -to [get_clocks {"<<name<<"}]\n"
       <<"# "<<item.first<<": target_period_ns="<<Fixed(Number(clock,"target_period_ns"))
       <<" period_ticks="<<Integer(clock,"period_ticks")<<"\n";
  }
  out<<"\n# Board-specific input/output delays, package pins, IOSTANDARD,\n"
     <<"# source-synchronous forwarding, and link training are intentionally\n"
     <<"# absent until a hardware BSP is selected.\n"
     <<"# physical_link_io_delay_status=requires-hardware-bsp\n\n";
  return out.str();
}

std::string RuntimeTestbench(const Raw& runtime,const std::vector<std::string>& fpgas) {
  const auto frame=Fields(Get(runtime,"frame")),clocks=Fields(Get(runtime,"virtual_dut_clocks"));
  const auto timebase=Fields(Get(runtime,"virtual_timebase"));const auto events=Array(Get(timebase,"events"));
  const auto slots=std::uint64_t(Integer(frame,"slots"));
  const auto clock_count=clocks.size(),event_count=events.size();
  Check(!fpgas.empty() && clock_count && event_count,"runtime testbench dimensions are empty");
  const auto bits=[](std::size_t count){std::size_t value=1;while((std::size_t(1)<<value)<count)++value;return value;};
  const auto slot_bits=bits(slots),event_bits=bits(event_count);std::map<std::string,std::size_t> clock_index;std::size_t ci=0;
  for(const auto& clock:clocks)clock_index.emplace(clock.first,ci++);
  std::vector<std::uint64_t> masks;
  for(const auto& token:events){std::uint64_t mask=0;for(const auto& raw:Array(Get(Fields(token),"clocks"))){
      const auto name=DecodeJsonString(raw);Check(clock_index.count(name),"runtime event references unknown clock");
      Check(clock_index.at(name)<64,"runtime testbench clock mask exceeds 64 bits");mask|=std::uint64_t(1)<<clock_index.at(name);}masks.push_back(mask);}
  std::ostringstream out;out<<"`timescale 1ns/1ps\nmodule virtual_runtime_controller_tb;\n"
    <<"  localparam integer FRAME_SLOTS = "<<slots<<";\n  localparam integer SLOT_BITS = "<<slot_bits
    <<";\n  localparam integer TARGET_FRAMES = 12;\n  localparam integer CLOCK_COUNT = "<<clock_count
    <<";\n  localparam integer EVENT_COUNT = "<<event_count<<";\n  localparam integer EVENT_BITS = "<<event_bits
    <<";\n  logic fabric_clk = 1'b0;\n  logic reset = 1'b1;\n  logic links_ready = 1'b0;\n";
  for(std::size_t i=0;i<fpgas.size();++i)out<<"  logic [CLOCK_COUNT-1:0] virtual_clock_enable_"<<i<<";\n"
    <<"  logic [EVENT_BITS-1:0] event_index_"<<i<<";\n  logic [SLOT_BITS-1:0] slot_"<<i<<";\n"
    <<"  emuflow_virtual_runtime_controller #(.FRAME_SLOTS(FRAME_SLOTS), .SLOT_BITS(SLOT_BITS)) dut_"<<i<<" (\n"
    <<"    .fabric_clk(fabric_clk), .reset(reset), .links_ready(links_ready),\n"
    <<"    .virtual_clock_enable(virtual_clock_enable_"<<i<<"), .event_index_debug(event_index_"<<i<<"), .slot(slot_"<<i<<")\n"
    <<"  );\n  // FPGA instance "<<fpgas[i]<<"\n";
  out<<"  always #2 fabric_clk = ~fabric_clk;\n  integer pulses = 0;\n  integer stalled_cycles = 0;\n"
    <<"  integer previous_slot;\n  integer previous_event;\n  function automatic [CLOCK_COUNT-1:0] expected_mask;\n"
    <<"    input integer event_index;\n    begin\n      case (event_index)\n";
  for(std::size_t i=0;i<masks.size();++i)out<<"        "<<i<<": expected_mask = "<<clock_count<<"'h"<<std::hex<<masks[i]<<std::dec<<";\n";
  out<<"        default: expected_mask = '0;\n      endcase\n    end\n  endfunction\n"
    <<"  initial begin\n    repeat (3) @(posedge fabric_clk);\n    #1 reset = 1'b0;\n"
    <<"    repeat (3) begin @(posedge fabric_clk); #1; if (slot_0 !== 0 || virtual_clock_enable_0 !== '0) $fatal(1, \"controller advanced before global ready\"); end\n"
    <<"    @(negedge fabric_clk); links_ready = 1'b1;\n    @(posedge fabric_clk); #1;\n"
    <<"    if (slot_0 !== 0 || virtual_clock_enable_0 !== '0) $fatal(1, \"startup barrier did not begin at slot zero\");\n"
    <<"    while (pulses < TARGET_FRAMES) begin\n      @(negedge fabric_clk); previous_slot = slot_0; previous_event = event_index_0;\n"
    <<"      if (pulses == 2 && previous_slot == FRAME_SLOTS - 1 && stalled_cycles < 3) links_ready = 1'b0; else links_ready = 1'b1;\n      #1;\n"
    <<"      if (previous_slot == FRAME_SLOTS - 1 && links_ready) begin if (virtual_clock_enable_0 !== expected_mask(previous_event)) $fatal(1, \"clock-event mask mismatch before release\"); end\n"
    <<"      else if (virtual_clock_enable_0 !== '0) $fatal(1, \"clock enable asserted outside release window\");\n      @(posedge fabric_clk); #1;\n";
  for(std::size_t i=1;i<fpgas.size();++i)out<<"      if (slot_"<<i<<" !== slot_0 || virtual_clock_enable_"<<i
    <<" !== virtual_clock_enable_0 || event_index_"<<i<<" !== event_index_0) $fatal(1, \"controller "<<i<<" lost lockstep\");\n";
  out<<"      if (!links_ready && previous_slot == FRAME_SLOTS - 1) begin stalled_cycles = stalled_cycles + 1; if (slot_0 !== FRAME_SLOTS - 1 || virtual_clock_enable_0 !== '0 || event_index_0 !== previous_event) $fatal(1, \"barrier did not hold at release slot\"); end\n"
    <<"      else if (previous_slot == FRAME_SLOTS - 1) begin if (slot_0 !== 0 || virtual_clock_enable_0 !== '0) $fatal(1, \"illegal state after frame release\"); if (event_index_0 !== ((previous_event == EVENT_COUNT - 1) ? 0 : previous_event + 1)) $fatal(1, \"illegal multi-clock event progression\"); pulses = pulses + 1; end\n"
    <<"      else begin if (slot_0 !== previous_slot + 1) $fatal(1, \"illegal slot progression\"); end\n"
    <<"      if (virtual_clock_enable_0 !== '0) $fatal(1, \"clock enable remained asserted after edge\");\n    end\n"
    <<"    if (stalled_cycles != 3) $fatal(1, \"stall coverage mismatch\");\n"
    <<"    $display(\"EMUFLOW_RUNTIME_TB status=pass frames=%0d stalled_cycles=%0d controllers=%0d\", pulses, stalled_cycles, "<<fpgas.size()<<");\n"
    <<"    $finish;\n  end\nendmodule\n";return out.str();
}

std::string TimingEstimate(const Raw& runtime,BoundedJson& phase5) {
  if(!phase5.Has("timing_validation"))return Object({{"status",JsonQuote("unavailable")},{"qualification",JsonQuote("no-scheduled-path-timing")}});
  const auto timing=Fields(phase5.Token("timing_validation"));Check(Text(timing,"status")=="pass","scheduled timing did not pass");
  const auto worst=Number(timing,"worst_delay_ns"),slack=Number(timing,"worst_slack_ns");
  const auto virtual_clock=Fields(Get(runtime,"virtual_dut_clock"));const auto period=Number(virtual_clock,"nominal_period_ns");
  const auto runtime_slack=period-worst;Check(std::isfinite(runtime_slack),"runtime timing is not finite");
  Raw original{{"worst_path",timing.count("worst_path")?timing.at("worst_path"):"null"},{"worst_delay_ns",Number(worst)},
    {"worst_slack_ns",Number(slack)},{"negative_slack_paths",timing.count("negative_slack_paths")?timing.at("negative_slack_paths"):"null"},{"closure_gate","false"}};
  Raw clock{{"period_ns",Number(period)},{"frequency_mhz",Get(virtual_clock,"nominal_frequency_mhz")},
    {"estimated_worst_slack_ns",Number(runtime_slack)},{"estimated_closed",Boolean(runtime_slack>=0)}};
  return Object({{"status",JsonQuote(runtime_slack>=0?"pass":"fail")},
    {"qualification",JsonQuote("academic-preplacement-fixed-delay-plus-concrete-tdm-schedule")},
    {"original_clock_reference",Object(original)},{"virtual_clock",Object(clock)}});
}
}  // namespace

std::string RuntimeSignoffXdc(const Raw& runtime){return RuntimeXdc(runtime);}
std::string RuntimeSignoffTestbench(const Raw& runtime,const std::vector<std::string>& fpgas){return RuntimeTestbench(runtime,fpgas);}

void SignoffNonphysical(const fs::path& output) {
  Check(output.is_absolute() && fs::is_directory(output),"flow root is unavailable");
  const auto target=output/"runtime";Check(!fs::exists(target) && !fs::is_symlink(target),"runtime output already exists");
  std::vector<ArtifactRef> sources;
  const auto source=[&](const fs::path& relative,const std::string& schema="exact-sha256-text-v1") -> ArtifactRef {
    sources.push_back(Bind(output/relative,schema));return sources.back();};
  auto phase3_ref=source("partition/phase3_report.json","phase3-report-v1");auto phase3=Read(phase3_ref);
  const auto design=phase3.Text("design"),platform=phase3.Text("platform");
  auto phase4_ref=source("system-route/phase4_report.json","phase4-report-v1");auto phase4=Read(phase4_ref);
  auto phase5_ref=source("tdm/phase5_report.json","phase5-report-v1");auto phase5=Read(phase5_ref);
  auto phase6_ref=source("split/phase6_report.json","phase6-report-v1");auto phase6=Read(phase6_ref);
  Pass(phase3,"Phase 3",design,platform);Pass(phase4,"Phase 4",design,platform);
  Pass(phase5,"Phase 5",design,platform);Pass(phase6,"Phase 6",design,platform);
  const auto equivalence=Fields(phase6.Token("equivalence"));
  Check(Text(equivalence,"status")=="pass" && Integer(equivalence,"mismatches")==0,"functional equivalence did not pass");
  const auto schedule_selector=source("tdm/schedule.json");
  sources.push_back(ArtifactRef::Bind(ResolveSchedule(schedule_selector,output/"tdm"),"tdm-schedule-store-v4"));
  const auto schedule=sources.back();const auto board=source("frontend/phase1/platform.normalized.json","boarddb-v1");
  auto board_document=Read(board);Check(board_document.Text("schema")=="emuflow.boarddb/v1","BoardDB schema disagrees");
  const auto board_platform=Fields(board_document.Token("platform"));Check(Text(board_platform,"name")==platform,"BoardDB platform disagrees");
  std::vector<std::string> fpgas;for(const auto& token:Array(board_document.Token("fpgas")))fpgas.push_back(Text(Fields(token),"id"));
  const auto manifest_ref=source("split/manifest.json");auto manifest=Read(manifest_ref);
  Check(manifest.Has("clock_periods"),"split manifest lacks clock periods");
  const auto runtime_text=BuildVirtualRuntime(schedule,board,manifest.Token("clock_periods"));const auto runtime=Fields(runtime_text);
  Check(Text(runtime,"design")==design && Text(runtime,"platform")==platform,"runtime identity disagrees");
  const auto frame=Fields(Get(runtime,"frame")),clocks=Fields(Get(runtime,"virtual_dut_clocks")),timebase=Fields(Get(runtime,"virtual_timebase"));
  Raw validation{{"status",JsonQuote("pass")},{"fpgas",std::to_string(fpgas.size())},
    {"frame_slots",Get(frame,"slots")},{"completion_slot",Get(frame,"completion_slot")},
    {"barrier_release_slot",Get(frame,"barrier_release_slot")},{"shadow_settle_slots",Get(frame,"shadow_settle_slots")},
    {"shadow_settle_ns",Get(frame,"shadow_settle_ns")},{"virtual_dut_clocks",std::to_string(clocks.size())},
    {"clock_event_count",std::to_string(Array(Get(timebase,"events")).size())},
    {"nominal_virtual_frequency_mhz",Get(Fields(Get(runtime,"virtual_dut_clock")),"nominal_frequency_mhz")}};
  const auto p3=Fields(phase3.Token("validation")),p4=Fields(phase4.Token("validation")),p5=Fields(phase5.Token("validation"));
  const auto timing=TimingEstimate(runtime,phase5);
  Raw qor{{"schema",JsonQuote("emuflow.qor-report/v4")},{"status",JsonQuote("pending")},
    {"design",JsonQuote(design)},{"platform",JsonQuote(platform)},{"whole_design_timing_complete","true"},
    {"partition",Object({{"instances",Get(p3,"instances")},{"used_fpgas",Get(p3,"used_fpgas")},
      {"cut_nets",Get(p3,"cut_nets")},{"cut_sink_endpoints",Get(p3,"cut_sink_endpoints")}})},
    {"system_routing",Object({{"demands",Get(p4,"demands")},{"routed_sinks",Get(p4,"routed_sinks")},
      {"bit_hops",Get(p4,"total_link_bit_hops")},{"max_link_utilization",Get(p4,"max_link_utilization")}})},
    {"tdm",Object({{"scheduled_bit_hops",Get(p5,"scheduled_bit_hops")},{"frame_slots",Get(p5,"frame_slots")},
      {"completion_slot",Get(p5,"completion_slot")},{"max_domain_utilization",Get(p5,"max_domain_utilization")},
      {"collisions",Get(p5,"collisions")}})},
    {"runtime",Object({{"fabric_frequency_mhz",Get(Fields(Get(runtime,"fabric_clock")),"frequency_mhz")},
      {"nominal_virtual_frequency_mhz",Get(Fields(Get(runtime,"virtual_dut_clock")),"nominal_frequency_mhz")},
      {"nominal_virtual_period_ns",Get(Fields(Get(runtime,"virtual_dut_clock")),"nominal_period_ns")},
      {"shadow_settle_ns",Get(frame,"shadow_settle_ns")},{"barrier_release_slot",Get(frame,"barrier_release_slot")}})},
    {"timing",timing},{"equivalence",Object({{"cycles",Get(equivalence,"cycles")},
      {"compared_state_bits",Get(equivalence,"compared_state_bits")},{"compared_output_bits",Get(equivalence,"compared_output_bits")},
      {"mismatches",Get(equivalence,"mismatches")},{"trace_sha256",Get(equivalence,"trace_sha256")}})},
    {"physical",Object({{"status",JsonQuote("pending")}})},{"board_binding",Get(runtime,"board_binding")}};
  const auto timing_fields=Fields(timing);
  Raw timing_summary{{"status",Get(timing_fields,"status")},{"qualification",Get(timing_fields,"qualification")}};
  const auto schedule_legal=phase5.Text("status")=="pass" && Integer(p5,"collisions")==0;
  Raw report{{"schema",JsonQuote("emuflow.phase7c-report/v3")},{"phase",JsonQuote("7C")},
    {"increment",JsonQuote("unified-physical-link-tdm-system-timing")},{"status",JsonQuote("generated")},
    {"design",JsonQuote(design)},{"platform",JsonQuote(platform)},{"provider",Get(runtime,"provider")},
    {"validation",Object(validation)},
    {"functional_equivalence",Object({{"status",JsonQuote("pass")},{"cycles",Get(equivalence,"cycles")},{"trace_sha256",Get(equivalence,"trace_sha256")}})},
    {"schedule_legality",Object({{"status",JsonQuote(schedule_legal?"pass":"fail")},{"collisions",Get(p5,"collisions")},
      {"cross_layer_timing",phase5.Has("cross_layer_timing_validation")?phase5.Token("cross_layer_timing_validation"):"null"}})},
    {"physical",Object({{"status",JsonQuote("pending")}})},
    {"artifacts",Object({{"runtime_contract",JsonQuote("runtime_contract.json")},{"runtime_controller_rtl",JsonQuote("virtual_runtime_controller.sv")},
      {"runtime_controller_testbench",JsonQuote("virtual_runtime_controller_tb.sv")},{"runtime_timing_xdc",JsonQuote("runtime_timing.xdc")},
      {"qor_report",JsonQuote("qor_report.json")},{"report",JsonQuote("phase7c_report.json")}})},
    {"runtime_timing_summary",Object(timing_summary)},
    {"system_timing_ref",Object({{"artifact",JsonQuote("qor_report")},{"json_pointer",JsonQuote("/timing")}})}};
  const auto controller=source("split/virtual_runtime_controller.sv");
  Workspace work(output);const auto stage=work.root;
  Write(stage/"runtime_contract.json",runtime_text+'\n');Write(stage/"qor_report.json",Object(qor)+'\n');
  fs::copy_file(controller.path(),stage/"virtual_runtime_controller.sv");
  Write(stage/"virtual_runtime_controller_tb.sv",RuntimeTestbench(runtime,fpgas));
  Write(stage/"runtime_timing.xdc",RuntimeXdc(runtime));Write(stage/"phase7c_report.json",Object(report)+'\n');
  for(const auto& item:sources)item.RequireUnchanged();PublishNewDirectory(stage.string(),target.string(),sources);work.root.clear();
}

}  // namespace emuflow::core
