#ifndef ROUTER_Dijkstra_H_
#define ROUTER_Dijkstra_H_

#include "../database/database.h"
#include "../utils.h"
#include "./routegraph.h"
#include "./routetree.h"
/*
namespace router {
class Dijkstra {
 public:
  Dijkstra() {}

  void dijkstra_router(IndexType               source_node_die_id,
                       std::vector<IndexType>& target_die_id,
                       database::Database&     database,
                       RouteGraph&             routegraph,
                       //    RouteGraph&            graph,
                       //    database::Net&          net,
                       CostType                Aij,
                       RouteTree&              tree,
                       IndexType&              iter);
};

}   // namespace router
*/
namespace router {
class Dijkstra {
 public:
  Dijkstra() {}

  void dijkstra_router(database::Database& database, RouteGraph& routegraph, RouteTree& tree,IndexType& state);
};
}   // namespace router
#endif