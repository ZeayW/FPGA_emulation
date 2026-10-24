#include "emuflow_core/store.hpp"
#include "emuflow_core/exact_captures.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/emuir_view.hpp"
#include "emuflow_core/native_stages.hpp"
#include "emuflow_core/ordered_record_table.hpp"
#include "emuflow_core/resource_view.hpp"
#include <algorithm>
#include <array>
#include <filesystem>
#include <vector>

namespace emuflow::core {
namespace {
void Check(bool valid,const char* message) {
  if (!valid) throw StageError(ErrorCode::kInvalidArtifact,message);
}
struct Proof { std::uint64_t coordinates=0, captures=0; };

// This checker never reads the producer's normalization/index relation. It
// independently binds raw topology coordinates to source endpoints/resources,
// canonicalizes its own set, then compares one ordered capture at a time.
Proof CheckCaptures(const std::vector<ArtifactRef>& inputs,const std::string& scratch) {
  Check(inputs.size()==5,"capture validation artifact coverage disagrees");
  for (const auto& input:inputs) input.RequireUnchanged();
  EmuirView source_view(inputs[0]);
  BinaryReader request(inputs[1].path()), topology(inputs[2].path()), parts_file(inputs[3].path());
  ReadAndValidateHeader(request,{0x31584553464d5545ULL,4,"indexed exact request"});
  ReadAndValidateHeader(topology,{0x315845534f4d5545ULL,3,"exact topology"});
  ReadAndValidateHeader(parts_file,{0x31504553464d5545ULL,1,"exact parts"});
  auto cells=request.Read<std::uint32_t>(),nets=request.Read<std::uint32_t>();
  auto cuts=request.Read<std::uint32_t>(),parts=request.Read<std::uint32_t>();
  auto sinks=request.Read<std::uint32_t>();
  Check(cells==source_view.counts().cells && nets==source_view.counts().nets &&
        parts && parts<=65536 && parts_file.Read<std::uint32_t>()==parts &&
        parts<=parts_file.remaining()/5 && cells<=request.remaining()/4 &&
        topology.Read<std::uint32_t>()==cuts,"capture validation source dimensions disagree");
  Proof proof; proof.coordinates=topology.Read<std::uint64_t>();
  std::vector<std::string> part_ids;
  for (std::uint32_t p=0;p<parts;++p) {
    auto value=parts_file.ReadString32();
    Check(value.find('\0')==std::string::npos && (p==0 || value>part_ids.back()),"capture validation part order disagrees");
    part_ids.push_back(std::move(value));
  }
  parts_file.RequireEnd();
  std::vector<std::uint32_t> owners(cells);
  for (auto& part:owners) {
    part=request.Read<std::uint32_t>();
    Check(part<parts,"capture validation owner is invalid");
  }
  Check(cuts<=request.remaining()/12 && cuts<=topology.remaining()/5,"capture validation cut coverage is invalid");
  SqliteStore source_store(inputs[0]);
  auto& source=source_store.database();
  ResourceView resources(source);
  auto net_name=source.Prepare("SELECT id FROM net_records WHERE seq=?");
  std::vector<std::string> cut_ids;
  std::vector<std::uint64_t> sink_offsets{0};
  std::vector<std::uint32_t> sink_parts;
  for (std::uint32_t c=0;c<cuts;++c) {
    auto net=request.Read<std::uint32_t>(),from=request.Read<std::uint32_t>(),count=request.Read<std::uint32_t>();
    Check(net<nets && from<parts && count<=request.remaining()/4,"capture validation cut is invalid");
    net_name.BindInteger(1,net);
    Check(net_name.Step(),"capture validation cut name is missing");
    auto name=net_name.TypedText(0); net_name.Reset();
    Check(!name.empty() && (!c || name>cut_ids.back()),"capture validation cut order disagrees");
    cut_ids.push_back(std::move(name));
    for (std::uint32_t i=0;i<count;++i) {
      auto part=request.Read<std::uint32_t>();
      Check(part<parts && (!i || part>sink_parts.back()),"capture validation sink part is invalid");
      sink_parts.push_back(part);
    }
    sink_offsets.push_back(sink_parts.size());
    auto launch=topology.Read<std::uint8_t>(); auto edges=topology.Read<std::uint32_t>();
    Check(launch<=1 && edges<=cuts && edges<=topology.remaining()/4,"capture validation topology is invalid");
    std::uint32_t previous=0;
    for (std::uint32_t i=0;i<edges;++i) {
      auto predecessor=topology.Read<std::uint32_t>();
      Check(predecessor<cuts && (!i || predecessor>previous),"capture validation predecessor is invalid");
      previous=predecessor;
    }
  }
  request.RequireEnd();
  Check(proof.coordinates==topology.remaining()/16 && topology.remaining()%16==0,
        "capture validation coordinates are truncated");
  SqliteDatabase expected(scratch,SqliteDatabase::Mode::kCreateExclusive);
  expected.Execute("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE;"
      "CREATE TABLE expected(net TEXT NOT NULL,part TEXT NOT NULL,kind INTEGER NOT NULL,"
      "endpoint TEXT NOT NULL,port TEXT NOT NULL,bit_length INTEGER NOT NULL,bit BLOB NOT NULL,"
      "PRIMARY KEY(net,part,kind,endpoint,port,bit_length,bit)) WITHOUT ROWID");
  SqliteTransaction transaction(expected);
  auto insert=expected.Prepare("INSERT OR IGNORE INTO expected VALUES(?,?,?,?,?,?,?)");
  // LEFT joins preserve malformed source rows so missing identities cannot
  // silently shift dense endpoint ordinals or disappear from validation.
  auto endpoints=source.Prepare("SELECT e.net_seq,e.ordinal,e.instance_seq,c.id,p.name,e.bit,c.resources_seq "
      "FROM endpoint_records e LEFT JOIN cell_records c ON c.seq=e.instance_seq "
      "LEFT JOIN pin_names p ON p.seq=e.port_seq WHERE e.role=1 ORDER BY e.net_seq,e.ordinal");
  std::int64_t previous_net=-1,previous_ordinal=-1;
  std::uint64_t dense=0,observed_sinks=0;
  auto advance=[&]() {
    if (!endpoints.Step()) return false;
    auto net=endpoints.TypedInteger(0),ordinal=endpoints.TypedInteger(1);
    Check(net>=0 && net<nets && ordinal>=0 && net>=previous_net &&
          (net!=previous_net || ordinal>previous_ordinal),"capture validation source endpoint order is invalid");
    dense=net==previous_net ? dense+1 : 0; previous_net=net; previous_ordinal=ordinal;
    Check(dense<=UINT32_MAX && ++observed_sinks<=sinks,"capture validation source sink coverage disagrees");
    return true;
  };
  bool have=advance(),have_previous=false;
  std::array<std::uint32_t,4> previous{};
  for (std::uint64_t i=0;i<proof.coordinates;++i) {
    auto cut=topology.Read<std::uint32_t>(),part=topology.Read<std::uint32_t>();
    auto net=topology.Read<std::uint32_t>(),sink=topology.Read<std::uint32_t>();
    std::array<std::uint32_t,4> key{net,sink,cut,part};
    Check(cut<cuts && part<parts && net<nets && (!have_previous || key>previous),
          "capture validation coordinate is invalid");
    Check(std::binary_search(sink_parts.begin()+sink_offsets[cut],sink_parts.begin()+sink_offsets[cut+1],part),
          "capture validation coordinate is not a cut sink");
    previous=key; have_previous=true;
    while (have && (endpoints.Integer(0)<net || (endpoints.Integer(0)==net && dense<sink))) have=advance();
    Check(have && endpoints.Integer(0)==net && dense==sink,"capture validation source coordinate is missing");
    const auto bit=endpoints.TypedInteger(5);
    const auto port=endpoints.TypedText(4);
    Check(bit>=0 && !port.empty(),"capture validation source pin is invalid");
    const bool top=endpoints.IsNull(2);
    std::string endpoint,port_key,bit_key;
    if (top) {
      Check(endpoints.IsNull(3) && endpoints.IsNull(6),"capture validation top endpoint is invalid");
      endpoint="top:"+port+"["+std::to_string(bit)+"]";
    } else {
      auto owner=endpoints.TypedInteger(2);
      Check(owner>=0 && owner<cells && owners[owner]==part,"capture validation source ownership disagrees");
      endpoint=endpoints.TypedText(3);
      auto flags=resources.Get(endpoints.TypedInteger(6)).present;
      Check(!endpoint.empty() && (flags & ((1U<<1)|(1U<<2)|(1U<<5)|(1U<<6))),
            "capture validation endpoint is not architectural state");
      port_key=port;
      auto remaining=static_cast<std::uint64_t>(bit);
      do { bit_key.insert(bit_key.begin(),char(remaining&255)); remaining>>=8; } while (remaining);
    }
    insert.BindText(1,cut_ids[cut]); insert.BindText(2,part_ids[part]);
    insert.BindInteger(3,top?1:0); insert.BindText(4,endpoint); insert.BindText(5,port_key);
    insert.BindInteger(6,top?-1:static_cast<std::int64_t>(bit_key.size()));
    insert.BindBlob(7,bit_key.data(),bit_key.size()); insert.Execute();
  }
  topology.RequireEnd();
  while (have) have=advance();
  Check(observed_sinks==sinks,"capture validation source sink count disagrees");
  transaction.Commit();
  OrderedRecordTable captures(inputs[4].path(),0x31434553464d5545ULL,1);
  auto rows=expected.Prepare("SELECT net,part,kind,endpoint,port,bit FROM expected ORDER BY net,part,kind,endpoint,port,bit_length,bit");
  while (rows.Step()) {
    Check(proof.captures<captures.count,"capture validation result is missing records");
    const auto record=ReadExactCaptureRecord(captures);
    Check(record.net==rows.Text(0) && record.fpga==rows.Text(1) &&
          record.kind==(rows.Integer(2)?0:1) && record.endpoint==rows.Text(3) &&
          record.port==rows.Text(4) && record.bit==rows.Blob(5),
          "capture validation record disagrees with independent source");
    ++proof.captures;
  }
  Check(proof.captures==captures.count,"capture validation result has unrequested records");
  captures.reader.RequireEnd();
  for (const auto& input:inputs) input.RequireUnchanged();
  return proof;
}
}
void ValidateExactCaptures(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  StagedArtifact scratch(output,{".sqlite"});
  const auto proof=CheckCaptures(inputs,scratch.path()+".sqlite");
  AtomicBinaryWriter result(output);
  result.stream()<<"{\"schema\":\"emuflow.exact-capture-validation/v1\",\"status\":\"pass\",\"coordinates\":"
                 <<proof.coordinates<<",\"captures\":"<<proof.captures<<"}\n";
  result.Commit();
}
void BuildExactCaptures(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  Check(inputs.size()==4,"capture producer artifact coverage disagrees");
  StagedArtifact staged(output,{".sqlite",".verify.sqlite"});
  emuflow::native::ResolveStaticExactCaptures(inputs[0].path(),inputs[1].path(),inputs[2].path(),
                                           inputs[3].path(),staged.path(),false);
  // Producer decisions and its uniqueness spool are not validator evidence.
  std::filesystem::remove(staged.path()+".sqlite");
  auto checked=inputs;
  checked.push_back(ArtifactRef::Bind(staged.path(),"exact-captures-v1"));
  CheckCaptures(checked,staged.path()+".verify.sqlite");
  for (const auto& input:inputs) input.RequireUnchanged();
  PublishTemporaryFile(staged.path(),output);
}
}
