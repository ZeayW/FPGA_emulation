#ifndef ROUTER_TDMASSIGN_H_
#define ROUTER_TDMASSIGN_H_

#include "../database/database.h"
#include "../utils.h"
#include "./routegraph.h"
#include "./routetree.h"

namespace router {

struct Connection {
  IndexType from, to;
  IndexType net_id;
  IndexType sll_delay = 0;
  CostType  connect_delay;
  CostType  delay_bar;
  Connection() {}
  Connection(IndexType const &v0, IndexType const &v1, IndexType const &v2) : from(v0), to(v1), net_id(v2) {}
};

struct Edge {
  IndexType from, to;
  IndexType cap;

  Edge() {}
  Edge(IndexType const &v0, IndexType const &v1, IndexType const &v2) : from(v0), to(v1), cap(v2) {}
};

struct TDMAssignParam {
  CostType  alpha              = 3.0;
  CostType  beta               = 1.0;
  IndexType window_size        = 10;
  CostType  break_percent      = 0.0027;
  IndexType multi_thread_thres = 200000;
  CostType  remain_margin      = 2.0;
};

class TDMAssigner {
 public:
  TDMAssigner() {}

  void           tdmAssign(database::Database &database, RouteGraph &routegraph, RouteTrees &routetrees);
  void           outputFile(database::Database &database, RouteGraph &routegraph, RouteTrees &routetrees);
  void           loadRatioAndLegalize(database::Database                       &database,
                                      RouteGraph                               &routegraph,
                                      RouteTrees                               &routetrees,
                                      std::vector<std::vector<CostType>> const &init_ratios);
  TDMAssignParam params;

 protected:
  void initConnectionAndEdge(database::Database &database, RouteGraph &routegraph, RouteTrees &routetrees);
  void legalizeTDMRatio(database::Database &database, RouteGraph &routegraph, RouteTrees &routetrees);

  std::vector<Connection>             connections;
  std::vector<Edge>                   edges;
  std::vector<std::vector<IndexType>> connection_edge_usage;
  std::vector<std::vector<CostType>>  net_edge_ratio;
  std::vector<IndexType>              edge_from_use, edge_to_use;
};

}   // namespace router
#endif