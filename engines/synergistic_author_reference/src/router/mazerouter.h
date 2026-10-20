#ifndef MAZEROUTER_H_
#define MAZEROUTER_H_

#include "routegraph.h"
#include "routetree.h"

namespace router {
struct MazeRoutingParams {
  CostType  path_max_delay     = 10000.0;
  CostType  delay_crititcal    = 0.5;
  IndexType tdm_max_capacity   = 1000000;
  bool      ignore_sll_cap     = false;
  CostType  pres_fac           = 1.0;
  bool      need_resort        = false;
  bool      need_total_reroute = false;
  bool      cap_constr_strict  = false;
  CostType  sll_delay_estimate = 1.8;
  bool      is_small_case      = false;
  bool      use_estimate_delay = false;
  bool      is_tiny_case       = false;
};

class MazeRouter {
 public:
  MazeRouter() {}
  MazeRouter(MazeRoutingParams const& p) : params(p) {}
  ~MazeRouter() {}

  bool routeSingleNet(database::Database& database, RouteGraph& routegraph, RouteTree& routetree, database::Net& net);
  bool routeSingleSink(database::Database& database, RouteGraph& routegraph, RouteTree& routetree, IndexType sink);
  bool run(MazeRoutingParams const& p, database::Database& database, RouteGraph& routegraph, RouteTrees& routetrees);

  bool runConnectionRoute(MazeRoutingParams const& p,
                          database::Database&      database,
                          RouteGraph&              routegraph,
                          RouteTrees&              routetrees);

  void initializeEstimateDelay(database::Database& database, RouteGraph& routegraph);

 protected:
  void              checkCongAndRipup(database::Database& database, RouteGraph& routegraph, RouteTree& routetree);
  bool              checkCong(database::Database& database, RouteGraph& routegraph, RouteTrees& routetrees);
  MazeRoutingParams params;

  std::vector<std::vector<CostType>> estimate_delay_;
};

}   // namespace router

#endif