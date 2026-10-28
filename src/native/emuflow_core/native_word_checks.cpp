#include "emuflow_core/native_word_checks.hpp"
#include "emuflow_core/stage_runtime.hpp"
#include <limits>
#include <string>
namespace emuflow::core {
namespace {
using Wide=__int128;
void Check(bool ok,const char* message){if(!ok)throw StageError(ErrorCode::kInvalidArtifact,message);}
std::int64_t Integer(Wide value){
  Check(value>=std::numeric_limits<std::int64_t>::min() && value<=std::numeric_limits<std::int64_t>::max(),
      "native word verification time exceeds its domain");return static_cast<std::int64_t>(value);
}
std::string Hex(const std::string& text){
  static const char digits[]="0123456789abcdef";std::string result;result.reserve(text.size()*2);
  for(unsigned char c:text){result+=digits[c>>4];result+=digits[c&15];}return result;
}
struct Writer {
  std::ostream& stream;std::uint64_t window,count=0;
  void Emit(const std::string& path,const char* role,const std::string& event,
      Wide launch,Wide arc,Wide required,bool accepted){
    Check(arc>=0,"native verification arc is negative");
    const auto l=Integer(launch),a=Integer(arc),r=Integer(required);
    Check(accepted ? launch+4*arc<=required : launch+4*arc>required,
        "native word phase witness contradicts an exported timing check");
    Check(count<UINT64_MAX,"native word verification event count exceeds its domain");++count;
    stream<<window<<'\t'<<Hex(path)<<'\t'<<role<<'\t'<<Hex(event)<<'\t'<<l<<'\t'<<a<<'\t'<<r<<'\t'
        <<(accepted?"accept":"reject")<<'\n';
    Check(bool(stream),"native word verification stream failed");
  }
};
}
void NativeWordCheckHeader(std::ostream& stream){
  stream<<"window\tpath_hex\trole\tevent_hex\tlaunch_quarter_ps\tverification_arc_ps\trequired_quarter_ps\texpectation\n";
}
std::uint64_t WriteNativeWordWindowChecks(std::ostream& stream,std::uint64_t window,
    const NativeWordWindowProjection& projection,const std::vector<NativeWordTransferOwner>& owners,
    const std::vector<WordPhaseTransfer>& graph,std::int64_t period){
  Check(period>0 && graph.size()==owners.size() && bool(projection.maximum_sampling_cost_ps),
      "native word verification window is incomplete");
  Writer writer{stream,window};const Wide bound=*projection.maximum_sampling_cost_ps;
  const std::string prefix="window"+std::to_string(window)+":";
  // A 1 ns verification arc is a nonnegative numerical probe, not a routed
  // data measurement. The signed E contribution is encoded in the deadline.
  const Wide probe=1000;
  if(projection.local_sampling_cost_ps){
    const Wide cost=*projection.local_sampling_cost_ps;
    writer.Emit(prefix+"local","target","local-bound",0,probe,4*(bound-cost+probe),true);
    if(cost==bound)writer.Emit(prefix+"local","runtime","reduced-cost-bound",0,probe,4*(bound-1-cost+probe),false);
  }
  if(!projection.transport.witness)return writer.count;
  const auto& witness=*projection.transport.witness;
  Check(witness.load_words.size()==graph.size() && witness.upper_offset_ps>witness.lower_offset_ps &&
      witness.upper_offset_ps<=period && witness.lower_offset_ps>=0,
      "native word verification witness has invalid coordinates");
  // All provider costs and phase boundaries have a 1 ps quantum. A point
  // 0.25 ps below the upper boundary stays strictly inside this phase cell.
  const Wide offset=4*Wide(witness.upper_offset_ps)-1;
  const auto load=[&](std::size_t i){
    Check(bool(witness.load_words[i]),"native word verification causal predecessor is absent");
    return offset+4*Wide(*witness.load_words[i])*period;
  };
  const auto arrival=[&](std::size_t i){return load(i)+4*Wide(graph[i].fixed_latency_words)*period;};
  for(std::size_t i=0;i<graph.size();++i){
    if(!witness.load_words[i])continue;
    const auto& node=graph[i];const Wide current=load(i);
    const auto words=node.ratio_serial_uis/8;
    Check(words>0 && node.ratio_serial_uis%8==0 && node.phase_word<words,
        "native word verification serializer units differ");
    Wide phase=(Wide(*witness.load_words[i])+witness.phase_word)%words;if(phase<0)phase+=words;
    Check(phase==node.phase_word,"native word verification load is outside its canonical phase");
    const auto path=prefix+"transfer:"+owners[i].id;
    struct Contribution{Wide launch,cost;std::string name;};
    std::optional<Contribution> blocker;
    const auto add=[&](Wide launch,Wide cost,const std::string& event){
      writer.Emit(path,"tx",event,launch,probe,current-4*(cost-probe),true);
      if(!blocker || launch+4*cost>blocker->launch+4*blocker->cost)
        blocker=Contribution{launch,cost,event};
    };
    if(node.root_sampling_cost_ps)add(0,*node.root_sampling_cost_ps,"dut-root");
    for(const auto& edge:node.fanins){
      Check(edge.source<graph.size(),"native word verification fanin index differs");
      if(!witness.load_words[edge.source])continue;
      const auto from=arrival(edge.source);
      add(from,edge.sampling_cost_ps,"fanin-data:"+owners[edge.source].id);
      add(from,period,"fanin-next-word:"+owners[edge.source].id);
    }
    Check(bool(blocker),"native word verification active transfer has no cause");
    const Wide previous=current-4*Wide(words)*period;
    writer.Emit(path,"commit","previous-occurrence:"+blocker->name,blocker->launch,probe,
        previous-4*(blocker->cost-probe),false);
    writer.Emit(path,"target","declared-link",current,Wide(node.fixed_latency_words)*period,arrival(i),true);
    if(node.capture_sampling_cost_ps){
      const Wide cost=*node.capture_sampling_cost_ps;
      writer.Emit(path,"target","capture-bound",arrival(i),probe,4*(bound-cost+probe),true);
      if(i==witness.capture_transfer && projection.transport.maximum_capture_cost_ps==projection.maximum_sampling_cost_ps)
        writer.Emit(path,"runtime","reduced-cost-bound",arrival(i),probe,4*(bound-1-cost+probe),false);
    }
  }
  return writer.count;
}
}
