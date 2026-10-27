#include "transport/runtime_calendar.hpp"
#include "common/artifact_io.hpp"
#include "common/json_record.hpp"
#include "storage/store.hpp"
#include <algorithm>
#include <charconv>
#include <cmath>
#include <fstream>
#include <numeric>

namespace emuflow::core {
namespace {
using Object=std::map<std::string,JsonRecord>;
using Array=std::vector<JsonRecord>;
using U=std::uint64_t;
void Check(bool value,const std::string& message){
  if(!value)throw StageError(ErrorCode::kInvalidArtifact,"runtime: "+message);
}
std::string Read(const ArtifactRef& ref){
  ref.RequireUnchanged();Check(ref.generation().bytes<=8*1024*1024,"board exceeds bound");
  std::ifstream in(ref.path(),std::ios::binary);std::string text(ref.generation().bytes,'\0');
  in.read(text.data(),text.size());Check(bool(in),"cannot read board");return text;
}
std::map<std::string,std::string> Fields(const std::string& text){
  std::map<std::string,std::string> result;
  VisitJsonObjectTokens(text,[&](const auto& name,const auto& token){
    Check(result.size()<16384 && result.emplace(name,token).second,"duplicate/excessive clock fields");
  });return result;
}
std::string Meta(SqliteDatabase& db,const char* name){
  auto q=db.Prepare("SELECT value FROM metadata WHERE key=?");q.BindText(1,name);
  Check(q.Step(),"missing schedule metadata");return q.TypedText(0);
}
std::string Get(const std::map<std::string,std::string>& fields,const std::string& name){
  auto found=fields.find(name);Check(found!=fields.end(),"missing field "+name);return found->second;
}
std::int64_t Integer(const std::string& text){
  std::int64_t value=0;auto parsed=std::from_chars(text.data(),text.data()+text.size(),value);
  Check(parsed.ec==std::errc() && parsed.ptr==text.data()+text.size(),"invalid frame count");return value;
}
struct Decimal { U mantissa=0;int exponent=0;double value=0; };
Decimal Period(const std::string& text){
  Decimal value;value.value=ParseFiniteDecimal(text,"root clock period");
  Check(value.value>0,"DUT clock periods must be positive");
  // Common shortest float spelling matches Fraction(str(float(period))).
  auto digits=JsonRecord(value.value).Encode();const auto at=digits.find_first_of("eE");
  if(at!=std::string::npos){value.exponent=std::stoi(digits.substr(at+1));digits.resize(at);}
  const auto dot=digits.find('.');if(dot!=std::string::npos){value.exponent-=int(digits.size()-dot-1);digits.erase(dot,1);}
  for(const auto c:digits){Check(c>='0' && c<='9',"invalid positive decimal period");value.mantissa=value.mantissa*10+U(c-'0');}
  Check(value.mantissa>0,"zero root period");
  while(value.mantissa%10==0){value.mantissa/=10;++value.exponent;}return value;
}
std::pair<U,U> Ratio(const Decimal& value,const Decimal& reference){
  const auto common=std::gcd(value.mantissa,reference.mantissa);
  U numerator=value.mantissa/common,denominator=reference.mantissa/common;
  const auto exponent=value.exponent-reference.exponent;
  for(int i=0;i<std::abs(exponent);++i){
    auto& growing=exponent>0?numerator:denominator;
    auto& shrinking=exponent>0?denominator:numerator;
    const auto factor=std::gcd<U>(shrinking,10);shrinking/=factor;
    Check(growing<=4096/(10/factor),"multi-clock event calendar exceeds 4096 ticks");growing*=10/factor;
  }
  Check(numerator<=4096 && denominator<=4096,"multi-clock event calendar exceeds 4096 ticks");
  return {numerator,denominator};
}
U Lcm(U left,U right){
  const auto factor=left/std::gcd(left,right);
  Check(right && factor<=4096/right,"multi-clock event calendar exceeds 4096 ticks");return factor*right;
}
double Quantum(const Decimal& reference,U ticks){
  const auto common=std::gcd(reference.mantissa,ticks);
  auto denominator=ticks/common;std::string numerator=std::to_string(reference.mantissa/common);
  unsigned twos=0,fives=0;while(denominator%2==0){denominator/=2;++twos;}
  while(denominator%5==0){denominator/=5;++fives;}Check(denominator==1,"non-decimal timebase");
  const auto places=std::max(twos,fives);
  const auto multiply=[&](unsigned factor){unsigned carry=0;for(auto p=numerator.rbegin();p!=numerator.rend();++p){
      const auto v=unsigned(*p-'0')*factor+carry;*p=char('0'+v%10);carry=v/10;
    }if(carry)numerator.insert(numerator.begin(),char('0'+carry));};
  for(unsigned i=twos;i<places;++i)multiply(2);
  for(unsigned i=fives;i<places;++i)multiply(5);
  return ParseFiniteDecimal(numerator+'e'+std::to_string(reference.exponent-int(places)),"clock timebase");
}
}
std::string BuildVirtualRuntime(const ArtifactRef& schedule_ref,const ArtifactRef& board_ref,
                               const std::string& clock_periods){
  Check(schedule_ref.schema()=="tdm-schedule-store-v4" && board_ref.schema()=="boarddb-v1","source roles disagree");
  SqliteStore schedule(schedule_ref);const auto header=Fields(Meta(schedule.database(),"header"));
  const auto board=Fields(Read(board_ref));
  Check(DecodeJsonString(Get(board,"schema"))=="emuflow.boarddb/v1" &&
        DecodeJsonString(Get(Fields(Get(board,"platform")),"name"))==DecodeJsonString(Get(header,"platform")),"BoardDB namespace disagrees");
  std::vector<double> frequencies;
  VisitJsonArrayTokens(Get(board,"links"),[&](const auto& token){
    const auto value=ParseFiniteDecimal(Get(Fields(token),"fabric_clock_mhz"),"fabric frequency");
    Check(value>0,"fabric frequency must be positive");frequencies.push_back(value);
  });
  Check(!frequencies.empty(),"virtual runtime requires a BoardDB link");
  for(const auto value:frequencies)Check(std::abs(value-frequencies.front())<=1e-9,"requires one common fabric clock frequency");
  const auto metrics=Fields(Get(header,"metrics"));
  const auto frame=Integer(Get(metrics,"frame_slots")),completion=Integer(Get(metrics,"completion_slot"));
  Check(frame>=2 && completion>=0 && completion<frame-1,"schedule must complete before barrier release");
  auto maximum=schedule.database().Prepare("SELECT coalesce(max(arrival_slot),0) FROM schedule_entries");
  Check(maximum.Step() && maximum.TypedInteger(0)==completion,"completion_slot differs from scheduled arrivals");
  const auto frequency=frequencies.front(),fabric_period=1000.0/frequency,frame_period=frame*fabric_period;
  const auto release=frame-1,settle=release-completion;
  const auto periods=Fields(clock_periods);Check(!periods.empty(),"requires original DUT clocks");
  std::vector<std::pair<std::string,Decimal>> clocks;
  for(const auto& item:periods){Check(!item.first.empty(),"empty root clock identity");clocks.emplace_back(item.first,Period(item.second));}
  std::vector<std::pair<U,U>> ratios;U reference_ticks=1;
  for(const auto& clock:clocks){const auto ratio=Ratio(clock.second,clocks.front().second);
    ratios.push_back(ratio);reference_ticks=Lcm(reference_ticks,ratio.second);}
  U calendar=1;std::vector<U> ticks;
  for(const auto& ratio:ratios){const auto count=ratio.first*(reference_ticks/ratio.second);
    Check(count<=4096,"multi-clock event calendar exceeds 4096 ticks");ticks.push_back(count);calendar=Lcm(calendar,count);}
  Object domains;Array names,events;double fastest=frame_period*ticks.front();
  for(std::size_t i=0;i<clocks.size();++i){const auto period=ticks[i]*frame_period;fastest=std::min(fastest,period);
    names.emplace_back(clocks[i].first);domains.emplace(clocks[i].first,Object{
      {"target_period_ns",clocks[i].second.value},{"period_ticks",ticks[i]},
      {"nominal_period_ns",period},{"nominal_frequency_mhz",1000.0/period},
      {"phase_ticks",0},{"edge","complete-pulse-per-calendar-event"}});}
  for(U tick=1;tick<=calendar;++tick){Array active;
    for(std::size_t i=0;i<clocks.size();++i)if(tick%ticks[i]==0)active.emplace_back(clocks[i].first);
    events.emplace_back(Object{{"offset_ticks",tick},{"clocks",active}});}
  const Object runtime{{"schema","emuflow.virtual-runtime/v2"},
    {"design",DecodeJsonString(Get(header,"design"))},{"platform",DecodeJsonString(Get(header,"platform"))},
    {"provider","multi-clock-pausible-calendar-barrier-v2"},
    {"semantic_envelope",Object{{"virtual_dut_clocks",clocks.size()},{"reset_model","synchronous-fabric-controller"},
      {"clock_progression","per-domain-edge-calendar-after-complete-frame"}}},
    {"fabric_clock",Object{{"frequency_mhz",frequency},{"period_ns",fabric_period},{"distribution","common-frequency-lockstep"}}},
    {"frame",Object{{"slots",frame},{"completion_slot",completion},{"barrier_release_slot",release},
      {"shadow_settle_slots",settle},{"shadow_settle_ns",settle*fabric_period}}},
    {"virtual_timebase",Object{{"target_period_ns",Quantum(clocks.front().second,reference_ticks)},
      {"nominal_frequency_mhz",1000.0/frame_period},{"nominal_period_ns",frame_period},
      {"calendar_ticks",calendar},{"events",events}}},
    {"virtual_dut_clocks",domains},
    {"virtual_dut_clock",Object{{"scope","fastest-domain-derived-summary"},{"nominal_frequency_mhz",1000.0/fastest},
      {"nominal_period_ns",fastest},{"enable_pulse_fabric_cycles",1},
      {"enable_assertion_phase","during-release-slot-before-dut-edge"},{"may_stall",true},
      {"binding","dedicated-clock-buffer-enable"},{"unsafe_binding","fabric-routed-combinational-clock-gate"}}},
    {"barrier",Object{{"ready_signal","links_ready"},{"ready_scope","global-consensus-across-all-fpgas"},
      {"stall_slot",release},{"stall_behavior","hold-slot-and-suppress-dut-clock-enable"},
      {"startup_behavior","hold-slot-zero-until-global-ready"},
      {"lockstep_requirements",Array{"common fabric-clock frequency","phase-aligned fabric clocks or trained slot alignment",
        "synchronous controller reset release","same synchronized global links_ready value"}}}},
    {"timing_model",Object{{"dut_clock_ports",names},{"fabric_clock_port","fabric_clk"},
      {"fabric_to_dut_max_delay_ns",settle*fabric_period},{"physical_link_io_delay_status","requires-hardware-bsp"},
      {"cdc_protocol","pausible-clock-stable-data-window"}}},
    {"board_binding",Object{{"status","virtual"},{"requires_package_pins",true},
      {"requires_clock_buffer_binding",true},{"requires_link_training",true}}}};
  schedule.RequireUnchanged();board_ref.RequireUnchanged();return JsonRecord(runtime).Encode();
}
}
