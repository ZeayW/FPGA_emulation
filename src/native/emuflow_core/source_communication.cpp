#include "emuflow_core/source_communication.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/emuir_view.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/patron_ingress.hpp"
#include <algorithm>
#include <charconv>
#include <cmath>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <set>
#include <tuple>

namespace emuflow::core {
namespace {
void Check(bool valid,const char* message) {
  if(!valid)throw StageError(ErrorCode::kInvalidArtifact,message);
}
std::uint32_t Index(std::int64_t value) {
  Check(value>=0 && std::uint64_t(value)<UINT32_MAX,"source communication index exceeds native domain");
  return static_cast<std::uint32_t>(value);
}
std::string Text(SqliteDatabase& db,const std::string& sql) {
  auto row=db.Prepare(sql);Check(row.Step(),"source communication metadata is missing");
  auto result=row.TypedText(0);Check(!result.empty() && !row.Step(),"source communication metadata is invalid");return result;
}
void Attach(SqliteDatabase& db,const ArtifactRef& input,const char* name) {
  auto row=db.Prepare(std::string("ATTACH DATABASE ? AS ")+name);row.BindText(1,SqliteImmutableUri(input.path()));row.Execute();
}
std::string Hex(const std::string& value) {
  JsonQuote(value);Check(!value.empty() && value.find('\0')==std::string::npos,"source communication pin identity is invalid");
  const char* digits="0123456789abcdef";std::string result;result.reserve(value.size()*2);
  for(unsigned char c:value){result+=digits[c>>4];result+=digits[c&15];}return result;
}
std::string Sdc(const std::string& value) {
  std::string result;for(char c:value){if(c=='\\')result+='\\';result+=c;}return result;
}
struct Edge { std::uint32_t from,to,ratio,depth; };
struct Route {
  std::uint32_t net,source;
  std::vector<std::uint32_t> sinks;
  std::vector<Edge> edges;
  std::map<std::uint32_t,double> delays;
};
void PrepareCommunicationPinShapes(SqliteDatabase& db) {
  db.Execute("CREATE TEMP TABLE communication_shapes AS WITH bits AS ("
      "SELECT e.instance_seq AS cell,p.name AS pin,e.bit AS bit,1 AS valid FROM communication_cells t "
      "CROSS JOIN endpoint_records e INDEXED BY endpoint_owner ON e.instance_seq=t.seq JOIN pin_names p ON p.seq=e.port_seq "
      "UNION ALL SELECT c.seq,json_extract(j.value,'$.port'),json_extract(j.value,'$.bit'),"
      "json_type(j.value,'$.port')='text' AND json_type(j.value,'$.bit')='integer' AND json_extract(j.value,'$.bit')>=0 "
      "FROM communication_cells t JOIN cell_records c ON c.seq=t.seq JOIN payloads p ON p.seq=c.constants_seq,json_each(p.value) j) "
      "SELECT cell,pin,max(bit)+1 AS width,min(valid) AS valid FROM bits GROUP BY cell,pin;"
      "CREATE UNIQUE INDEX communication_shapes_key ON communication_shapes(cell,pin);");
}
std::string CostPin(SqliteStatement& row,int cell,int port,int bit,int width,int valid) {
  const auto b=row.TypedInteger(bit),w=row.TypedInteger(width);
  Check(b>=0 && w>b && row.TypedInteger(valid)==1,"source communication pin shape is invalid");
  return Sdc(row.TypedText(cell))+"/"+Sdc(row.TypedText(port)+(w==1?"":"__"+std::to_string(b)));
}
}

void ExportSourceCommunication(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  Check(inputs.size()==4,"source communication input coverage differs");
  for(const auto& input:inputs)input.RequireUnchanged();
  CheckedRequestReader request(inputs[3].path(),1024*1024);
  const bool metric_request=inputs[3].schema()=="source-metric-request-v1";
  Check(request.Read<std::uint64_t>()==(metric_request?0x31514d53434d5545ULL:0x31514353434d5545ULL) && request.Read<std::uint32_t>()==1,
        "unsupported source communication request");
  for(unsigned i=0;i<3;++i) {
    const FileGeneration generation{request.Read<std::uint64_t>(),request.Read<std::uint64_t>(),request.Read<std::uint64_t>(),
      request.Read<std::int64_t>(),request.Read<std::int64_t>(),request.Read<std::int64_t>(),request.Read<std::int64_t>()};
    if(!(generation==inputs[i].generation()))throw StageError(ErrorCode::kMixedGeneration,"source communication parent generation changed");
  }
  double fixed=0.,ui=0.;std::uint32_t metric=0;
  if(metric_request)metric=request.Read<std::uint32_t>();
  else {fixed=request.Read<double>();ui=request.Read<double>();}
  const auto quantum=request.Read<std::uint32_t>(),maximum=request.Read<std::uint32_t>();
  const auto design=request.ReadString32(),platform=request.ReadString32(),semantic=request.ReadString32();request.RequireEnd();
  Check((metric_request?(metric>=1 && metric<=3):
        (std::isfinite(fixed) && fixed>=0 && std::isfinite(ui) && ui>0)) &&
        quantum==8 && maximum>=8 && maximum<=256 && maximum%8==0,
        "source communication requires the declared 8-bit serial planning model");
  Check(semantic.size()==64 && semantic.find_first_not_of("0123456789abcdef")==std::string::npos,
        "source communication semantic identity is invalid");
  EmuirView original(inputs[0]);AssignmentView assignment(inputs[1]);SqliteStore calendar(inputs[2]);
  Check(original.counts().cells<UINT32_MAX && original.counts().nets<UINT32_MAX && assignment.source_cells()==original.counts().cells,
        "source communication original/assignment dimensions differ");
  const auto cuts=assignment.ValidateCuts();auto& db=original.store().database();
  Attach(db,inputs[1],"assignment");Attach(db,inputs[2],"calendar");
  Check(Text(db,"SELECT json_extract(value,'$.name') FROM metadata WHERE key='design_json'")==design &&
        Text(db,"SELECT json_extract(value,'$') FROM assignment.metadata WHERE key='design'")==design &&
        Text(db,"SELECT json_extract(value,'$') FROM assignment.metadata WHERE key='platform'")==platform &&
        Text(db,"SELECT json_extract(value,'$') FROM assignment.metadata WHERE key='semantic_contract_sha256'")==semantic &&
        Text(db,"SELECT json_extract(value,'$.design') FROM calendar.metadata WHERE key='header'")==design &&
        Text(db,"SELECT json_extract(value,'$.platform') FROM calendar.metadata WHERE key='header'")==platform &&
        Text(db,"SELECT json_extract(value,'$.semantic_contract_sha256') FROM calendar.metadata WHERE key='header'")==semantic &&
        Text(db,"SELECT json_extract(value,'$.word_event_policy') FROM calendar.metadata WHERE key='header'")=="periodic-lvds-8bit-word-load-to-rx-visible-v2",
        "source communication belongs to another source, assignment or schedule");
  std::vector<std::string> parts;
  for(std::size_t i=0;i<assignment.partitions();++i)parts.push_back(assignment.PartitionName(assignment.Partition(i)));
  const auto part=[&](const std::string& name) {
    const auto found=std::lower_bound(parts.begin(),parts.end(),name);
    Check(found!=parts.end() && *found==name,"source communication route references an unknown FPGA");return std::uint32_t(found-parts.begin());
  };
  std::vector<std::uint32_t> owners(original.counts().cells,UINT32_MAX);
  auto cells=db.Prepare("SELECT seq,id FROM cell_records ORDER BY seq");
  assignment.VisitCopies([&](const InstanceCopy& copy) {
    const auto seq=copy.source.sequence();
    Check(!copy.replica && cells.Step() && cells.TypedInteger(0)==static_cast<std::int64_t>(seq) &&
          cells.TypedText(1)==copy.identity && seq<owners.size() && owners[seq]==UINT32_MAX,
          "source communication requires unique unchanged original cell ownership");
    owners[seq]=Index(copy.partition.sequence());
  });
  Check(!cells.Step() && std::find(owners.begin(),owners.end(),UINT32_MAX)==owners.end(),"source communication cell ownership is incomplete");
  std::vector<std::int32_t> by_net(original.counts().nets,-1);
  std::vector<Route> routes;std::set<std::uint32_t> seen_cuts;
  auto records=db.Prepare("SELECT r.seq,n.seq,p.part_index,c.cut_index,c.source_part_index,c.cut_class,n.cut_class FROM calendar.schedule_routes r "
      "LEFT JOIN net_records n ON n.id=r.net LEFT JOIN assignment.parts p ON p.fpga=r.source_fpga "
      "LEFT JOIN assignment.cuts c ON c.net_id=r.net ORDER BY r.seq");
  while(records.Step()) {
    Check(records.TypedInteger(0)==static_cast<std::int64_t>(routes.size()) && routes.size()<INT32_MAX,
          "source communication route order is invalid");
    const auto net=Index(records.TypedInteger(1)),source=Index(records.TypedInteger(2)),cut=Index(records.TypedInteger(3));
    Check(net<by_net.size() && by_net[net]<0 && source<parts.size() && records.TypedInteger(4)==source &&
          records.TypedText(5)==records.TypedText(6) && seen_cuts.insert(cut).second,
          "source communication route/cut identity is missing or repeated");
    by_net[net]=routes.size();routes.push_back({net,source,{},{},{}});
  }
  Check(routes.size()==cuts.records && seen_cuts.size()==cuts.records,"source communication omits an assigned cut");
  auto sinks=db.Prepare("SELECT r.seq,s.seq,p.part_index,c.source_part_index FROM calendar.schedule_route_sinks s "
      "LEFT JOIN calendar.schedule_routes r ON r.seq=s.route_seq LEFT JOIN assignment.parts p ON p.fpga=s.sink_fpga "
      "LEFT JOIN assignment.cut_sinks c ON c.net_id=r.net AND c.sink_part_index=p.part_index ORDER BY s.route_seq,s.seq");
  std::uint64_t sink_records=0;
  while(sinks.Step()) {
    const auto route=Index(sinks.TypedInteger(0)),sink=Index(sinks.TypedInteger(2));
    Check(route<routes.size() && sink<parts.size() && sink!=routes[route].source &&
          sinks.TypedInteger(1)==static_cast<std::int64_t>(routes[route].sinks.size()) && sinks.TypedInteger(3)==routes[route].source,
          "source communication scheduled sink differs from its assigned cut");
    auto& values=routes[route].sinks;Check(std::find(values.begin(),values.end(),sink)==values.end(),"source communication repeats a sink");
    values.push_back(sink);++sink_records;
  }
  Check(sink_records==cuts.sinks,"source communication omits an assigned sink");
  using EdgeKey=std::tuple<std::uint32_t,std::string,std::uint32_t,std::uint32_t>;
  std::set<EdgeKey> edges;
  auto route_edges=db.Prepare("SELECT e.route_seq,e.seq,e.link_id,e.source_fpga,e.sink_fpga FROM calendar.schedule_route_edges e ORDER BY e.route_seq,e.seq");
  std::vector<std::uint32_t> edge_counts(routes.size());
  while(route_edges.Step()) {
    const auto r=Index(route_edges.TypedInteger(0)),from=part(route_edges.TypedText(3)),to=part(route_edges.TypedText(4));
    Check(r<routes.size() && route_edges.TypedInteger(1)==edge_counts[r]++ && from!=to &&
          !route_edges.TypedText(2).empty() && edges.emplace(r,route_edges.TypedText(2),from,to).second,
          "source communication route edge is invalid or repeated");
  }
  auto entries=db.Prepare("SELECT e.seq,r.seq,e.net,r.net,e.source_fpga,e.sink_fpga,e.link_id,e.tdm_ratio,e.hop "
      "FROM calendar.schedule_entries e LEFT JOIN calendar.schedule_routes r ON r.demand=e.demand ORDER BY e.seq");
  std::uint64_t entry_records=0;
  while(entries.Step()) {
    const auto r=Index(entries.TypedInteger(1)),from=part(entries.TypedText(4)),to=part(entries.TypedText(5));
    const auto ratio=Index(entries.TypedInteger(7)),depth=Index(entries.TypedInteger(8));
    Check(entries.TypedInteger(0)==static_cast<std::int64_t>(entry_records++) && r<routes.size() && entries.TypedText(2)==entries.TypedText(3) &&
          ratio>=quantum && ratio<=maximum && ratio%quantum==0 && edges.erase({r,entries.TypedText(6),from,to})==1,
          "source communication entry differs from its route or ratio domain");
    routes[r].edges.push_back({from,to,ratio,depth});
  }
  Check(edges.empty(),"source communication schedule omits a route edge");
  for(auto& route:routes) {
    Check(!route.sinks.empty() && !route.edges.empty(),"source communication route is empty");
    std::map<std::uint32_t,std::size_t> incoming;
    std::map<std::uint32_t,std::vector<std::size_t>> outgoing;
    for(std::size_t i=0;i<route.edges.size();++i) {
      Check(route.edges[i].to!=route.source && incoming.emplace(route.edges[i].to,i).second,"source communication route is not a directed tree");
      outgoing[route.edges[i].from].push_back(i);
    }
    route.delays.emplace(route.source,0.);std::vector<std::uint32_t> ready{route.source};
    std::size_t visited=0;
    for(std::size_t cursor=0;cursor<ready.size();++cursor) {
      const auto children=outgoing.find(ready[cursor]);if(children==outgoing.end())continue;
      for(const auto index:children->second) {
        const auto& edge=route.edges[index];const auto predecessor=route.delays.find(edge.from);
        const auto previous=incoming.find(edge.from);
        Check(edge.depth==(edge.from==route.source?0:route.edges.at(previous->second).depth+1),"source communication hop depth differs from its tree");
        const double increment=metric==0?fixed+ui*edge.ratio:metric==1?1.:metric==2?0.:double(edge.ratio);
        const double delay=metric==2?std::max(predecessor->second,double(edge.ratio)):predecessor->second+increment;
        Check(std::isfinite(delay) && delay>=0 && route.delays.emplace(edge.to,delay).second,"source communication delay or tree is invalid");
        ready.push_back(edge.to);++visited;
      }
    }
    Check(visited==route.edges.size(),"source communication route has a cycle or disconnected relay");
    for(const auto sink:route.sinks)Check(route.delays.count(sink),"source communication sink is unreachable");
    if(metric_request)for(const auto& value:route.delays)
      Check(value.second>=0 && value.second<8388608. && std::floor(value.second)==value.second,
            "source metric route exceeds the exact integer scoring range");
    std::sort(route.sinks.begin(),route.sinks.end());
  }
  db.Execute("CREATE TEMP TABLE communication_arcs(seq INTEGER PRIMARY KEY,fcell INTEGER,fpin INTEGER,fbit INTEGER,"
      "tcell INTEGER,tpin INTEGER,tbit INTEGER,delay REAL,UNIQUE(tcell,tpin,tbit));"
      "CREATE TEMP TABLE communication_cells(seq INTEGER PRIMARY KEY) WITHOUT ROWID;");
  auto insert=db.Prepare("INSERT INTO communication_arcs VALUES(?,?,?,?,?,?,?,?)");
  auto selected_cell=db.Prepare("INSERT OR IGNORE INTO communication_cells VALUES(?)");
  auto classes=db.Prepare("SELECT seq,cut_class FROM net_records ORDER BY seq");bool have_class=classes.Step();
  std::vector<EmuirEndpoint> drivers,sink_pins;
  std::uint64_t connections=0,crossing_nets=0;
  const std::set<std::string> data_classes={"register_output","register_input","combinational"};
  const std::set<std::string> all_classes={"clock","reset","primary_input","register_output","register_input","combinational","multi_driver","undriven"};
  {
    SqliteTransaction transaction(db);
    original.VisitConnectivity([&](const EmuirEndpoint& endpoint) {
      Check(have_class && classes.TypedInteger(0)==static_cast<std::int64_t>(endpoint.net.sequence()),"source communication net metadata differs");
      if(!data_classes.count(classes.TypedText(1)))return;
      if(endpoint.role==EndpointRole::kDriver)drivers.push_back(endpoint);
      else if(endpoint.cell)sink_pins.push_back(endpoint);
    },[&](EmuirNetId net) {
      Check(have_class && classes.TypedInteger(0)==static_cast<std::int64_t>(net.sequence()) && all_classes.count(classes.TypedText(1)),
            "source communication net classification is invalid");
      const auto index=by_net[net.sequence()];
      if(data_classes.count(classes.TypedText(1))) {
        Check(drivers.size()==1 && drivers.front().cell.has_value(),"source communication data net has no unique cell driver");
        const auto& driver=drivers.front();const auto from=owners.at(driver.cell->sequence());std::set<std::uint32_t> remote;
        for(const auto& sink:sink_pins)if(owners.at(sink.cell->sequence())!=from)remote.insert(owners.at(sink.cell->sequence()));
        Check((index>=0)==!remote.empty(),"source communication route coverage disagrees with complete original connectivity");
        if(index>=0) {
          auto& route=routes.at(index);
          Check(route.source==from && route.sinks==std::vector<std::uint32_t>(remote.begin(),remote.end()),
                "source communication route sink ownership differs from complete original connectivity");
          ++crossing_nets;
          for(const auto& sink:sink_pins) {
            const auto to=owners.at(sink.cell->sequence());if(to==from)continue;
            insert.BindInteger(1,connections++);insert.BindInteger(2,driver.cell->sequence());insert.BindInteger(3,driver.pin->sequence());insert.BindInteger(4,driver.bit);
            insert.BindInteger(5,sink.cell->sequence());insert.BindInteger(6,sink.pin->sequence());insert.BindInteger(7,sink.bit);
            insert.BindReal(8,route.delays.at(to));insert.Execute();insert.Reset();
            for(const auto cell:{driver.cell->sequence(),sink.cell->sequence()}){selected_cell.BindInteger(1,cell);selected_cell.Execute();selected_cell.Reset();}
          }
        }
      } else Check(index<0,"source communication route names an unsupported global or non-data net");
      drivers.clear();sink_pins.clear();have_class=classes.Step();
    },true);
    Check(!have_class && crossing_nets==routes.size(),"source communication original crossing coverage is incomplete");
    transaction.Commit();
  }
  // Union connected and constant pin shapes for every participating cell.
  // A constant high bit still makes bit zero a scalarized bus pin.
  PrepareCommunicationPinShapes(db);
  auto output_rows=db.Prepare("SELECT a.seq,f.id,fp.name,a.fbit,fs.width,fs.valid,t.id,tp.name,a.tbit,ts.width,ts.valid,a.delay "
      "FROM communication_arcs a LEFT JOIN cell_records f ON f.seq=a.fcell LEFT JOIN pin_names fp ON fp.seq=a.fpin "
      "LEFT JOIN communication_shapes fs ON fs.cell=a.fcell AND fs.pin=fp.name "
      "LEFT JOIN cell_records t ON t.seq=a.tcell LEFT JOIN pin_names tp ON tp.seq=a.tpin "
      "LEFT JOIN communication_shapes ts ON ts.cell=a.tcell AND ts.pin=tp.name ORDER BY a.seq");
  AtomicBinaryWriter writer(output,inputs);auto& stream=writer.stream();
  stream<<(metric_request?"from_pin_hex\tto_pin_hex\tinteger_weight\n":"from_pin_hex\tto_pin_hex\textra_delay_ns\n")<<std::setprecision(17);
  std::uint64_t emitted=0;
  while(output_rows.Step()) {
    Check(output_rows.TypedInteger(0)==static_cast<std::int64_t>(emitted++),"source communication emitted order differs");
    stream<<Hex(CostPin(output_rows,1,2,3,4,5))<<'\t'<<Hex(CostPin(output_rows,6,7,8,9,10))<<'\t'<<output_rows.TypedReal(11)<<'\n';
  }
  Check(emitted==connections && stream.good(),"source communication emitted coverage differs");writer.Commit();
  if(metric_request) {
    const char* name=metric==1?"physical-hops":metric==2?"maximum-net-route-ratio":"serial-ratio-sum";
    std::cout<<JsonRecord(std::map<std::string,JsonRecord>{{"schema","emuflow.source-metric-weights/v1"},{"status","pass"},
      {"metric",name},{"weight_scale_ns",std::uint64_t(1000000000)},{"interpretation","integer-path-metric-not-physical-time"},
      {"crossing_nets",crossing_nets},{"sink_partitions",sink_records},{"route_edges",entry_records},{"connections",connections}}).Encode()<<'\n';
  } else std::cout<<JsonRecord(std::map<std::string,JsonRecord>{{"schema","emuflow.source-communication-export/v1"},{"status","pass"},
    {"cost_policy","per-hop-fixed-plus-serial-ui-times-ratio-planning-bound-v1"},{"crossing_nets",crossing_nets},
    {"sink_partitions",sink_records},{"route_edges",entry_records},{"connections",connections},{"fixed_delay_ns",fixed},{"serial_ui_ns",ui}}).Encode()<<'\n';
}

void ExportPatronSourceProxy(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  PatronBuildContext context;
  const auto model=BuildPatronModel(inputs,context,false);
  const auto transitions=emuflow::native::BuildPatronSourceTransitions(model);
  EmuirView original(inputs.at(0));
  Check(context.source_cell_cluster.size()==original.counts().cells &&
        context.source_net_native.size()==original.counts().nets,
        "PATRON source profile ownership coverage differs");
  using Key=std::tuple<int,int,int>;
  std::map<Key,const emuflow::native::PatronSourceTransition*> by_branch;
  for(const auto& branch:transitions)
    Check(by_branch.emplace(Key{branch.net,branch.source,branch.sink},&branch).second,
          "PATRON source profile repeats a branch");
  std::set<Key> observed;
  auto& db=original.store().database();
  db.Execute("CREATE TEMP TABLE communication_arcs(seq INTEGER PRIMARY KEY,fcell INTEGER,fpin INTEGER,fbit INTEGER,"
      "tcell INTEGER,tpin INTEGER,tbit INTEGER,delay REAL,hops INTEGER,ratio INTEGER,UNIQUE(tcell,tpin,tbit));"
      "CREATE TEMP TABLE communication_cells(seq INTEGER PRIMARY KEY) WITHOUT ROWID;");
  auto insert=db.Prepare("INSERT INTO communication_arcs VALUES(?,?,?,?,?,?,?,?,?,?)");
  auto selected_cell=db.Prepare("INSERT OR IGNORE INTO communication_cells VALUES(?)");
  auto classes=db.Prepare("SELECT seq,cut_class FROM net_records ORDER BY seq");
  bool have_class=classes.Step();
  const std::set<std::string> data_classes={"register_output","register_input","combinational"};
  const std::set<std::string> all_classes={"clock","reset","primary_input","register_output","register_input","combinational","multi_driver","undriven"};
  const auto owner=[&](const EmuirEndpoint& endpoint) {
    Check(endpoint.cell.has_value(),"PATRON source data pin has no original cell");
    return model.cluster.at(context.source_cell_cluster.at(endpoint.cell->sequence())).part;
  };
  std::vector<EmuirEndpoint> drivers,sinks;
  std::uint64_t connections=0,crossing_nets=0;
  {
    SqliteTransaction transaction(db);
    original.VisitConnectivity([&](const EmuirEndpoint& endpoint) {
      Check(have_class && classes.TypedInteger(0)==static_cast<std::int64_t>(endpoint.net.sequence()),
            "PATRON source net metadata differs");
      if(!data_classes.count(classes.TypedText(1)))return;
      if(endpoint.role==EndpointRole::kDriver)drivers.push_back(endpoint);
      else if(endpoint.cell)sinks.push_back(endpoint);
    },[&](EmuirNetId net) {
      Check(have_class && classes.TypedInteger(0)==static_cast<std::int64_t>(net.sequence()) &&
            all_classes.count(classes.TypedText(1)),"PATRON source net classification is invalid");
      if(data_classes.count(classes.TypedText(1))) {
        Check(drivers.size()==1 && drivers.front().cell.has_value(),"PATRON source data net has no unique original driver");
        const auto& driver=drivers.front();const auto from=owner(driver);
        bool crossing=false;
        for(const auto& sink:sinks) {
          const auto to=owner(sink);if(from==to)continue;
          const auto native=context.source_net_native.at(net.sequence());
          Check(native>=0,"PATRON model omits a crossing in complete original connectivity");
          const Key key{native,from,to};const auto found=by_branch.find(key);
          Check(found!=by_branch.end(),"PATRON model omits an actual remote data sink");
          observed.insert(key);crossing=true;const auto& value=*found->second;
          insert.BindInteger(1,connections++);insert.BindInteger(2,driver.cell->sequence());
          insert.BindInteger(3,driver.pin->sequence());insert.BindInteger(4,driver.bit);
          insert.BindInteger(5,sink.cell->sequence());insert.BindInteger(6,sink.pin->sequence());insert.BindInteger(7,sink.bit);
          insert.BindReal(8,value.delay_ns);insert.BindInteger(9,value.hops);insert.BindInteger(10,value.maximum_ratio);
          insert.Execute();insert.Reset();
          for(const auto cell:{driver.cell->sequence(),sink.cell->sequence()}) {
            selected_cell.BindInteger(1,cell);selected_cell.Execute();selected_cell.Reset();
          }
        }
        if(crossing)++crossing_nets;
      }
      drivers.clear();sinks.clear();have_class=classes.Step();
    },true);
    Check(!have_class && observed.size()==by_branch.size(),"PATRON profile contains non-data or absent source branches");
    transaction.Commit();
  }
  PrepareCommunicationPinShapes(db);
  auto rows=db.Prepare("SELECT a.seq,f.id,fp.name,a.fbit,fs.width,fs.valid,t.id,tp.name,a.tbit,ts.width,ts.valid,a.delay,a.hops,a.ratio "
      "FROM communication_arcs a LEFT JOIN cell_records f ON f.seq=a.fcell LEFT JOIN pin_names fp ON fp.seq=a.fpin "
      "LEFT JOIN communication_shapes fs ON fs.cell=a.fcell AND fs.pin=fp.name "
      "LEFT JOIN cell_records t ON t.seq=a.tcell LEFT JOIN pin_names tp ON tp.seq=a.tpin "
      "LEFT JOIN communication_shapes ts ON ts.cell=a.tcell AND ts.pin=tp.name ORDER BY a.seq");
  AtomicBinaryWriter writer(output,inputs);auto& stream=writer.stream();
  stream<<"from_pin_hex\tto_pin_hex\tphysical_hops\tmaximum_net_route_ratio\tpredicted_delay_ns\n"<<std::setprecision(17);
  std::uint64_t emitted=0;
  while(rows.Step()) {
    Check(rows.TypedInteger(0)==static_cast<std::int64_t>(emitted++),"PATRON source emitted order differs");
    stream<<Hex(CostPin(rows,1,2,3,4,5))<<'\t'<<Hex(CostPin(rows,6,7,8,9,10))<<'\t'
          <<rows.TypedInteger(12)<<'\t'<<rows.TypedInteger(13)<<'\t'<<rows.TypedReal(11)<<'\n';
  }
  Check(emitted==connections && stream.good(),"PATRON source emitted coverage differs");writer.Commit();
  std::cout<<JsonRecord(std::map<std::string,JsonRecord>{{"schema","emuflow.patron-source-proxy/v1"},{"status","pass"},
      {"coverage","complete-original-data-connectivity"},{"profile","patron-predicted-routes-and-domain-ratios-v1"},
      {"connections",connections},{"crossing_nets",crossing_nets},{"branches",std::uint64_t(transitions.size())},
      {"selected_paths_loaded",std::uint64_t(model.paths)}}).Encode()<<'\n';
}

void ExportSourceNetMap(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  Check(inputs.size()==2 || inputs.size()==3,"source net-map parent coverage differs");
  CheckedRequestReader request(inputs.back().path(),1024);
  Check(request.Read<std::uint64_t>()==0x31514d4e534d5545ULL && request.Read<std::uint32_t>()==1 &&
        request.Read<std::uint32_t>()==inputs.size()-1,"unsupported source net-map request");
  for(std::size_t i=0;i<inputs.size()-1;++i) {
    const FileGeneration generation{request.Read<std::uint64_t>(),request.Read<std::uint64_t>(),request.Read<std::uint64_t>(),
      request.Read<std::int64_t>(),request.Read<std::int64_t>(),request.Read<std::int64_t>(),request.Read<std::int64_t>()};
    if(!(generation==inputs[i].generation()))throw StageError(ErrorCode::kMixedGeneration,"source net-map parent generation changed");
  }
  request.RequireEnd();
  EmuirView source(inputs[0]);auto& db=source.store().database();
  const auto design=Text(db,"SELECT json_extract(value,'$.name') FROM metadata WHERE key='design_json'");
  JsonQuote(design);
  struct Port { std::int64_t width;std::string direction; };
  std::map<std::string,Port> ports;
  auto declarations=db.Prepare("SELECT seq,json_extract(record_json,'$.id'),json_extract(record_json,'$.width'),"
      "json_extract(record_json,'$.direction') FROM port_records ORDER BY seq");
  while(declarations.Step()) {
    const auto name=declarations.TypedText(1),direction=declarations.TypedText(3);
    JsonQuote(name);
    const auto width=declarations.TypedInteger(2);
    Check(declarations.TypedInteger(0)==static_cast<std::int64_t>(ports.size()) && !name.empty() &&
          width>0 && (direction=="input" || direction=="output" || direction=="inout") &&
          ports.emplace(name,Port{width,direction}).second,"source net-map port declaration is invalid");
  }
  AtomicBinaryWriter writer(output,inputs);auto& stream=writer.stream();
  stream<<"mapped_net_hex\temuir_net_hex\n";
  auto nets=db.Prepare("SELECT seq,id FROM net_records ORDER BY seq");
  std::uint64_t count=0;
  while(nets.Step()) {
    Check(nets.TypedInteger(0)==static_cast<std::int64_t>(count),"source net-map net order differs");
    stream<<Hex("__emuflow_net_"+std::to_string(count++))<<'\t'<<Hex(nets.TypedText(1))<<'\n';
  }
  Check(count==source.counts().nets,"source net-map net coverage differs");
  std::map<std::string,std::pair<std::uint64_t,std::string>> aliases;
  auto endpoints=db.Prepare("SELECT p.name,e.bit,e.role,e.net_seq,n.id FROM endpoint_records e INDEXED BY endpoint_owner "
      "JOIN pin_names p ON p.seq=e.port_seq JOIN net_records n ON n.seq=e.net_seq WHERE e.instance_seq IS NULL");
  while(endpoints.Step()) {
    const auto name=endpoints.TypedText(0);const auto port=ports.find(name);
    const auto bit=endpoints.TypedInteger(1),role=endpoints.TypedInteger(2),net=endpoints.TypedInteger(3);
    Check(port!=ports.end() && bit>=0 && bit<port->second.width && net>=0 && std::uint64_t(net)<count &&
          (role==0 || role==1) && (port->second.direction=="inout" || (port->second.direction=="input"?role==0:role==1)),
          "source net-map top-level endpoint differs from its declared port");
    const auto alias=name+(port->second.width==1?"":"["+std::to_string(bit)+"]");
    // get_property(net, name) is the raw net name, rather than the escaped
    // hierarchical pin path used by the communication table.
    const auto value=std::make_pair(std::uint64_t(net),endpoints.TypedText(4));
    const auto found=aliases.find(alias);
    Check(found==aliases.end() || found->second==value,"source net-map port alias is ambiguous");
    aliases.emplace(alias,value);
  }
  std::uint64_t emitted_aliases=0;
  for(const auto& [alias,value]:aliases) {
    const std::string prefix="__emuflow_net_";
    if(alias.compare(0,prefix.size(),prefix)==0) {
      std::uint64_t index=0;
      const auto parsed=std::from_chars(alias.data()+prefix.size(),alias.data()+alias.size(),index);
      if(parsed.ec==std::errc{} && parsed.ptr==alias.data()+alias.size() && index<count && alias==prefix+std::to_string(index)) {
        Check(index==value.first,"source port aliases a different generated timing wire");
        continue;
      }
    }
    stream<<Hex(alias)<<'\t'<<Hex(value.second)<<'\n';++emitted_aliases;
  }
  Check(stream.good(),"source net-map output failed");writer.Commit();
  std::cout<<JsonRecord(std::map<std::string,JsonRecord>{{"schema","emuflow.source-net-map/v1"},{"status","pass"},
      {"design",design},{"nets",count},{"port_aliases",emitted_aliases}}).Encode()<<'\n';
}
}
