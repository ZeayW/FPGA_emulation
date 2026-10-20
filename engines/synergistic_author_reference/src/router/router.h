#ifndef ROUTER_ROUTER_H_
#define ROUTER_ROUTER_H_

#include "../database/database.h"
#include "../utils.h"
#include "routegraph.h"
#include "./routetree.h"

namespace router {

class Router {
 public:
  Router() {}
  ~Router() {}

  void run(database::Database &database);

  void buildRouteGraph(database::Database &database);
  //void writeTxTResult(std::string const &file_name, std::string const &content);
  // CostType calculateSlackRatio(database::Net &net,CostType &delay);
  //void adjustWeights(database::Net &net, CostType &delay, RouteGraph &graph);
  void adjustWeights(database::Database &database,RouteTree &tree,RouteGraph &graph);
  //void testMethod(database::Database &database, RouteGraph &graph, RouteTrees &trees);
  void optimize(database::Database &database,RouteTrees &trees, RouteGraph &graph,std::vector<IndexType> &net_id);

  void writeDatabase(database::Database &database);
 private:
  RouteGraph routegraph_;
};

}   // namespace router
#endif