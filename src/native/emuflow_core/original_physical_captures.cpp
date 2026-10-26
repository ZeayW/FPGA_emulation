#include "emuflow_core/original_physical_captures.hpp"
#include <algorithm>
#include <cmath>
#include <iomanip>
#include <memory>
#include <set>
#include <sstream>

namespace emuflow::core {
namespace {
void Check(bool valid,const char* message) {if(!valid)throw StageError(ErrorCode::kInvalidArtifact,message);}
void Digest(const std::string& value) {Check(value.size()==64 && value.find_first_not_of("0123456789abcdef")==std::string::npos,"capture binding parent digest is invalid");}
std::string Header(SqliteDatabase& db,const std::string& field) {
  auto row=db.Prepare("SELECT json_extract(value,?1) FROM metadata WHERE key='header'");row.BindText(1,"$."+field);
  Check(row.Step(),"original capture cost header is missing");return row.TypedText(0);
}
void Attach(SqliteDatabase& db,const ArtifactRef& ref,const char* name) {
  auto row=db.Prepare(std::string("ATTACH DATABASE ? AS ")+name);row.BindText(1,SqliteImmutableUri(ref.path()));row.Execute();
}
std::uint32_t Index(std::int64_t value) {Check(value>=0 && std::uint64_t(value)<UINT32_MAX,"capture coordinate is invalid");return value;}
std::uint32_t Group(const std::string& name) {
  Check(name.rfind("segment",0)==0 && name.size()>=13 && name.find_first_not_of("0123456789",7)==std::string::npos,
        "capture semantic group spelling is invalid");
  const auto value=std::stoull(name.substr(7));Check(value<UINT32_MAX,"capture semantic coordinate is too large");
  std::ostringstream expected;expected<<"segment"<<std::setw(6)<<std::setfill('0')<<value;
  Check(expected.str()==name,"capture semantic group spelling is not canonical");return value;
}
std::string Escaped(const std::string& name) {std::string result;for(char c:name){if(c=='\\')result+='\\';result+=c;}return result;}
std::string OriginalPin(SqliteDatabase& db,const OriginalEndpoint& endpoint,bool source) {
  Check(endpoint.bit<=INT64_MAX,"original capture endpoint coordinate is too large");
  if(endpoint.instance) {
    auto cell=db.Prepare("SELECT seq FROM cell_records WHERE id=?");cell.BindText(1,*endpoint.instance);
    Check(cell.Step(),"original capture source cell is missing");const auto seq=cell.TypedInteger(0);
    auto pins=db.Prepare("SELECT max(e.bit)+1,sum(CASE WHEN e.bit=?3 AND e.role=?4 THEN 1 ELSE 0 END) "
        "FROM endpoint_records e JOIN pin_names p ON p.seq=e.port_seq WHERE e.instance_seq=?1 AND p.name=?2");
    pins.BindInteger(1,seq);pins.BindText(2,endpoint.port);pins.BindInteger(3,endpoint.bit);pins.BindInteger(4,source?0:1);
    Check(pins.Step() && pins.TypedInteger(0)>0 && std::uint64_t(pins.Integer(0))>endpoint.bit && pins.TypedInteger(1)>0,
          "original capture endpoint is not a connected source/sink pin");
    return Escaped(*endpoint.instance)+"/"+(pins.Integer(0)==1?endpoint.port:endpoint.port+"["+std::to_string(endpoint.bit)+"]");
  }
  auto port=db.Prepare("SELECT json_extract(record_json,'$.width'),json_extract(record_json,'$.direction') FROM port_records "
      "WHERE json_extract(record_json,'$.id')=?");port.BindText(1,endpoint.port);
  Check(port.Step() && port.TypedInteger(0)>0 && std::uint64_t(port.Integer(0))>endpoint.bit &&
        port.TypedText(1)==(source?"input":"output"),"original capture top-level endpoint is invalid");
  const auto width=port.Integer(0);
  auto connected=db.Prepare("SELECT 1 FROM endpoint_records e JOIN pin_names p ON p.seq=e.port_seq "
      "WHERE e.instance_seq IS NULL AND p.name=? AND e.bit=? AND e.role=? LIMIT 1");
  connected.BindText(1,endpoint.port);connected.BindInteger(2,endpoint.bit);connected.BindInteger(3,source?0:1);
  Check(connected.Step(),"original capture top-level endpoint is unconnected");
  return width==1?endpoint.port:endpoint.port+"["+std::to_string(endpoint.bit)+"]";
}
struct Reader {
  SqliteStore logic,local;
  ArtifactRef boundary;
  NativeOptimizationProofView* proof;
  std::uint32_t part;
  std::uint64_t consumed_local=0,consumed_cross=0;
  Reader(const OriginalPhysicalPartition& input,std::uint32_t index):logic(input.logic_costs),local(input.local_costs),
      boundary(input.boundary_identity),proof(input.optimization),part(index) {}
};
void Disposition(SqliteStatement& row,int measurement,int proof,int data,int sampling,
    Reader& owner,const std::vector<std::string>& endpoints,OriginalPhysicalCapture& capture,bool local) {
  const auto kind=row.TypedText(measurement);
  if(kind=="optimized-away") {
    Check(owner.proof && row.IsNull(data) && row.IsNull(sampling) && !row.IsNull(proof),
          "original capture elimination lacks its checked proof or has numerical costs");
    capture.eliminated=owner.proof->Require(row.TypedText(proof),endpoints);owner.proof->RequireOwner(*capture.eliminated);
  } else {
    const bool supported=local?(kind=="explicit-routed-path-chain" || kind=="endpoint-longest-path-fallback"):
        (kind=="endpoint-exact" || kind=="cut-net-cone-upper-bound" || kind=="optimized-endpoint-collection-upper-bound");
    Check(supported && row.IsNull(proof),
          "original capture measurement is unsupported or mixed with elimination");
    const auto d=row.TypedReal(data),e=row.TypedReal(sampling);
    Check(std::isfinite(d) && d>=0 && std::isfinite(e) && std::isfinite(e-d),"original capture lacks finite physical data/check costs");
    capture.cost=OriginalCaptureCost{d,e,kind};
  }
}
}
void OriginalCaptureCoverage::RequireUnchanged() const {for(const auto& ref:inputs)ref.RequireUnchanged();}
OriginalCaptureCoverage VisitOriginalPhysicalCaptures(OriginalTimingView& original,const ArtifactRef& source,
    const BoundPhysicalWordModel& words,const std::string& assignment_seal,const std::vector<OriginalPhysicalPartition>& partitions,
    const std::function<void(const OriginalTimingPath&,const OriginalPhysicalCapture&)>& visitor) {
  Digest(assignment_seal);Check(words.semantics && original.selector_sha256()==words.context.source.path_database_sha256,
        "original capture population differs from the word binding");
  Check(source.schema()=="emuir-compact-v2","original captures require their compact original source");
  std::optional<ArtifactRef> calendar;
  for(const auto& ref:words.inputs)if(ref.schema()=="tdm-schedule-store-v4") {Check(!calendar,"capture calendar repeats");calendar=ref;}
  Check(calendar.has_value(),"original cross captures lack their schedule owner");
  bool paired=false;for(const auto& ref:words.inputs)if(ref.schema()==source.schema()) {
    ArtifactScope(ref).RequireOwner(source);paired=true;
  }
  Check(paired,"original capture source is not the word binding's source");
  const auto& exact=*words.semantics;const auto& context=words.context;
  Check(partitions.size()==exact.parts().size(),"original capture partition coverage differs");
  SqliteStore original_source(source);OriginalCaptureCoverage coverage;
  coverage.inputs={source,original.artifact()};
  coverage.inputs.insert(coverage.inputs.end(),words.inputs.begin(),words.inputs.end());
  std::vector<std::unique_ptr<Reader>> readers(exact.parts().size());
  for(const auto& input:partitions) {
    Check(input.logic_costs.schema()=="logic-segment-timing-store-v3" && input.local_costs.schema()=="local-path-timing-store-v3" &&
          input.boundary_identity.schema()=="boundary-identity-store-v1","original captures require current physical cost owners");
    SqliteStore header(input.logic_costs);const auto name=Header(header.database(),"fpga");
    const auto part=std::lower_bound(exact.parts().begin(),exact.parts().end(),name);
    Check(part!=exact.parts().end() && *part==name,"original capture partition is unknown");const auto index=part-exact.parts().begin();
    Check(!readers[index],"original capture partition repeats");auto owner=std::make_unique<Reader>(input,index);
    for(auto* store:{&owner->logic,&owner->local}) {
      auto& db=store->database();Check(Header(db,"fpga")==name && Header(db,"design")==context.design &&
          Header(db,"status")=="pass" && Header(db,"sampling_cost_policy")=="vivado-clock-aware-reference-v1",
          "original capture physical identity or cost policy differs");
    }
    auto& local=owner->local.database();
    Check(Header(local,"identity_schema")=="emuflow.local-path-identity/v2","original local captures require current identity semantics");
    Check(Header(local,"source.original_ir_sha256")==context.source.original_ir_sha256 &&
          Header(local,"source.path_database_sha256")==original.selector_sha256() &&
          Header(local,"source.routes_sha256")==context.source.routes_sha256 && Header(local,"source.assignment_sha256")==assignment_seal,
          "original local captures belong to another source or assignment");
    auto count=local.Prepare("SELECT json_extract(value,'$.source.original_paths') FROM metadata WHERE key='header'");
    Check(count.Step() && count.TypedInteger(0)==static_cast<std::int64_t>(original.count()),"original capture population count differs");
    auto& db=owner->logic.database();
    Check(Header(db,"platform")==context.platform && Header(db,"system_routes_sha256")==context.source.routes_sha256 &&
          Header(db,"tdm_schedule_sha256")==context.source.schedule_sha256 && Header(db,"semantic_contract_sha256")==context.source.semantic_sha256,
          "original cross captures belong to another physical context");
    for(const auto* required:{&input.logic_costs,&input.boundary_identity}) {
      bool matched=false;
      for(const auto& ref:words.inputs)if(ref.path()==required->path()) {ArtifactScope(ref).RequireOwner(*required);matched=true;}
      Check(matched,"original captures are not paired with their word physical owners");
    }
    Attach(db,input.boundary_identity,"boundary");Attach(db,*calendar,"calendar");
    // Original capture members have no leading-key index in the canonical
    // ordered segment owner. Build a private integer lookup once; never scan
    // all physical segments again for each original member.
    db.Execute("CREATE TEMP TABLE original_capture_lookup(member TEXT PRIMARY KEY,seq INTEGER NOT NULL) WITHOUT ROWID;"
        "INSERT INTO original_capture_lookup SELECT member_path,seq FROM segments WHERE kind='capture' AND system_path NOT GLOB 'static-exact-contract:*'");
    for(const auto* table:{"unsupported_members","unmeasured_segments"}) {
      auto unsupported=db.Prepare(std::string("SELECT 1 FROM ")+table+" LIMIT 1");Check(!unsupported.Step(),"original physical capture has unsupported or unmeasured members");
    }
    if(owner->proof) {
      Check(owner->proof->fpga()==name,"original capture proof belongs to another partition");
      for(const auto& ref:owner->proof->artifacts())coverage.inputs.push_back(ref);
    }
    for(const auto* ref:{&input.logic_costs,&input.local_costs,&input.boundary_identity})coverage.inputs.push_back(*ref);
    readers[index]=std::move(owner);
  }
  std::map<std::pair<std::uint32_t,std::uint32_t>,std::uint32_t> incoming;
  for(std::uint32_t i=0;i<words.transfers.size();++i) {
    const auto& transfer=words.transfers[i];Check(incoming.emplace(std::make_pair(transfer.cut,transfer.to),i).second,
        "original capture word binding repeats a routed arrival");
  }
  original.Visit([&](const auto& path) {
    original.RequireOwner(path.source);
    const auto end_pin=OriginalPin(original_source.database(),path.end,false);
    std::optional<OriginalPhysicalCapture> found;
    for(auto& reader:readers) {
      auto& owner=*reader;
      auto local=owner.local.database().Prepare("SELECT seq,clock_domain,clock_period_ns,start_pin,end_pin,measurement,optimization_source,delay_ns,sampling_cost_ns FROM timings WHERE id=?");
      local.BindText(1,path.identity);
      if(local.Step()) {
        Check(!found,"original member has more than one physical disposition");
        const auto start_pin=OriginalPin(original_source.database(),path.start,true);
        Check(local.TypedText(1)==path.budget.target_clock && local.TypedReal(2)==path.budget.target_period_ns &&
              local.TypedText(3)==start_pin && local.TypedText(4)==end_pin,"original local cost belongs to another endpoint or clock");
        OriginalPhysicalCapture capture{owner.part,false,{},owner.local.Id<LocalCaptureCostTag>(Index(local.TypedInteger(0))),{}, {}};
        std::vector<std::string> cells;if(path.start.instance)cells.push_back(*path.start.instance);if(path.end.instance)cells.push_back(*path.end.instance);
        Disposition(local,5,6,7,8,owner,cells,capture,true);Check(!local.Step(),"original local capture identity repeats");
        found=std::move(capture);++owner.consumed_local;
      }
      auto& db=owner.logic.database();auto cross=db.Prepare("SELECT s.seq,s.static_exact_segment_id,s.start_pin,s.end_pin,s.start_object_kind,s.end_object_kind,"
          "s.measurement,s.optimization_source,s.delay_ns,s.sampling_cost_ns FROM original_capture_lookup k JOIN segments s ON s.seq=k.seq WHERE k.member=?");
      cross.BindText(1,path.identity);
      if(cross.Step()) {
        Check(!found,"original member has both local and cross physical dispositions");const auto group=Group(cross.TypedText(1));
        Check(group<exact.segments().size(),"original capture semantic coordinate is unknown");const auto& semantic=exact.segments()[group];
        Check(semantic.kind==2 && semantic.part==owner.part && cross.TypedText(4)=="pin" && cross.TypedText(5)==(path.end.instance?"pin":"port") &&
              cross.TypedText(3)==end_pin,"original cross capture belongs to another endpoint or semantic role");
        const auto arrival=incoming.find({semantic.source,owner.part});Check(arrival!=incoming.end(),"original cross capture has no canonical RX event");
        const auto& transfer=words.transfers[arrival->second];
        auto shadow=db.Prepare("SELECT r.instance,r.ordinal FROM boundary.endpoints b JOIN boundary.boundary_registers r ON r.endpoint_id=b.id "
            "WHERE b.kind='rx' AND b.schedule_entry=(SELECT entry_id FROM calendar.schedule_entries WHERE seq=?) ORDER BY r.ordinal");
        shadow.BindInteger(1,transfer.entry.sequence());Check(shadow.Step() && shadow.TypedInteger(1)==0,"original capture RX shadow is missing");
        const auto cell=shadow.TypedText(0);Check(!shadow.Step() && cross.TypedText(2)==Escaped(cell)+"/Q","original cross capture starts at another RX shadow");
        OriginalPhysicalCapture capture{owner.part,true,arrival->second,owner.logic.Id<WordLogicCostTag>(Index(cross.TypedInteger(0))),{}, {}};
        std::vector<std::string> cells{cell};if(path.end.instance)cells.push_back(*path.end.instance);
        Disposition(cross,6,7,8,9,owner,cells,capture,false);Check(!cross.Step(),"original cross capture member repeats");
        found=std::move(capture);++owner.consumed_cross;
      }
    }
    Check(found.has_value(),"original member lacks a measured or proven optimized-away physical capture");
    ++coverage.original;if(found->crosses)++coverage.cross;else ++coverage.local;if(found->eliminated)++coverage.optimized_away;
    visitor(path,*found);
  });
  for(auto& owner:readers) {
    auto local=owner->local.database().Prepare("SELECT count(*) FROM timings");auto cross=owner->logic.database().Prepare("SELECT count(*) FROM original_capture_lookup");
    Check(local.Step() && cross.Step() && local.TypedInteger(0)==static_cast<std::int64_t>(owner->consumed_local) &&
          cross.TypedInteger(0)==static_cast<std::int64_t>(owner->consumed_cross),"physical capture owners contain extra original members");
    if(owner->proof)owner->proof->RequireUnchanged();
  }
  Check(coverage.original==original.count(),"original physical capture coverage differs");
  original.RequireUnchanged();exact.RequireUnchanged();words.RequireUnchanged();coverage.RequireUnchanged();return coverage;
}
}  // namespace emuflow::core
