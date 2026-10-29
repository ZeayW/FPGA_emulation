#include "physical/flow/context.hpp"
namespace emuflow::core {
ArtifactRef PreparePhysicalFlowCompileRequest(
    const ArtifactRef &policy,
    const std::vector<std::pair<std::string, ArtifactRef>> &dynamic,
    const std::string &output) {
  using namespace physical_flow;
  Check(policy.schema() == "physical-flow-policy-v1",
        "invalid compile physical policy role");
  auto config = openparf_input::Read(policy);
  BoundedJson(config.encode())
      .Keys({"schema", "sources", "backend", "backend_options", "workers",
             "seed"});
  Check(config.at("schema").text() == "emuflow.physical-flow-policy/v1",
        "invalid compile physical policy version");
  std::vector<ArtifactRef> refs{policy};
  for (const auto &row : config.at("sources").array()) {
    auto ref =
        ArtifactRef::Bind(row.at("path").text(), row.at("schema").text());
    Check(Generation(ref).encode() == row.at("generation").encode(),
          "compile physical tool/package source changed");
    refs.push_back(ref);
  }
  auto rows = config.at("sources").array();
  rows.push_back(Source("compile-policy", policy));
  for (const auto &row : dynamic) {
    rows.push_back(Source(row.first, row.second));
    refs.push_back(row.second);
  }
  config["schema"] = "emuflow.physical-flow-request/v1";
  config["sources"] = rows;
  config["resume"] = false;
  config["selected_fpgas"] = R{};
  return Request(output, config, "physical-flow-request-v1", refs);
}
} // namespace emuflow::core
