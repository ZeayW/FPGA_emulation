#include "physical/openparf/placement_records.hpp"
#include "physical/openparf/runtime.hpp"
#include <cmath>
namespace emuflow::core {
namespace {
using namespace openparf_input;
double Number(const R &r) {
  if (auto p = std::get_if<I>(&r.value))
    return double(*p);
  if (auto p = std::get_if<double>(&r.value)) {
    Check(std::isfinite(*p), "nonfinite convergence metric");
    return *p;
  }
  throw StageError(ErrorCode::kInvalidArtifact,
                   "OpenPARF convergence metric is not a number");
}
bool True(const R &r, const std::string &key) {
  return r.has(key) && r.at(key).encode() == "true";
}
} // namespace
JsonRecord ValidateOpenparfMetrics(const ArtifactRef &metrics, bool detailed) {
  using namespace openparf_input;
  auto v = Read(metrics);
  Check(v.at("schema").text() == "emuflow.openparf-native-convergence/v1",
        "native convergence certificate is invalid");
  const auto &policy = v.at("policy"), &sll = policy.at("sll_weighting"),
             &fill = policy.at("density_filler_sampling");
  Check(sll.at("provider").text() == "leaps-fixed-adam-step-v2" &&
            Number(sll.at("step_size")) > 0 &&
            Number(sll.at("delta_sll_ema_weight")) > 0 &&
            Number(sll.at("delta_sll_ema_weight")) <= 1 &&
            sll.at("accepted_range").array().size() == 2 &&
            Number(sll.at("accepted_range").array()[0]) == 0 &&
            Number(sll.at("accepted_range").array()[1]) == 1000,
        "native SLL weighting policy is invalid");
  Check(fill.at("provider").text() == "density-bin-bounded-fillers-v2" &&
            fill.at("status").text() == "pass" &&
            fill.at("maximum_bin_span").array().size() == 2 &&
            Number(fill.at("maximum_bin_span").array()[0]) == 0.5 &&
            Number(fill.at("maximum_bin_span").array()[1]) == 0.5,
        "native density filler policy is invalid");
  const auto resources = std::get_if<O>(&fill.at("resources").value);
  Check(resources && !resources->empty(),
        "native density filler policy is invalid");
  for (const auto &entry : *resources) {
    const auto &g = entry.second;
    Check(g.at("count").integer() > 0,
          "native density filler policy is invalid");
    for (const auto *key : {"size", "bin_size", "bin_span"})
      Check(g.at(key).array().size() == 2,
            "native density filler policy is invalid");
    for (unsigned a = 0; a < 2; ++a) {
      const auto size = Number(g.at("size").array()[a]),
                 bin = Number(g.at("bin_size").array()[a]),
                 span = Number(g.at("bin_span").array()[a]);
      Check(size > 0 && bin > 0 && span > 0 && span <= 0.5 + 1e-6 &&
                std::abs(span - size / bin) <=
                    std::max(1e-9, 1e-9 * std::max(std::abs(span),
                                                   std::abs(size / bin))),
            "native density filler policy is invalid");
    }
  }
  const auto status = v.at("status").text(),
             reason = v.at("stop_reason").text();
  const bool feasible =
      status == "pass" && std::set<std::string>{"feasible-hpwl-patience",
                                                "maximum-iterations-feasible",
                                                "empty-active-subspace"}
                              .count(reason);
  const bool diagnostic = detailed && status == "diagnostic" &&
                          std::set<std::string>{"diagnostic-handoff-patience",
                                                "maximum-iterations-diagnostic"}
                              .count(reason) &&
                          True(v, "restored_best_diagnostic");
  Check(feasible || diagnostic, "native convergence certificate is invalid");
  if (reason == "feasible-hpwl-patience" ||
      reason == "maximum-iterations-feasible")
    Check(True(v, "restored_best_feasible"),
          "did not restore its best feasible placement");
  Check(v.at("iterations").integer() >= 0,
        "native termination iteration is invalid");
  for (const auto *key : {"best_feasible_hpwl", "final_legal_hpwl"})
    if (v.has(key) && !v.at(key).null())
      (void)Number(v.at(key));
  if (diagnostic) {
    Check(!True(v, "restored_best_feasible"),
          "diagnostic handoff cannot claim a restored feasible iterate");
    const auto limit = Number(policy.at("diagnostic_handoff_overflow"));
    Check(limit > 0.1 && limit <= 0.25 &&
              policy.at("diagnostic_handoff_patience").integer() >= 0,
          "diagnostic legalization evidence is invalid");
    const auto overflow =
        std::get_if<O>(&v.at("best_diagnostic_logic_overflow").value);
    Check(overflow && !overflow->empty(),
          "diagnostic legalization evidence is invalid");
    for (const auto &entry : *overflow)
      Check(Number(entry.second) <= limit + 1e-9,
            "diagnostic legalization evidence is invalid");
    const auto &d = v.at("diagnostic_legalization_displacement");
    const auto mean = Number(d.at("mean")), maximum = Number(d.at("maximum"));
    Check(d.at("instances").integer() > 0 && mean >= 0 && maximum >= mean,
          "diagnostic legalization evidence is invalid");
  }
  metrics.RequireUnchanged();
  return openparf_placement::Record(v);
}
} // namespace emuflow::core
