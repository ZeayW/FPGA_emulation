#pragma once
#include "common/stage_runtime.hpp"
#include "scheduling/schedule_ingress.hpp"
#include <functional>
#include <optional>
namespace emuflow::core {
struct ScheduledTimingHop {
  std::string entry,demand,link,from,to;
  std::uint64_t slot=0,ready=0;
  std::optional<std::uint64_t> ratio;
  double period=0,tx_time=0;
  bool affine=false;
};
struct ScheduledTimingCut {
  std::string net,demand,from,to;
  std::size_t first_hop=0,hop_count=0;
};
struct ScheduledTimingPath {
  std::uint64_t source_seq=0;
  std::string path,clock;
  double period=0,required=0,fixed=0,transport=0;
  bool explicit_members=false;
  std::uint64_t member_count=0;
  std::vector<ScheduledTimingCut> cuts;
  std::vector<ScheduledTimingHop> hops;
};
struct ScheduledTimingCounts { std::uint64_t paths=0,cuts=0,route_hops=0; };
// One active path at a time. Route choice, member continuity and concrete TX
// events share the Phase5 projection implementation; consumers do not parse a
// reconstructed Python or JSON hop population. The callback may not retain refs.
ScheduledTimingCounts VisitScheduledTimingPaths(
    const ArtifactRef& routes,const ArtifactRef& schedule,const ExactScheduleBoard& board,
    const std::function<void(const ScheduledTimingPath&)>& visitor);

// Ordered path projection and fixed-event/ratio timing. Paths remain in the
// source RouteStore; output contains only a sealed projection and summaries.
void ProjectScheduleTiming(const std::vector<ArtifactRef>& inputs,
                           const std::string& output);
}
