#include "emuflow_core/native_word_network.hpp"
#include "emuflow_core/json_record.hpp"
#include <iostream>
#include <numeric>
#include <sstream>

using namespace emuflow::core;
namespace {
const std::string header="phase_word\tquery_hex\tsource_kind\tsource_hex\tlaunch_edge\tmeasurement\trequirement_ns\tdata_ns\tsampling_cost_ns\tclock_adjustment_ns\tstart_hex\tend_hex\tstart_clock_hex\tend_clock_hex\tlaunch_ns\tcapture_ns\n";
std::string Hex(const std::string& text) {
  std::string result;for(unsigned char c:text){result+="0123456789abcdef"[c>>4];result+="0123456789abcdef"[c&15];}return result;
}
std::string Clock(unsigned part,unsigned family) {return "native"+std::to_string(family)+"_p"+std::to_string(part);}
std::string Family(unsigned family) {return family ? "csb":"core";}
NativeWordLaunchEdge Edge(unsigned edge) {
  if(edge!=1 && edge!=2)throw std::runtime_error("invalid fixture edge");return static_cast<NativeWordLaunchEdge>(edge);
}
void Row(std::ostream& stream,std::int64_t phase,const std::string& query,const std::string& kind,
    const std::string& source,unsigned edge,const std::optional<std::int64_t>& cost,const std::string& end_clock="physical") {
  stream<<phase<<'\t'<<Hex(query)<<'\t'<<kind<<'\t'<<Hex(source)<<'\t'<<(edge==1?"rise":edge==2?"fall":"")<<'\t';
  if(!cost){stream<<"native-no-timed-path\t\t\t\t\t\t\t\t\t\t\n";return;}
  const double ns=static_cast<double>(*cost)/1000;
  stream<<"native-clock-aware-reference\t1.000\t1.000\t"<<JsonRecord(ns).Encode()<<'\t'<<JsonRecord(ns-1).Encode()<<'\t'
      <<Hex("optimized/C")<<'\t'<<Hex("actual/D")<<'\t'<<Hex(kind=="rx"?"emuflow_tdm_clk":source)<<'\t'
      <<Hex(end_clock)<<"\t0.000\t0.000\n";
}
struct Node {
  unsigned source,sink,phase,ratio,latency,has_root,root_family,root_edge,has_capture,capture_family,capture_edge;
  std::int64_t root,capture;
  std::vector<std::pair<unsigned,std::int64_t>> fanins;
};
struct Direct {unsigned part,source_family,source_edge,capture_family,capture_edge;std::int64_t cost;};
}
int main() {
  try {
    std::int64_t period,reference;std::uint64_t bound;unsigned part_count,node_count,window_count,direct_count,flags;
    if(!(std::cin>>period>>reference>>bound>>part_count>>node_count>>window_count>>direct_count>>flags) ||
        part_count>10 || node_count>10000 || window_count>16 || direct_count>1000)throw std::runtime_error("invalid network fixture");
    std::vector<Node> nodes;
    std::vector<NativeWordTransferOwner> owners;
    for(unsigned i=0;i<node_count;++i){
      Node n;unsigned fanins;
      if(!(std::cin>>n.source>>n.sink>>n.phase>>n.ratio>>n.latency>>n.has_root>>n.root>>n.root_family>>n.root_edge
            >>n.has_capture>>n.capture>>n.capture_family>>n.capture_edge>>fanins) || fanins>node_count)
        throw std::runtime_error("invalid fixture transfer");
      for(unsigned j=0;j<fanins;++j){unsigned source;std::int64_t cost;if(!(std::cin>>source>>cost))throw std::runtime_error("invalid fixture relay");n.fanins.emplace_back(source,cost);}
      owners.push_back({"e"+std::to_string(i),"p"+std::to_string(n.source),"p"+std::to_string(n.sink),n.phase,n.ratio,n.latency});
      nodes.push_back(n);
    }
    std::vector<NativeWordClockWindow> windows;
    for(unsigned i=0;i<window_count;++i){
      unsigned sf,se,cf,ce;std::int64_t w;if(!(std::cin>>sf>>se>>cf>>ce>>w))throw std::runtime_error("invalid fixture window");
      windows.push_back({{Family(sf),Edge(se)},{Family(cf),Edge(ce)},w});
    }
    std::vector<Direct> direct;
    for(unsigned i=0;i<direct_count;++i){Direct d;if(!(std::cin>>d.part>>d.source_family>>d.source_edge>>d.capture_family>>d.capture_edge>>d.cost))throw std::runtime_error("invalid direct fixture");direct.push_back(d);}
    std::vector<NativeWordTxBinding> bindings(part_count);
    std::vector<std::istringstream> captures(part_count);
    std::vector<NativeWordPartitionCosts> partitions;
    for(unsigned p=0;p<part_count;++p){
      NativeWordTxCatalog catalog;catalog.maximum_contexts=1000000;catalog.maximum_repeat_words=bound;
      const bool transit=(flags&32) && p==1;
      if(!transit)catalog.dut_clocks={Clock(p,0),Clock(p,1)};
      std::set<std::uint32_t> periods;
      for(unsigned i=0;i<nodes.size();++i){
        const auto& n=nodes[i];
        if(n.source==p)catalog.entries.push_back({owners[i].id,"word"+std::to_string(i),n.phase,n.ratio});
        if(n.sink==p)catalog.rx_entries.emplace("s"+std::to_string(i),owners[i].id);
        if(n.source==p || n.sink==p)periods.insert(n.ratio/8);
      }
      if(periods.empty())periods.insert(1);
      catalog.calendar_period_words.assign(periods.begin(),periods.end());
      std::uint64_t repeat=1;for(auto q:periods)repeat=std::lcm(repeat,static_cast<std::uint64_t>(q));
      std::ostringstream tx;tx<<header;
      std::vector<NativeWordTxFaninCounts> fanins;
      for(unsigned i=0;i<nodes.size();++i){
        const auto& n=nodes[i];if(n.source!=p)continue;
        for(std::uint64_t phase=n.phase;phase<repeat;phase+=n.ratio/8){
          if((flags&1) && i==0 && phase==n.phase)continue;
          const auto query="word"+std::to_string(i);
          if(!transit)for(unsigned family:{0,1})for(unsigned edge:{1,2}){
            std::optional<std::int64_t> cost;
            if(n.has_root && n.root_family==family && n.root_edge==edge)cost=n.root;
            Row(tx,phase,query,"dut-clock",Clock(p,family),edge,cost);
          }
          for(const auto& f:n.fanins)Row(tx,phase,query,"rx","s"+std::to_string(f.first),0,f.second);
          fanins.push_back({phase,query,n.fanins.size(),0,0,0});
        }
      }
      std::istringstream tx_stream(tx.str());bindings[p]=BindNativeWordTxCosts(catalog,fanins,tx_stream);
      std::ostringstream cap;cap<<header;
      if(!transit)for(unsigned cf:{0,1})for(unsigned ce:{1,2}){
        const auto query="dut-capture:"+Clock(p,cf)+(ce==1?":rise":":fall");
        for(unsigned sf:{0,1})for(unsigned se:{1,2}){
          std::optional<std::int64_t> cost;
          for(const auto& d:direct)if(d.part==p && d.source_family==sf && d.source_edge==se && d.capture_family==cf && d.capture_edge==ce)
            if(!cost || d.cost>*cost)cost=d.cost;
          Row(cap,-1,query,"dut-clock",Clock(p,sf),se,cost,Clock(p,cf));
        }
        for(unsigned i=0;i<nodes.size();++i){
          const auto& n=nodes[i];if(n.sink!=p)continue;
          if((flags&2) && i==0 && cf==0 && ce==1)continue;
          std::optional<std::int64_t> cost;if(n.has_capture && n.capture_family==cf && n.capture_edge==ce)cost=n.capture;
          Row(cap,-1,query,"rx","s"+std::to_string(i),0,cost,Clock(p,(flags&16) && i==0 && cf==0 && ce==1 ? 1:cf));
          if((flags&4) && i==0 && cf==0 && ce==1)Row(cap,-1,query,"rx","s"+std::to_string(i),0,cost,Clock(p,cf));
        }
      }
      captures[p].str(cap.str());
      std::vector<NativeWordClockAlias> aliases;
      if(!transit)aliases={{Clock(p,0),"core"},{Clock(p,1),"csb"}};
      partitions.push_back({"p"+std::to_string(p),&bindings[p],aliases,catalog.rx_entries,{}, {},&captures[p]});
    }
    if(flags&8)owners[0].sink_partition="p"+std::to_string(nodes[0].source);
    const auto result=ProjectNativeWordNetwork(owners,partitions,windows,period,reference,bound);
    std::vector<JsonRecord> projected;
    for(const auto& w:result.windows){
      std::vector<JsonRecord> words;
      if(w.transport.witness)for(const auto& word:w.transport.witness->load_words)words.push_back(word ? JsonRecord(*word):JsonRecord());
      projected.emplace_back(std::map<std::string,JsonRecord>{{"source",w.window.launch.clock},{"source_edge",static_cast<int>(w.window.launch.edge)},
          {"capture",w.window.capture.clock},{"capture_edge",static_cast<int>(w.window.capture.edge)},
          {"cost_ps",*w.maximum_sampling_cost_ps},{"required_period_ps",*w.required_reference_period_ps},
          {"cross_ps",w.transport.maximum_capture_cost_ps ? JsonRecord(*w.transport.maximum_capture_cost_ps):JsonRecord()},
          {"phase",w.transport.witness ? JsonRecord(w.transport.witness->phase_word):JsonRecord()},
          {"lower",w.transport.witness ? JsonRecord(w.transport.witness->lower_offset_ps):JsonRecord()},
          {"upper",w.transport.witness ? JsonRecord(w.transport.witness->upper_offset_ps):JsonRecord()},
          {"words",JsonRecord(std::move(words))}});
    }
    std::cout<<JsonRecord(std::map<std::string,JsonRecord>{{"maximum_cost_ps",result.maximum_sampling_cost_ps ? JsonRecord(*result.maximum_sampling_cost_ps):JsonRecord()},
        {"required_period_ps",result.required_reference_period_ps ? JsonRecord(*result.required_reference_period_ps):JsonRecord()},
        {"transfers",result.transfers},{"capture_rows",result.capture_rows},{"windows",JsonRecord(std::move(projected))}}).Encode()<<"\n";
    return 0;
  }catch(const std::exception& e){std::cerr<<e.what()<<"\n";return 1;}
}
