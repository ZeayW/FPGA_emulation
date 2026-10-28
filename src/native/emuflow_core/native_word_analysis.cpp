#include "emuflow_core/native_word_analysis.hpp"
#include "emuflow_core/native_word_network.hpp"
#include "emuflow_core/native_word_checks.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/store.hpp"
#include <array>
#include <charconv>
#include <fstream>
#include <memory>
#include <sstream>

namespace emuflow::core {
namespace {
constexpr std::uint64_t kMagic=0x315150574e4d5545ULL;
constexpr const char* kCostHeader="phase_word\tquery_hex\tsource_kind\tsource_hex\tlaunch_edge\tmeasurement\trequirement_ns\tdata_ns\tsampling_cost_ns\tclock_adjustment_ns\tstart_hex\tend_hex\tstart_clock_hex\tend_clock_hex\tlaunch_ns\tcapture_ns\n";
void Check(bool ok,const char* message) {
  if(!ok)throw StageError(ErrorCode::kInvalidArtifact,message);
}
std::uint64_t Number(const std::string& s) {
  std::uint64_t value=0;const auto parsed=std::from_chars(s.data(),s.data()+s.size(),value);
  Check(!s.empty() && parsed.ec==std::errc{} && parsed.ptr==s.data()+s.size() && std::to_string(value)==s,
      "native word observation integer is invalid");return value;
}
std::uint32_t Small(std::int64_t value) {
  Check(value>=0 && value<=UINT32_MAX,"native word index coordinate exceeds its domain");return value;
}
std::string Identity(const std::string& s) {
  Check(!s.empty() && s.size()%2==0,"native word query encoding is invalid");std::string result;
  const auto digit=[](char c){return c>='0'&&c<='9'?c-'0':c>='a'&&c<='f'?c-'a'+10:-1;};
  for(std::size_t i=0;i<s.size();i+=2){auto a=digit(s[i]),b=digit(s[i+1]);Check(a>=0&&b>=0,"native word query encoding is invalid");result+=char(16*a+b);}
  Check(result.find('\0')==std::string::npos,"native word query contains NUL");(void)JsonQuote(result);return result;
}
void Lines(const ArtifactRef& input,const std::string& header,
    const std::function<void(const std::vector<std::string>&)>& consume) {
  std::ifstream stream(input.path(),std::ios::binary);std::array<char,1048578> buffer;
  bool first=true;
  while(true){
    stream.getline(buffer.data(),buffer.size());
    Check(!stream.bad() && !(stream.fail()&&!stream.eof()),"native word observation line is unreadable or too large");
    if(!stream.gcount())break;
    const auto size=static_cast<std::size_t>(stream.gcount())-(stream.eof()?0:1);
    Check(size<=1048576,"native word observation line is too large");
    std::string line(buffer.data(),size);Check(line.find('\0')==std::string::npos && line.find('\r')==std::string::npos,
        "native word observation delimiter is invalid");
    if(first){Check(line==header,"native word observation header differs");first=false;continue;}
    std::vector<std::string> fields;std::size_t start=0;
    while(true){const auto end=line.find('\t',start);fields.push_back(line.substr(start,end==std::string::npos?end:end-start));
      Check(fields.size()<=8,"native word fanin row has too many columns");if(end==std::string::npos)break;start=end+1;}
    consume(fields);
  }
  Check(!first,"native word observation header is absent");input.RequireUnchanged();
}
std::int64_t Meta(SqliteDatabase& db,const std::string& key) {
  auto q=db.Prepare("SELECT json_extract(value,'$') FROM metadata WHERE key=?");q.BindText(1,key);
  Check(q.Step(),"native word index parameter is absent");const auto value=q.TypedInteger(0);
  Check(value>0 && !q.Step(),"native word index parameter is invalid");return value;
}
NativeWordLaunchEdge Edge(std::int64_t edge) {
  Check(edge==1||edge==2,"native word clock window edge is invalid");return static_cast<NativeWordLaunchEdge>(edge);
}
struct Part {
  std::string name;
  NativeWordTxCatalog catalog;
  std::vector<NativeWordClockAlias> clocks;
  std::set<std::string> missing,inputs;
  NativeWordTxBinding binding;
  std::unique_ptr<std::ifstream> captures;
  std::optional<std::size_t> first;
  std::uint64_t capture_rows=0;
};
JsonRecord Optional(const std::optional<std::int64_t>& value){return value?JsonRecord(*value):JsonRecord();}
}
void AnalyzeNativeWordNetwork(const std::vector<ArtifactRef>& inputs,const std::string& output,bool verification_events) {
  Check(inputs.size()>=2 && (inputs.size()-2)%4==0 && inputs.size()<=258,
      "native word analysis stream coverage is invalid");
  for(const auto& input:inputs)input.RequireUnchanged();
  CheckedRequestReader request(inputs[1].path(),1024*1024);
  Check(request.Read<std::uint64_t>()==kMagic && request.Read<std::uint32_t>()==1,
      "native word analysis request version differs");
  const auto observed=request.Read<std::uint32_t>(),project=request.Read<std::uint32_t>();
  Check(observed==(inputs.size()-2)/4 && project<=1,"native word request observation count or mode differs");
  for(std::size_t i=0;i<inputs.size();++i){
    if(i==1)continue;
    const FileGeneration expected{request.Read<std::uint64_t>(),request.Read<std::uint64_t>(),request.Read<std::uint64_t>(),
        request.Read<std::int64_t>(),request.Read<std::int64_t>(),request.Read<std::int64_t>(),request.Read<std::int64_t>()};
    Check(expected==inputs[i].generation(),"native word observation generation differs from its request");
  }
  SqliteStore index(inputs[0]);auto& db=index.database();
  const auto word=Meta(db,"word_period_ps"),reference=Meta(db,"reference_period_ps");
  const auto maximum_repeat=Meta(db,"maximum_repeat_words"),maximum_contexts=Meta(db,"maximum_contexts");
  Check(Meta(db,"word_width_bits")==8,"native word index has another serializer width");
  std::vector<Part> parts;
  auto part_rows=db.Prepare("SELECT seq,fpga FROM partitions ORDER BY seq");
  while(part_rows.Step()){
    Check(part_rows.TypedInteger(0)==static_cast<std::int64_t>(parts.size()) && parts.size()<64,
        "native word index partition order is not dense");
    Part p;p.name=part_rows.TypedText(1);Check(!p.name.empty(),"native word index partition identity is empty");
    p.catalog.maximum_repeat_words=maximum_repeat;p.catalog.maximum_contexts=maximum_contexts;
    parts.push_back(std::move(p));
  }
  Check(!parts.empty(),"native word index has no partitions");
  for(const auto& sql:{
      "SELECT 1 FROM transfers t LEFT JOIN partitions a ON a.fpga=t.source_partition LEFT JOIN partitions b ON b.fpga=t.sink_partition WHERE a.seq IS NULL OR b.seq IS NULL OR a.seq=b.seq LIMIT 1",
      "SELECT 1 FROM transfers t LEFT JOIN tx_entries e ON e.entry_id=t.id WHERE e.fpga IS NULL OR e.fpga!=t.source_partition LIMIT 1",
      "SELECT 1 FROM transfers t LEFT JOIN rx_entries e ON e.entry_id=t.id WHERE e.fpga IS NULL OR e.fpga!=t.sink_partition LIMIT 1",
      "SELECT 1 FROM tx_entries e LEFT JOIN transfers t ON t.id=e.entry_id WHERE t.id IS NULL LIMIT 1",
      "SELECT 1 FROM rx_entries e LEFT JOIN transfers t ON t.id=e.entry_id WHERE t.id IS NULL LIMIT 1"}) {
    auto bad=db.Prepare(sql);Check(!bad.Step(),"native word index canonical TX/RX ownership is incomplete");
  }
  for(std::uint32_t i=0;i<observed;++i){
    const auto seq=request.Read<std::uint32_t>();Check(seq<parts.size() && !parts[seq].first,"native word observed partition is absent or repeated");
    auto& part=parts[seq];part.first=2+4*i;
    const auto count=request.Read<std::uint32_t>();Check(count<=100000,"native word declared input population exceeds its bound");
    for(std::uint32_t j=0;j<count;++j)Check(part.inputs.insert(request.ReadString32()).second,"native word declared input repeats");
  }
  request.RequireEnd();
  std::vector<NativeWordTransferOwner> transfers;
  auto owner_rows=db.Prepare("SELECT id,source_partition,sink_partition,phase_word,ratio_serial_uis,fixed_latency_words FROM transfers ORDER BY id");
  while(owner_rows.Step())transfers.push_back({owner_rows.TypedText(0),owner_rows.TypedText(1),owner_rows.TypedText(2),
      Small(owner_rows.TypedInteger(3)),Small(owner_rows.TypedInteger(4)),Small(owner_rows.TypedInteger(5))});
  std::vector<NativeWordClockWindow> windows;
  auto window_rows=db.Prepare("SELECT launch_clock,launch_edge,capture_clock,capture_edge,reference_window_ps FROM windows ORDER BY launch_clock,launch_edge,capture_clock,capture_edge");
  while(window_rows.Step())windows.push_back({{window_rows.TypedText(0),Edge(window_rows.TypedInteger(1))},
      {window_rows.TypedText(2),Edge(window_rows.TypedInteger(3))},window_rows.TypedInteger(4)});
  std::vector<JsonRecord> coverage;
  std::uint64_t expected_contexts=0,observed_contexts=0,unknown_entries=0,dependent_entries=0,untimed_rx=0;
  bool ready=observed==parts.size();
  for(auto& part:parts){
    auto q=db.Prepare("SELECT native_clock,logical_clock FROM clocks WHERE fpga=? ORDER BY native_clock");q.BindText(1,part.name);
    while(q.Step()){part.catalog.dut_clocks.push_back(q.TypedText(0));part.clocks.push_back({q.TypedText(0),q.TypedText(1)});}
    auto periods=db.Prepare("SELECT period_words FROM calendar_periods WHERE fpga=? ORDER BY period_words");periods.BindText(1,part.name);
    while(periods.Step())part.catalog.calendar_period_words.push_back(Small(periods.TypedInteger(0)));
    auto rx=db.Prepare("SELECT origin,entry_id FROM rx_entries WHERE fpga=? ORDER BY origin");rx.BindText(1,part.name);
    while(rx.Step())Check(part.catalog.rx_entries.emplace(rx.TypedText(0),rx.TypedText(1)).second,"native word index RX role repeats");
    auto tx=db.Prepare("SELECT e.entry_id,e.query,t.phase_word,t.ratio_serial_uis FROM tx_entries e JOIN transfers t ON t.id=e.entry_id WHERE e.fpga=? ORDER BY e.entry_id");tx.BindText(1,part.name);
    while(tx.Step())part.catalog.entries.push_back({tx.TypedText(0),tx.TypedText(1),Small(tx.TypedInteger(2)),Small(tx.TypedInteger(3))});
    std::vector<NativeWordTxFaninCounts> fanins;
    if(part.first){
      const auto first=*part.first;
      Lines(inputs[first+1],"phase_word\tquery_hex\tnative_startpoints\trx_sources\tdut_cells\tinfrastructure_cells\tports\tunclassified",[&](const auto& fields){
        Check(fields.size()==8,"native word fanin ledger has another column count");
        const auto phase=Number(fields[0]);for(std::size_t i=2;i<8;++i)(void)Number(fields[i]);
        fanins.push_back({phase,Identity(fields[1]),Number(fields[3]),Number(fields[5]),Number(fields[6]),Number(fields[7])});
      });
      Lines(inputs[first+3],"source_origin",[&](const auto& fields){
        Check(fields.size()==1 && part.catalog.rx_entries.count(fields[0]) && part.missing.insert(fields[0]).second,
            "native word missing RX role is unknown or repeated");
      });
      std::ifstream cost(inputs[first].path(),std::ios::binary);
      part.binding=BindNativeWordTxCosts(part.catalog,fanins,cost);
      part.captures=std::make_unique<std::ifstream>(inputs[first+2].path(),std::ios::binary);
      const auto counts=VisitNativeWordCosts(*part.captures,[&](const auto& row){Check(row.phase_word==-1,"native capture row has a serializer phase");});
      part.capture_rows=counts.rows;part.captures->clear();part.captures->seekg(0);
    }else{
      std::istringstream empty(kCostHeader);part.binding=BindNativeWordTxCosts(part.catalog,{},empty);
    }
    std::uint64_t incomplete=0,dependent=0;
    for(const auto& entry:part.binding.entries){if(!entry.data_phase_invariant)++incomplete;else if(!*entry.data_phase_invariant)++dependent;}
    expected_contexts+=part.binding.expected_contexts;observed_contexts+=part.binding.observed_contexts;
    unknown_entries+=incomplete;dependent_entries+=dependent;untimed_rx+=part.binding.untimed_rx_rows;
    ready=ready && part.binding.expected_contexts==part.binding.observed_contexts && !incomplete && !dependent && !part.binding.untimed_rx_rows;
    coverage.emplace_back(std::map<std::string,JsonRecord>{{"fpga",part.name},{"observed",bool(part.first)},
        {"expected_tx_contexts",part.binding.expected_contexts},{"observed_tx_contexts",part.binding.observed_contexts},
        {"repeat_words",part.binding.repeat_words},{"tx_rows",part.binding.rows.rows},{"capture_rows_observed",part.capture_rows},
        {"incomplete_entries",incomplete},{"phase_dependent_entries",dependent},{"untimed_rx_rows",part.binding.untimed_rx_rows},
        {"missing_rx_roles",part.missing.size()},{"unbound_tx_source_rows",part.binding.unbound_source_rows}});
  }
  std::map<std::string,JsonRecord> report{{"schema","emuflow.native-word-network-analysis/v1"},
      {"status",ready ? "observations-bound":"awaiting-observations"},{"physical_acceptance","pending"},
      {"original_path_sta_acceptance","pending"},{"scope","Native clock/interface setup-model analysis; original path dispositions, independent STA acceptance, startup/control and hold qualification are separate"},
      {"expected_partitions",parts.size()},{"observed_partitions",observed},{"canonical_transfers",transfers.size()},
      {"expected_tx_contexts",expected_contexts},{"observed_tx_contexts",observed_contexts},{"incomplete_entries",unknown_entries},
      {"phase_dependent_entries",dependent_entries},{"untimed_rx_rows",untimed_rx},{"partitions",JsonRecord(std::move(coverage))},
      {"maximum_sampling_cost_ps",JsonRecord()},{"required_reference_period_ps",JsonRecord()},{"setup_model_frequency_mhz",JsonRecord()}};
  Check(!verification_events || (ready && project),"native word verification export requires complete observations and projection");
  std::unique_ptr<AtomicBinaryWriter> events;
  if(verification_events){events=std::make_unique<AtomicBinaryWriter>(output,inputs);NativeWordCheckHeader(events->stream());}
  if(ready && project){
    std::vector<NativeWordPartitionCosts> observations;
    for(auto& p:parts)observations.push_back({p.name,&p.binding,p.clocks,p.catalog.rx_entries,p.missing,p.inputs,p.captures.get()});
    std::uint64_t window_number=0,event_count=0;
    const auto model=ProjectNativeWordNetwork(transfers,observations,windows,word,reference,maximum_repeat,
        [&](const auto& window,const auto& graph){
          if(events)event_count+=WriteNativeWordWindowChecks(events->stream(),window_number,window,transfers,graph,word);
          ++window_number;
        });
    if(events){Check(event_count>0,"native word verification has no timed windows");events->Commit();return;}
    report["status"]=JsonRecord("setup-model-projected");report["maximum_sampling_cost_ps"]=Optional(model.maximum_sampling_cost_ps);
    report["required_reference_period_ps"]=Optional(model.required_reference_period_ps);
    if(model.required_reference_period_ps && *model.required_reference_period_ps>0)
      report["setup_model_frequency_mhz"]=JsonRecord(1000000. / *model.required_reference_period_ps);
    report["external_source_contexts"]=JsonRecord(model.external_sources.size());
    report["missing_rx_origins"]=JsonRecord(model.missing_rx_origins);
    std::vector<JsonRecord> projected;
    for(const auto& item:model.windows){
      std::vector<JsonRecord> words;JsonRecord witness;
      if(item.transport.witness){
        const auto& w=*item.transport.witness;for(const auto& load:w.load_words)words.push_back(Optional(load));
        witness=JsonRecord(std::map<std::string,JsonRecord>{{"phase_word",w.phase_word},{"lower_offset_ps",w.lower_offset_ps},
            {"upper_offset_ps",w.upper_offset_ps},{"capture_transfer",transfers.at(w.capture_transfer).id},{"load_words",JsonRecord(std::move(words))}});
      }
      projected.emplace_back(std::map<std::string,JsonRecord>{{"launch_clock",item.window.launch.clock},
          {"launch_edge",static_cast<int>(item.window.launch.edge)},{"capture_clock",item.window.capture.clock},
          {"capture_edge",static_cast<int>(item.window.capture.edge)},{"reference_window_ps",item.window.reference_window_ps},
          {"maximum_sampling_cost_ps",Optional(item.maximum_sampling_cost_ps)},
          {"required_reference_period_ps",Optional(item.required_reference_period_ps)},{"phase_witness",witness}});
    }
    std::vector<JsonRecord> order;for(const auto& t:transfers)order.emplace_back(t.id);
    report["transfer_order"]=JsonRecord(std::move(order));report["windows"]=JsonRecord(std::move(projected));
  }
  PublishTextArtifact(output,inputs,[&](auto& stream){stream<<JsonRecord(report).Encode()<<'\n';});
}
}  // namespace emuflow::core
