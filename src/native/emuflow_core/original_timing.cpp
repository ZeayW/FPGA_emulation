#include "emuflow_core/original_timing.hpp"
#include "emuflow_core/hash.hpp"
#include <algorithm>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <set>

namespace emuflow::core {
namespace {
void Check(bool valid,const char* message) {
  if(!valid)throw StageError(ErrorCode::kInvalidArtifact,message);
}
void Digest(const std::string& value) {
  Check(value.size()==64 && value.find_first_not_of("0123456789abcdef")==std::string::npos,"original timing seal is invalid");
}
std::string Hash(const std::string& value) {
  Sha256 hash;hash.update(reinterpret_cast<const unsigned char*>(value.data()),value.size());
  std::string result;for(auto byte:hash.finish()){result+="0123456789abcdef"[byte>>4];result+="0123456789abcdef"[byte&15];}return result;
}
std::string Text(SqliteStatement& row,int column) {
  auto value=row.TypedText(column);
  Check(!value.empty() && value!="NULL" && value.size()<=1024*1024 && value.find('\0')==std::string::npos,
        "original timing identity is missing or invalid");return value;
}
double Number(SqliteStatement& row,int column) {
  const auto value=row.TypedReal(column);Check(std::isfinite(value),"original timing number is nonfinite");return value;
}
OriginalEndpoint Endpoint(SqliteStatement& row,int first) {
  const auto bit=row.TypedInteger(first+3);Check(bit>=0,"original timing endpoint bit is invalid");
  OriginalEndpoint result{Text(row,first),Text(row,first+2),{},static_cast<std::uint64_t>(bit)};
  if(!row.IsNull(first+1))result.instance=Text(row,first+1);return result;
}
bool Flag(SqliteStatement& row,int column) {
  const auto flag=row.TypedInteger(column);Check(flag==0 || flag==1,"original timing check flag is invalid");return flag;
}
}
double OriginalClockBudget::ReferenceWindow() const {
  const auto result=target_cycle_time_ns+multicycle_adjustment_ns-launch_edge_ns;
  Check(std::isfinite(result) && result>0,"original timing clock edge window is nonpositive");return result;
}
ScaledOriginalWindow ScaleOriginalWindow(const OriginalClockBudget& budget,const SourceClockReferences& clocks,
    const std::map<std::string,double>& targets) {
  const auto source=clocks.find(budget.source_clock),target=clocks.find(budget.target_clock);
  const auto source_target=targets.find(budget.source_clock),capture_target=targets.find(budget.target_clock);
  Check(source!=clocks.end() && target!=clocks.end() && source_target!=targets.end() && capture_target!=targets.end(),
        "original timing requires explicit source and capture clock references");
  const auto validate=[](const SourceClockReference& clock,double target_period) {
    Check(std::isfinite(clock.declared_period_ns) && clock.declared_period_ns>0 && std::isfinite(clock.engine_period_ns) &&
          clock.engine_period_ns>0 && std::abs(clock.engine_period_ns-clock.declared_period_ns)<=1e-6*clock.declared_period_ns &&
          std::isfinite(target_period) && target_period>0,"original timing clock reference is invalid");
  };
  validate(source->second,source_target->second);validate(target->second,capture_target->second);
  Check(std::isfinite(budget.source_period_ns) && budget.source_period_ns>0 && std::isfinite(budget.target_period_ns) &&
        budget.target_period_ns>0 && std::abs(budget.source_period_ns-source->second.engine_period_ns)<=1e-12*budget.source_period_ns &&
        std::abs(budget.target_period_ns-target->second.declared_period_ns)<=1e-6*budget.target_period_ns,
        "original timing clock catalog belongs to another source context");
  const auto source_scale=source_target->second/source->second.engine_period_ns;
  const auto target_scale=capture_target->second/target->second.engine_period_ns;
  Check(std::isfinite(source_scale) && source_scale>0 && std::isfinite(target_scale) && target_scale>0 &&
        std::abs(source_scale-target_scale)<=std::max(1e-12,1e-7*std::max(std::abs(source_scale),std::abs(target_scale))),
        "original timing requires uniform source/capture clock scaling");
  const auto coefficient=budget.ReferenceWindow()/target->second.engine_period_ns;
  const auto window=coefficient*capture_target->second;
  const auto launch=(budget.launch_edge_ns/source->second.engine_period_ns)*source_target->second;
  Check(std::isfinite(coefficient) && coefficient>0 && std::isfinite(window) && window>0 && std::isfinite(launch),
        "original timing scaled edge window is unrepresentable");return {launch,window,coefficient};
}
OriginalTimingView::OriginalTimingView(const ArtifactRef& selector,const ArtifactRef& payload,const std::string& seal)
    :selector_(selector),paths_(payload) {
  Check(selector.schema()=="sta-path-selector-v1" && payload.schema()=="timing-path-store-v2",
        "original timing requires current clock-budget path owners");Digest(seal);
  Check(selector.generation().bytes>0 && selector.generation().bytes<=1024*1024,"original timing selector exceeds its bound");
  std::ifstream input(selector.path(),std::ios::binary);
  std::string json(selector.generation().bytes,'\0');input.read(json.data(),json.size());
  Check(input.gcount()==static_cast<std::streamsize>(json.size()) && input.peek()==std::char_traits<char>::eof() &&
        !input.bad() && Hash(json)==seal,"original timing selector seal differs");
  auto& db=paths_.database();
  auto descriptor=db.Prepare("SELECT json_extract(?1,'$.schema'),json_extract(?1,'$.payloads.paths.format'),"
      "json_extract(?1,'$.payloads.paths.path'),json_extract(?1,'$.payloads.paths.records'),json_extract(?1,'$.payloads.paths.sha256'),"
      "json_extract(?1,'$.summary.paths'),json_extract(?1,'$.summary.clock_budget_paths'),"
      "json_extract(?1,'$.source.clock_budget_policy'),json_extract(?1,'$.source.clock_logic_policy')");descriptor.BindText(1,json);
  Check(descriptor.Step() && descriptor.TypedText(0)=="emuflow.sta-path-database/v4" && descriptor.TypedText(1)=="sqlite-sta-path/v2" &&
        descriptor.TypedText(7)=="engine-edge-v1" && descriptor.TypedText(8)=="indexed-init-clock-sense-v1",
        "original timing selector schema or policy differs");
  const auto name=Text(descriptor,2);const auto count=descriptor.TypedInteger(3);Digest(Text(descriptor,4));
  Check(name!="." && name!=".." && std::filesystem::path(name).filename()==name && name.find('\\')==std::string::npos &&
        std::filesystem::equivalent(std::filesystem::path(selector.path()).parent_path()/name,payload.path()),
        "original timing payload belongs to another selector location");
  Check(count>0 && static_cast<std::uint64_t>(count)<=payload.generation().bytes && descriptor.TypedInteger(5)==count &&
        descriptor.TypedInteger(6)==count,"original timing population count differs");count_=count;
  bool unique=false;auto indexes=db.Prepare("PRAGMA index_list(paths)");
  while(indexes.Step())if(indexes.TypedInteger(2)==1 && indexes.TypedInteger(4)==0) {
    auto columns=db.Prepare("SELECT name FROM pragma_index_info(?) ORDER BY seqno");columns.BindText(1,indexes.TypedText(1));
    if(columns.Step() && columns.TypedText(0)=="path_id" && !columns.Step())unique=true;
  }
  Check(unique,"original timing path identities lack their unique owner index");RequireUnchanged();
}
void OriginalTimingView::RequireUnchanged() const {selector_.RequireUnchanged();paths_.RequireUnchanged();}
void OriginalTimingView::Visit(const std::function<void(const OriginalTimingPath&)>& visitor) {
  RequireUnchanged();
  auto& db=paths_.database();
  auto rows=db.Prepare("SELECT p.ordinal,p.path_id,p.clock_domain,p.clock_period_ns,p.start_object,p.start_instance,p.start_port,p.start_bit,"
      "p.end_object,p.end_instance,p.end_port,p.end_bit,b.path_ordinal,b.source_clock,b.source_period_ns,b.launch_edge_ns,b.capture_edge_ns,"
      "b.source_offset_ns,b.target_cycle_time_ns,b.multicycle_adjustment_ns,b.check_margin_ns,b.source_latency_ns,b.target_clock_delay_ns,"
      "b.clock_skew_ns,b.output_delay,b.path_delay,b.latch_check FROM paths p LEFT JOIN clock_budgets b ON b.path_ordinal=p.ordinal ORDER BY p.ordinal");
  std::uint64_t observed=0;
  while(rows.Step()) {
    Check(rows.TypedInteger(0)>=0 && static_cast<std::uint64_t>(rows.Integer(0))==observed && observed<count_ &&
          rows.TypedInteger(12)==rows.Integer(0),"original timing path/budget coverage or order differs");
    OriginalClockBudget budget{Text(rows,13),Text(rows,2),Number(rows,14),Number(rows,3),Number(rows,15),Number(rows,16),
        Number(rows,17),Number(rows,18),Number(rows,19),Number(rows,20),Number(rows,21),Number(rows,22),Number(rows,23),Flag(rows,24)};
    Check(budget.source_period_ns>0 && budget.target_period_ns>0 && !Flag(rows,25) && !Flag(rows,26),
          "original timing requires supported clocked setup/output checks");budget.ReferenceWindow();
    OriginalTimingPath path{paths_.Id<PathTag>(observed++),Text(rows,1),Endpoint(rows,4),Endpoint(rows,8),std::move(budget)};
    visitor(path);
  }
  auto budgets=db.Prepare("SELECT count(*) FROM clock_budgets");
  Check(observed==count_ && budgets.Step() && budgets.TypedInteger(0)==static_cast<std::int64_t>(count_),
        "original timing has missing or extra path/budget records");RequireUnchanged();
}
OriginalCaptureCheck BindOriginalCaptureCheck(const OriginalTimingPath& path,const SourceClockReferences& clocks,
    const std::map<std::string,double>& targets,double event,double data,double sampling) {
  const auto window=ScaleOriginalWindow(path.budget,clocks,targets);
  Check(std::isfinite(event) && std::isfinite(data) && data>=0 && std::isfinite(sampling) && std::isfinite(sampling-data),
        "original capture requires finite routed data and sampling costs");
  const auto required=window.launch_ns+window.window_ns-(sampling-data);
  Check(std::isfinite(required),"original capture deadline is unrepresentable");
  return {event,data,required,window.launch_ns,window.window_ns,window.coefficient};
}
}  // namespace emuflow::core
