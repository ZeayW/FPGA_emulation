// Offline W0 reader differential. Build the same source against each exact
// core/header revision; never include this hashing pass in a production handoff.
#include "emuflow_core/emuir_view.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/json_record.hpp"

#include <iostream>
#include <sstream>
#include <filesystem>

int main(int argc,char** argv) {
  using namespace emuflow::core;
  if (argc!=4) { std::cerr<<"usage: native_store_view_probe SCHEMA EMUIR SUMMARY\n";return 2; }
  std::error_code error;
  if (std::filesystem::weakly_canonical(argv[2])==std::filesystem::weakly_canonical(argv[3]) ||
      std::filesystem::equivalent(argv[2],argv[3],error)) {
    std::cerr<<"store probe output aliases its source\n";return 2;
  }
  Sha256 digest;
  std::uint64_t cells=0,nets=0,endpoints=0,pins=0,stateful=0,top=0;
  const auto number=[&](std::uint64_t value) {
    unsigned char bytes[8];for (unsigned i=0;i<8;++i) bytes[i]=static_cast<unsigned char>(value>>(8*i));
    digest.update(bytes,8);
  };
  const auto result=RunStage("qualification.store-view",[&] {
    return std::vector<ArtifactRef>{ArtifactRef::Bind(argv[2],argv[1])};
  },[&](const auto& refs) {
    EmuirView view(refs[0]);
    view.VisitCellStates([&](auto id,bool state) { number(0);number(id.sequence());number(state);++cells;stateful+=state; });
    view.VisitConnectivity([&](const auto& endpoint) {
      number(1);number(endpoint.net.sequence());number(endpoint.role==EndpointRole::kSink);number(endpoint.ordinal);
      number(endpoint.cell ? endpoint.cell->sequence()+1 : 0);number(endpoint.pin ? endpoint.pin->sequence()+1 : 0);
      ++endpoints;pins+=endpoint.pin.has_value();top+=!endpoint.cell.has_value();
    },[&](auto net) { number(2);number(net.sequence());++nets; },true);
    if (view.counts().cells!=cells || view.counts().nets!=nets || view.counts().endpoints!=endpoints)
      throw StageError(ErrorCode::kInvalidArtifact,"store probe coverage disagrees");
  });
  std::string hex;
  for (auto value:digest.finish()) { hex+="0123456789abcdef"[value>>4];hex+="0123456789abcdef"[value&15]; }
  const auto facts=JsonRecord(std::map<std::string,JsonRecord>{{"cells",cells},{"nets",nets},{"endpoints",endpoints},
      {"pins",pins},{"stateful",stateful},{"top_endpoints",top},{"ordered_sha256",hex}}).Encode();
  const auto output="{\"schema\":\"emuflow.native-store-view-probe/v1\",\"operation\":"+
      StageResultJson(result)+",\"facts\":"+facts+"}";
  AtomicBinaryWriter writer(argv[3]);writer.WriteBytes(output.data(),output.size());writer.Commit();
  return result.ok() ? 0 : 1;
}
