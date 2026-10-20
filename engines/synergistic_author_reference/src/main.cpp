#include "./router/router.h"
#include "database/database.h"
#include "parser/parser.h"
#include "router/iccadloader.h"
#include "router/topologyloader.h"

int main(int argc, char **argv) {
  if (argc == 1) {
    std::cout << "[usage] ./router /path/to/testcase_dir" << std::endl;
    std::cout << "[usage] ./router -load_iccad_result /path/to/testcase_dir /path/to/iccad_result.txt" << std::endl;
    std::cout << "[usage] ./router -load_contest_result /path/to/testcase_dir /path/to/design.route.out" << std::endl;
  }

  if (argc == 4) {
    if (argv[1][6] == 'i') {
      database::Database database;
      buildDatabase(argv[2], database);
      router::RouteGraph routegraph;
      router::RouteTrees routetrees;
      router::loadICCADRouterResult(argv[3], database, routegraph, routetrees);
    } else if (argv[1][6] == 'c') {
      database::Database database;
      buildDatabase(argv[2], database);
      router::RouteGraph routegraph;
      router::RouteTrees routetrees;
      router::loadContestRouterResult(argv[3], database, routegraph, routetrees);
    } else {
      std::cout << "illegal command" << std::endl;
    }
    return 0;
  }
  std::chrono::high_resolution_clock::time_point load_s, load_t;
  load_s = std::chrono::high_resolution_clock::now();
  database::Database database;
  buildDatabase(argv[1], database);
  load_t = std::chrono::high_resolution_clock::now();
  std::chrono::duration<double, std::ratio<1, 1>> d_load(load_t - load_s);
  std::cout << "load data: " << d_load.count() << "s" << std::endl;
  // for (IndexType i=0;i<database.netlist().size();i++){
  //     std::cout << std::endl;
  //     std::cout << database.net(i).id() << std::endl;
  //     std::cout << database.net(i).drivenNodeId() << std::endl;
  //     for(IndexType j=0;j<database.net(i).loadenNodeId().size();j++){
  //         std::cout << database.net(i).loadenNodeId()[j] << " ";
  //     }
  // }

  router::Router router;
  router.run(database);
  // router.writeDatabase(database);
  return 0;
}