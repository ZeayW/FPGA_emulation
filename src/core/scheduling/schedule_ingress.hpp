#pragma once
#include "scheduling/schedule_model.hpp"
#include "common/stage_runtime.hpp"
#include <map>
#include <tuple>

namespace emuflow::core {
struct ExactScheduleArc {
  std::uint32_t link, from, to, capacity, offset, budget, affine, fixed_latency;
  double base, slope, period;
};
using ExactScheduleArcKey = std::tuple<std::uint32_t,std::uint32_t,std::uint32_t>;
struct ExactScheduleBoard {
  std::uint32_t frame, barrier, settle, minimum, maximum, quantum;
  ExactScheduleCatalogs catalogs;
  std::map<ExactScheduleArcKey,ExactScheduleArc> arcs;
};
// Shared bounded input grammar, independent of solver/model decisions.
ExactScheduleBoard ReadExactScheduleBoard(const ArtifactRef& routes,
                                          const std::string& request);
ExactScheduleModel BuildExactScheduleModel(const std::string& route_store,
                                          const std::string& request,
                                          ExactScheduleBuildContext& context);
}
