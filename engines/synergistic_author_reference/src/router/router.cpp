#include "router.h"

#include "../parser/parser.h"
#include "Dijkstra.h"
// #include "demandmanager.h"
#include "mazerouter.h"
// #include "pathscheduler.h"
#include "tdmassign.h"
namespace router {

void Router::writeDatabase(database::Database &database) {
  IndexType n = database.dies().size();
  IndexType m = 0;
  for (IndexType i = 0; i < n; i++)
    for (IndexType j = 0; j < i; j++)
      if (database.network(i, j)) m++;
  IndexType netlist_size = database.netlist().size();
  std::vector<int> avi(netlist_size, 0);
  for (auto& net : database.netlist()) {
    std::vector<int> vis(n, 0);
    for (auto node_id : net.loadenNodeId()) {
      IndexType die_id = database.node(node_id).DieId();
      if (die_id != database.node(net.drivenNodeId()).DieId()) {
        avi[net.id()] = 1;
      }
    }
    if (!avi[net.id()]) netlist_size--;
  }
  std::ofstream ofs("database.txt");
  ofs << n << ' ' << m << ' ' << netlist_size << ' ' << netlist_size << std::endl;
  for (IndexType i = 0; i < n; i++)
    for (IndexType j = i + 1; j < n; j++)
      if (database.network(i, j))
        ofs << i << ' ' << j << std::endl;
  for (auto& net : database.netlist()) {
    if (!avi[net.id()]) continue;
    ofs << database.node(net.drivenNodeId()).DieId();
    std::vector<int> vis(n, 0);
    for (auto node_id : net.loadenNodeId()) {
      IndexType die_id = database.node(node_id).DieId();
      if (!vis[die_id] && die_id != database.node(net.drivenNodeId()).DieId()) {
        vis[die_id] = 1;
        ofs << ' ' << die_id;
      }
    }
    ofs << std::endl;
  }
  // ofs << 0 << std::endl;
  for (IndexType i = 0; i < netlist_size; i++)
    ofs << i << std::endl;
  // ofs << std::endl;
}

void Router::run(database::Database &database) {
  std::chrono::high_resolution_clock::time_point init_s, init_t;
  init_s = std::chrono::high_resolution_clock::now();
  buildRouteGraph(database);
  Dijkstra                    ds;
  RouteTrees                  trees;
  std::vector<database::Net> &netlist      = database.netlist();
  IndexType                   netlist_size = netlist.size();
  std::vector<IndexType>      net_ids(netlist_size);   // 为了排序而产生的net列表
  IndexType                   state = 0;               // pre-routing
  trees.trees().resize(netlist_size);
  CostType max_delay = 0;
  for (IndexType i = 0; i < netlist_size; i++) net_ids[i] = i;

  // pre-routing
  for (IndexType i = 0; i < netlist_size; i++) {
    auto      &net  = database.net(net_ids[i]);
    RouteTree &tree = trees.trees()[net_ids[i]];

    tree.Init(database, routegraph_, net);
  }
  MazeRoutingParams maze_route_param;
  maze_route_param.delay_crititcal = 1.0;
  MazeRouter        mazerouter;
  maze_route_param.ignore_sll_cap = true;
  maze_route_param.cap_constr_strict = true;
  mazerouter.runConnectionRoute(maze_route_param, database, routegraph_, trees);
  // for (int i = 0; i < 1; i++) {
  // maze_route_param.use_estimate_delay = true;
  // mazerouter.initializeEstimateDelay(database, routegraph_);
  // maze_route_param.need_total_reroute = true;
  // mazerouter.runConnectionRoute(maze_route_param, database, routegraph_, trees);
  // }
  // mazerouter.run(maze_route_param, database, routegraph_, trees);
  IndexType vertex_num = routegraph_.vertexNum();
  // for (IndexType i = 0; i < vertex_num; i++)
  //   for (IndexType j = 0; j < i; j++)
  //     if (routegraph_.edge(i, j).Type() == SLL_EDGE) {
  //       std::cout << "SLL Edge " << i << " " << j
  //                 << " usage: " << routegraph_.edge(i, j).shareNum() + routegraph_.edge(j, i).shareNum()
  //                 << " cap: " << database.network(i, j) << std::endl;
  //     }
  // exit(0);
  // maze_route_param.ignore_sll_cap = false;
  for (auto &tree : trees.trees()) {
    tree.TrackBack();
    tree.calculateDelay(database, routegraph_);
    // 计算延迟
  }
  max_delay = 0;
  for (auto &net : netlist) {
    max_delay = std::max(max_delay, net.netMaxDelay());
  }
  std::cout << "Max delay after initial routing: " << max_delay << std::endl;
  init_t = std::chrono::high_resolution_clock::now();
  std::chrono::duration<double, std::ratio<1, 1>> d_init(init_t - init_s);
  std::cout << "initial routing: " << d_init.count() << "s" << std::endl;


  // DemandManager manager;
  // manager.calculateEdgeDemand(database, trees, routegraph_, DemandOptimizeTarget::MIN_TOTAL_TDM_USAGE);

  // PathScheduler scheduler;
  // scheduler.scheduleConnectPath(database, routegraph_, manager);
  // scheduler.scheduleNetPath(database, routegraph_, trees);
  // IndexType vertex_num = routegraph_.vertexNum();
  // for (auto &tree : trees.trees()) {
  //   auto targets = tree.targetsDieId();
  //   std::vector<IndexType> visited(vertex_num, 0);
  //   for (auto target : targets) {
  //     IndexType now = target;
  //     while (now != -1 && !visited[now]) {
  //       visited[now] = true;
  //       now = tree.parent()[now];
  //     }
  //   }
  //   for (IndexType i = 0; i < vertex_num; i++) {
  //     if (tree.parent()[i] != -1 && !visited[i]) {
  //       routegraph_.edge(tree.parent()[i], i).subShareNum();
  //       tree.changeParent(i, -1);
  //     }
  //   }
  //   tree.TrackBack();
  //   tree.calculateDelay(database, routegraph_);
  //   // 计算延迟
  // }
  // max_delay = 0;
  // for (auto &net : netlist) {
  //   max_delay = std::max(max_delay, net.netMaxDelay());
  // }
  // for (IndexType i = 0; i < vertex_num; i++)
  //   for (IndexType j = 0; j < i; j++) {
  //     if (routegraph_.edge(i, j).Type() == SLL_EDGE) {
  //       if (routegraph_.edge(i, j).shareNum() + routegraph_.edge(j, i).shareNum() > database.network(i, j)) {
  //         std::cout << "[Error] SLL Edge " << i << '-' << j << " cap: " << database.network(i, j) << " demand: " <<
  //         routegraph_.edge(i, j).shareNum() + routegraph_.edge(j, i).shareNum() << std::endl;
  //       }
  //     }
  //   }
  // std::cout << "Max delay after path schedule: " << max_delay << std::endl;

  // maze_route_param.tdm_max_capacity = 5000;
  // maze_route_param.path_max_delay   = 3000;
  // int32_t    max_iter = 100;
  // for (int32_t iter = 0; iter < max_iter; iter++) {
  //   maze_route_param.path_max_delay *= 1.1;
  //   // maze_route_param.tdm_max_capacity *= 1.1;
  //   maze_route_param.pres_fac *= 2.0;
  //   if (iter == 0) {
  //     maze_route_param.need_resort = true;
  //     maze_route_param.need_total_reroute = true;
  //   } else {
  //     maze_route_param.need_resort = true;
  //     maze_route_param.need_total_reroute = true;
  //   }
  //   std::cout << "Maze Routing Iter #" << iter << std::endl;
  //   bool res = mazerouter.run(maze_route_param, database, routegraph_, trees);
  //   if (!res) break;
  //   IndexType vertex_num = 0;
  //   for (IndexType i = 0; i < vertex_num; i++)
  //     for (IndexType j = 0; j < vertex_num; j++) {
  //       if (!database.network(i, j)) continue;
  //       if (routegraph_.edge(i, j).Type() == SLL_EDGE) {
  //         if (routegraph_.edge(i, j).shareNum() + routegraph_.edge(j, i).shareNum() > database.network(i, j)) {
  //           routegraph_.edge(i, j).addHistory(routegraph_.edge(i, j).shareNum() + routegraph_.edge(j, i).shareNum() -
  //                                             database.network(i, j));
  //         }
  //       } else {
  //         if (routegraph_.edge(i, j).shareNum() + routegraph_.edge(j, i).shareNum() >
  //             database.network(i, j) * maze_route_param.tdm_max_capacity) {
  //           routegraph_.edge(i, j).addHistory(routegraph_.edge(i, j).shareNum() + routegraph_.edge(j, i).shareNum() -
  //                                             database.network(i, j) * maze_route_param.tdm_max_capacity);
  //         }
  //       }
  //     }
  // }

  // for (auto &tree : trees.trees()) {
  //   tree.TrackBack();
  //   tree.calculateDelay(database, routegraph_);
  //   // 计算延迟
  // }
  // // CostType max_delay = 0;
  // for (auto &net : netlist) {
  //   max_delay = std::max(max_delay, net.netMaxDelay());
  // }
  // std::cout << "Max delay after Maze Routing " << max_delay << std::endl;
  // // exit(0);
  auto cmp = [&](IndexType x, IndexType y) {
    return database::Database::netCompareDelay(database.net(x), database.net(y));
  };
  // std::sort(net_ids.begin(), net_ids.end(), cmp);
  // 检查pre-routing 顺序结果
  //  for (IndexType i = 0; i < netlist_size; i++) {
  //    std::cout << database.net(net_ids[i]).lineNum() << " ";
  //  }
  //  squential-routing
  //  需要对edge进行初始化
  // {
  //   // IndexType                           vertex_num = routegraph_.vertexNum();
  //   // std::vector<std::vector<IndexType>> optim_dist(vertex_num);
  //   // for (IndexType i = 0; i < vertex_num; i++) {
  //   //   optim_dist[i].resize(vertex_num, 65536);
  //   //   optim_dist[i][i] = 0;
  //   // }

  //   // for (IndexType i = 0; i < vertex_num; i++)
  //   //   for (IndexType j = 0; j < vertex_num; j++)
  //   //     if (routegraph_.edge(i, j).Type() == SLL_EDGE) optim_dist[i][j] = 1;
  //   //     else if (routegraph_.edge(i, j).Type() == TDM_EDGE)
  //   //       optim_dist[i][j] = vertex_num + 1;

  //   // for (IndexType k = 0; k < vertex_num; k++)
  //   //   for (IndexType i = 0; i < vertex_num; i++)
  //   //     for (IndexType j = 0; j < vertex_num; j++)
  //   //       optim_dist[i][j] = std::min(optim_dist[i][j], optim_dist[i][k] + optim_dist[k][j]);

  //   // std::vector<CostType> net_criticality(netlist_size, 0);
  //   // for (IndexType i = 0; i < netlist_size; i++) {
  //   //   auto                  &tree = trees.gettree(i);
  //   //   std::vector<IndexType> visited(vertex_num, 0);
  //   //   visited[tree.sourceDieId()] = 1;
  //   //   for (auto target_die_id : tree.targetsDieId()) {
  //   //     if (!visited[target_die_id]) {
  //   //       visited[target_die_id] = 1;
  //   //       net_criticality[i] += optim_dist[tree.sourceDieId()][target_die_id];
  //   //     }
  //   //   }
  //   // }

  //   // auto netCriticalityCmp = [&](IndexType const &x, IndexType const &y) {
  //   //   if (net_criticality[x] == net_criticality[y]) {
  //   //     return trees.gettree(x).targetsDieId().size() < trees.gettree(y).targetsDieId().size();
  //   //   }
  //   //   return net_criticality[x] > net_criticality[y];
  //   // };

  //   // sort(net_ids.begin(), net_ids.end(), netCriticalityCmp);
  //   for (IndexType iter = 0; iter < 1; iter++) {
  //     std::cout << "第 " << iter << " 次迭代" << std::endl;
  //     size_t sz = database.dies().size();
  //     for (IndexType i = 0; i < sz; i++) {
  //       for (IndexType j = 0; j < sz; j++) {
  //         if (database.networks()[i][j] != 0) {
  //           auto &edge = routegraph_.edge(i, j);
  //           edge.InitSharenum();
  //           CostType cn;
  //           if (edge.Type() == 0) {
  //             cn = 4.5;
  //           } else {
  //             cn = 1;
  //           }
  //           edge.setCongestPunish(cn);
  //           // std::cout << edge.fromNodeId() << " to " << edge.toNodeId() << " : " << edge.shareNum() <<  " " <<
  //           // edge.congestPunish() << std::endl;
  //         }
  //       }
  //     }
  //     state = 1;   // squential-routing
  //     for (IndexType i = 0; i < netlist_size; i++) {
  //       auto      &net  = database.net(net_ids[i]);
  //       RouteTree &tree = trees.trees()[net_ids[i]];
  //       // RouteTree tree;

  //       tree.Init(database, routegraph_, net);
  //       ds.dijkstra_router(database, routegraph_, tree, state);
  //       tree.TrackBack();
  //       adjustWeights(database, tree, routegraph_);
  //       // 检查congestionPunish是否成功修改
  //       // std::cout << tree.netId() << std::endl;
  //       // for (IndexType i = 0; i < sz; i++) {
  //       //   for (IndexType j = 0; j < sz; j++) {
  //       //     if (database.networks()[i][j] != 0) {
  //       //       auto &edge = routegraph_.edge(i, j);
  //       //       if(edge.Type() == 0 )
  //       //       std::cout << edge.fromNodeId() << " to " << edge.toNodeId() << " : " << edge.shareNum() << " "
  //       //                 << edge.congestPunish() << std::endl;
  //       //     }
  //       //   }
  //       // }
  //       //     // std::cout << std::endl;
  //       //     // std::cout << i << " ";
  //     }

  //     for (auto &tree : trees.trees()) {
  //       tree.calculateDelay(database, routegraph_);
  //       // 计算延迟
  //     }
  //     std::sort(net_ids.begin(), net_ids.end(), cmp);
  //     // 检查是否成功将经过各edge最长的路径修改成功
  //     // size_t size_edge = database.dies().size();
  //     // for (auto &tree : trees.trees()) {
  //     //   std::cout << "[" << tree.netId() << "]" << std::endl;
  //     //   for (IndexType i = 0; i < size_edge; i++) {
  //     //     for (IndexType j = 0; j < size_edge; j++) {
  //     //       if(tree.thoughEdge(i,j) == true){
  //     //       std::cout << " 当前边为 " << i << " " << j << std::endl;
  //     //       std::cout << " 经过其的最长路径为 " << tree.thoughEdgePathId(i,j) << std::endl;
  //     //       std::cout << " 对应的延迟为 " << tree.delay(tree.thoughEdgePathId(i,j)) << std::endl;
  //     //       }
  //     //     }
  //     //   }
  //     // }
  //     // 检查边的share_num变化
  //     // for (IndexType i = 0; i < sz; i++) {
  //     //   for (IndexType j = 0; j < sz; j++) {
  //     //     if (database.networks()[i][j] != 0) {
  //     //       auto &edge = routegraph_.edge(i, j);
  //     //       if(edge.Type() == 0){
  //     //         std::cout << " TDM " ;
  //     //       }
  //     //       else{
  //     //         std::cout << " SLL " ;
  //     //       }
  //     //       std::cout << edge.fromNodeId() << " to " << edge.toNodeId() << " : " << edge.shareNum() << " "
  //     //                 << edge.congestPunish() << std::endl;
  //     //     }
  //     //   }
  //     // }
  //     //  检验tree的parent是否成功修改
  //     //  for (auto &tree : trees.trees()) {
  //     //    size_t sz = tree.parent().size();
  //     //    for (IndexType i = 0; i < sz; i++) {
  //     //      std::cout << tree.parent()[i] << " ";
  //     //    }
  //     //    std::cout << std::endl;
  //     //  }

  //     // 检验路径是否成功搜索
  //     // for (auto &tree : trees.trees()) {
  //     //   size_t paths_size = tree.paths().size();
  //     //   for (IndexType i = 0; i < paths_size; i++) {
  //     //     size_t path_size = tree.paths()[i].size();
  //     //     for (IndexType j = 0; j < path_size; j++) {
  //     //       std::cout << tree.paths()[i][j] << " ";
  //     //     }
  //     //     std::cout << std::endl;
  //     //   }
  //     // }

  //     // 检查squ-routing排序结果
  //     // for (IndexType i = 0; i < netlist_size; i++) {
  //     //   std::cout << database.net(net_ids[i]).lineNum() << " ";
  //     // }
  //     std::cout << " 计算延迟 " << std::endl;
  //     CostType max_delay = 0;
  //     for (auto &tree : trees.trees()) {
  //       tree.calculateDelay(database, routegraph_);
  //       if (tree.maxDelay() > max_delay) {
  //         max_delay = tree.maxDelay();
  //       }
  //     }
  //     std::cout << " 当前最大延迟为 " << max_delay << std::endl;
  //     std::sort(net_ids.begin(), net_ids.end(), cmp);
  //   }
  // }
  CostType total_occ_div_cap = 0;
  for (IndexType i = 0; i < vertex_num; i++)
    for (IndexType j = 0; j < i; j++)
      if (routegraph_.edge(i, j).Type() == TDM_EDGE) {
        std::cout << "TDM Edge " << i << " " << j
                  << " usage: " << routegraph_.edge(i, j).shareNum() + routegraph_.edge(j, i).shareNum()
                  << " cap: " << database.network(i, j) << std::endl;
        total_occ_div_cap += 1.0 * (routegraph_.edge(i, j).shareNum() + routegraph_.edge(j, i).shareNum()) / database.network(i, j);
      }
  std::cout << "TDM_occ_div_cap: " << total_occ_div_cap << std::endl;
  // // 检查修改过后的结果对于小case是否效果变差
  // FILE *out_file_1 = fopen("unoptimized.log", "w");
  // for (IndexType i = 0; i < net_ids.size(); i++) {
  //   auto &tree = trees.trees()[net_ids[i]];
  //   fprintf(out_file_1, "[%d]\n", tree.netId());
  //   size_t sz1 = tree.paths().size();
  //   for (IndexType j = 0; j < sz1; j++) {
  //     fprintf(out_file_1, "[");
  //     size_t sz2 = tree.paths()[j].size();
  //     for (IndexType m = 0; m < sz2; m++) {
  //       fprintf(out_file_1, "%d%s", tree.paths()[j][m], m == sz2 - 1 ? "]" : " ");
  //     }
  //     fprintf(out_file_1, "[%.1lf]\n", tree.delay(j));
  //   }
  //   for (auto &node : tree.parent()) {
  //     fprintf(out_file_1, "%d ", node);
  //   }
  //   fprintf(out_file_1, "\n");
  // }
  // fclose(out_file_1);

  // std::cout << " 开始优化 " << std::endl;
  // 加入迭代
  // {
  //   for (IndexType iter = 0; iter < 1; iter++) {
  //     std::cout << " 第 " << iter << " 次迭代 " << std::endl;
  //     // 将每个tree的changed标志清空
  //     optimize(database, trees, routegraph_, net_ids);
  //     // std::cout << " 交换完毕对结果进行重新获取和统计 " << std::endl;
  //     //  交换完毕对结果进行重新获取和统计
  //     // std::cout << " 清空share_num" << std::endl;
  //     size_t sz = database.dies().size();
  //     for (IndexType i = 0; i < sz; i++) {
  //       for (IndexType j = 0; j < sz; j++) {
  //         if (database.networks()[i][j] != 0) {
  //           auto &edge = routegraph_.edge(i, j);
  //           edge.InitSharenum();
  //           // std::cout << edge.fromNodeId() << " to " << edge.toNodeId() << " : " << edge.shareNum() <<  " " <<
  //           // edge.congestPunish() << std::endl;
  //         }
  //       }
  //     }
  //     //   // 检查更改后的父节点
  //     //   // FILE *out_file_2 = fopen("optimized_parent.log", "w");
  //     //   // for (IndexType i = 0; i < net_ids.size(); i++) {
  //     //   //   auto &tree = trees.trees()[net_ids[i]];
  //     //   //   fprintf(out_file_2, "[%d]\n", tree.netId());
  //     //   //   for (auto &node : tree.parent()) {
  //     //   //     fprintf(out_file_2, "%d ", node);
  //     //   //   }
  //     //   //   fprintf(out_file_2, "\n");
  //     //   // }
  //     //   // fclose(out_file_2);
  //     //   //   exit(0);
  //     //   std::cout << " 重新统计路径 " << std::endl;
  //     for (auto &tree : trees.trees()) {
  //       // std::cout << " trackback 错误 " << std::endl;
  //       // if(iter == 1)
  //       // std::cout << " 对 " << tree.netId() << " 进行路径计算" << std::endl;
  //       tree.Init(database, routegraph_, database.net(tree.id()));
  //       // if (tree.netId() != 16920) {
  //       tree.TrackBack();
  //       // if (tree.netId() == 16920) {
  //       //   for (IndexType i = 0; i < 12; i++) {
  //       //     for (IndexType j = 0; j < 12; j++) {
  //       //       std::cout << tree.thoughEdge(i,j)<< " ";
  //       //     }
  //       //     std::cout << std::endl;
  //       //   }
  //       // }
  //       adjustWeights(database, tree, routegraph_);
  //       //}
  //     }

  //     //   std::cout << " 计算延迟 " << std::endl;
  //     // CostType max_delay = 0;
  //     for (auto &tree : trees.trees()) {
  //       tree.calculateDelay(database, routegraph_);
  //       if (tree.maxDelay() > max_delay) {
  //         max_delay = tree.maxDelay();
  //       }
  //     }
  //     std::cout << " 当前最大延迟为 " << max_delay << std::endl;
  //     std::sort(net_ids.begin(), net_ids.end(), cmp);
  //     // 检查优化后的结果
  //   }
  // }
  TDMAssigner assigner;
  assigner.tdmAssign(database, routegraph_, trees);
  std::chrono::high_resolution_clock::time_point wr_s, wr_t;
  wr_s = std::chrono::high_resolution_clock::now();
  assigner.outputFile(database, routegraph_, trees);
  wr_t = std::chrono::high_resolution_clock::now();
  std::chrono::duration<double, std::ratio<1, 1>> d_load(wr_t - wr_s);
  std::cout << "Write Output: " << d_load.count() << "s" << std::endl;
  // 检查边的share_num变化
  // for (IndexType i = 0; i < sz; i++) {
  //   for (IndexType j = 0; j < sz; j++) {
  //     if (database.networks()[i][j] != 0) {
  //       auto &edge = routegraph_.edge(i, j);
  //       if (edge.Type() == 0)
  //         std::cout << edge.fromNodeId() << " to " << edge.toNodeId() << " : " << edge.shareNum() << " "
  //                   << edge.congestPunish() << std::endl;
  //     }
  //   }
  // }
  // std::cout << "start output" << std::endl;
  // FILE *out_file_3 = fopen("optimized_0.log", "w");
  // for (IndexType i = 0; i < net_ids.size(); i++) {
  //   auto &tree = trees.trees()[net_ids[i]];
  //   fprintf(out_file_3, "[%d]\n", tree.netId());
  //   size_t sz1 = tree.paths().size();
  //   for (IndexType j = 0; j < sz1; j++) {
  //     fprintf(out_file_3, "[");
  //     size_t sz2 = tree.paths()[j].size();
  //     for (IndexType m = 0; m < sz2; m++) {
  //       fprintf(out_file_3, "%d%s", tree.paths()[j][m], m == sz2 - 1 ? "]" : " ");
  //     }
  //     fprintf(out_file_3, "[%.1lf]\n", tree.delay(j));
  //   }
  //   for (auto &node : tree.parent()) {
  //     fprintf(out_file_3, "%d ", node);
  //   }
  //   fprintf(out_file_3, "\n");
  // }
  // fclose(out_file_3);
  // std::cout << "output finish" << std::endl;
}
void Router::buildRouteGraph(database::Database &database) { routegraph_.build(database); }
void Router::adjustWeights(database::Database &database, RouteTree &tree, RouteGraph &graph) {
  size_t sz = database.dies().size();

  for (IndexType i = 0; i < sz; i++) {
    for (IndexType j = 0; j < sz; j++) {
      if (database.network(i, j) != 0 && tree.thoughEdge(i, j) == true) {
        graph.edge(i, j).addShareNum();
        if (graph.edge(i, j).Type() == 1) {
          CostType cn = 1;
          graph.edge(i, j).setCongestPunish(cn);
        } else {
          CostType cn = 4 + 0.5 * (1 + 2 * ((graph.edge(i, j).shareNum() + graph.edge(j, i).shareNum()) /
                                            database.network(i, j)));
          graph.edge(i, j).setCongestPunish(cn);
        }
      }
    }
  }
}

void Router::optimize(database::Database     &database,
                      RouteTrees             &trees,
                      RouteGraph             &graph,
                      std::vector<IndexType> &net_id) {
  // 设定两个矩阵，对tree进行分类group
  std::vector<std::vector<IndexType>> edge_to_group_id;
  std::vector<std::vector<IndexType>> group;

  size_t                              die_size = database.dies().size();
  edge_to_group_id.resize(die_size);
  for (IndexType i = 0; i < die_size; i++) {
    edge_to_group_id[i].resize(die_size);
  }
  size_t group_size = die_size / 4 * 3 * 2 + 1;
  group.resize(group_size);
  // 对tree进行分类
  std::cout << "对tree进行分类" << std::endl;
  size_t    netlist_size = net_id.size();
  IndexType count        = 1;
  for (IndexType i = 0; i < netlist_size; i++) {
    auto     &tree              = trees.trees()[net_id[i]];
    IndexType max_die_pair_cost = 0;
    // 此处顺便将tree的changed标志复位
    tree.reChanged();
    size_t max_path_size = tree.maxPath().size();
    // 遍历tree的maxpath查找是否有相邻的die
    for (IndexType j = 0; j < max_path_size - 1; j++) {
      for (IndexType m = j; m < max_path_size; m++) {
        if ((tree.maxPath()[m] == tree.maxPath()[j] + 1) &&
            graph.edge(tree.maxPath()[j], tree.maxPath()[m]).Type() == 1 &&
            database.network(tree.maxPath()[j], tree.maxPath()[m]) != 0) {
          IndexType die_pair_cost = 0;
          for (IndexType k = j; k < m; k++) {
            die_pair_cost = die_pair_cost + graph.edge(tree.maxPath()[k], tree.maxPath()[k + 1]).congestPunish();
          }
          IndexType max_group = 0;
          // std::cout << count << " : " << tree.maxPath()[j] << " " << tree.maxPath()[m] << std::endl;
          if (edge_to_group_id[tree.maxPath()[j]][tree.maxPath()[m]] == 0) {
            edge_to_group_id[tree.maxPath()[j]][tree.maxPath()[m]] = count;
            group[count].push_back(tree.id());
            max_group = count;
            count     = count + 1;
          } else {
            group[edge_to_group_id[tree.maxPath()[j]][tree.maxPath()[m]]].push_back(tree.id());
            max_group = edge_to_group_id[tree.maxPath()[j]][tree.maxPath()[m]];
          }
          if (die_pair_cost > max_die_pair_cost) {
            max_die_pair_cost = die_pair_cost;
            tree.setMaxGroup(max_group);
          }
        } else if ((tree.maxPath()[m] == tree.maxPath()[j] - 1) &&
                   graph.edge(tree.maxPath()[j], tree.maxPath()[m]).Type() == 1 &&
                   database.network(tree.maxPath()[j], tree.maxPath()[m]) != 0) {
          IndexType die_pair_cost = 0;
          for (IndexType k = j; k < m; k++) {
            die_pair_cost = die_pair_cost + graph.edge(tree.maxPath()[k], tree.maxPath()[k + 1]).congestPunish();
          }
          // if(tree.netId() == 180675){
          //   std::cout << " 当前die pair为[" << tree.maxPath()[j] << " " << tree.maxPath()[m] << "]" << std::endl;
          //   std::cout << die_pair_cost << std::endl;
          // }
          IndexType max_group = 0;
          // std::cout << count << " : " << tree.maxPath()[j] << " " << tree.maxPath()[m] << std::endl;
          if (edge_to_group_id[tree.maxPath()[j]][tree.maxPath()[m]] == 0) {
            edge_to_group_id[tree.maxPath()[j]][tree.maxPath()[m]] = count;
            group[count].push_back(tree.id());
            max_group = count;
            count     = count + 1;
          } else {
            group[edge_to_group_id[tree.maxPath()[j]][tree.maxPath()[m]]].push_back(tree.id());
            max_group = edge_to_group_id[tree.maxPath()[j]][tree.maxPath()[m]];
          }
          if (die_pair_cost > max_die_pair_cost) {
            max_die_pair_cost = die_pair_cost;
            tree.setMaxGroup(max_group);
          }
        }
      }
    }
  }
  std::cout << " 创建对应关系 " << std::endl;
  std::vector<std::pair<IndexType, IndexType>> myMap(group.size() + 1);
  // 创建对应关系
  for (IndexType i = 0; i < die_size; i++) {
    for (IndexType j = 0; j < die_size; j++) {
      if (edge_to_group_id[i][j] != 0) {
        myMap[edge_to_group_id[i][j]] = {i, j};
      }
    }
  }
  // 在每个group中增加非maxpath的net
  for (IndexType i = 1; i < group.size(); i++) {
    auto it = myMap[i];
    for (auto &tree : trees.trees()) {
      if (tree.thoughEdge(it.first, it.second) && tree.thoughEdgePathId(it.first, it.second) != tree.maxPathId()) {
        group[i].push_back(tree.id());
      }
    }
  }

  // 打印检查是否存储了关键路径经过的最大die-pair
  // for (auto &tree : trees.trees()) {
  //   std::cout << tree.netId() << " 关键路径中经过最大的Die-Pair为[";
  //   std::cout << myMap.find(tree.MaxDiePair())->second.first << " " << myMap.find(tree.MaxDiePair())->second.second
  //             << "]" << std::endl;
  // }
  // exit(0);
  // 检查group分类是否正确
  // for (IndexType i = 1; i < group.size(); i++) {
  //   if (group[i].size() != 0) {
  //     std::cout << " 当前对应边为[" << myMap.find(i)->second.first << " " << myMap.find(i)->second.second << "]"
  //               << std::endl;
  //     for (IndexType j = 0; j < group[i].size(); j++) {
  //       std::cout << trees.trees()[group[i][j]].netId() << " ";
  //     }
  //     std::cout << std::endl;
  //   }
  // }
  auto cmp = [&](IndexType x, IndexType y) {
    return trees.trees()[x].CurrentDelay() > trees.trees()[y].CurrentDelay();
  };
  std::cout << " 对每一个group进行处理 " << std::endl;
  // 对每一个group进行处理
  // 对程序进行测试
  for (IndexType i = 1; i < group.size(); i++) {
    // std::cout << " 处理group[" << i << "]" << std::endl;
    for (IndexType j = 0; j < group[i].size(); j++) {
      auto     &tree = trees.trees()[group[i][j]];
      IndexType pathId;
      CostType  c;
      if (tree.thoughEdge(myMap[i].first, myMap[i].second)) {
        pathId = tree.thoughEdgePathId(myMap[i].first, myMap[i].second);
        if (pathId == tree.maxPathId()) {
          c = 0;
        }
        c = tree.delay(pathId);
      } else {
        c = tree.maxDelay();
      }
      // if (tree.netId() == 536455) {
      //   std::cout << "pathId:" <<pathId << std::endl;
      //   std::cout << "c:" << c << std::endl;
      // }
      tree.setCurrentDelay(c);
    }
    std::sort(group[i].begin(), group[i].end(), cmp);
    // 检查当前排序是否正确
    // if (myMap.find(i)->second.first == 7 && myMap.find(i)->second.second == 6) {
    //   for (IndexType j = 0; j < group[i].size(); j++) {
    //      std::cout << trees.trees()[group[i][j]].netId() << ":";
    //       std::cout << trees.trees()[group[i][j]].CurrentDelay() << " ";

    //   }
    // }
    // std::cout << " 当前对应边为[" << myMap.find(i)->second.first << " " << myMap.find(i)->second.second << "]"
    //          << std::endl;
    // for (IndexType j = 0; j < group[i].size(); j++) {
    //   std::cout << trees.trees()[group[i][j]].netId() << ":";
    //   std::cout << trees.trees()[group[i][j]].CurrentDelay() << " ";
    // }
    // std::cout << std::endl;
    size_t    sz = group[i].size();
    IndexType n  = sz - 1;
    for (IndexType j = 0; j < n; j++) {
      // for (IndexType j = 0; j < 1; j++) {
      //  std::cout << " 第 " << j << " 个树" << std::endl;
      auto &tree = trees.trees()[group[i][j]];
      // if (tree.netId() == 180675) {
      //   std::cout << " 对net 180675 进行处理 " << std::endl;
      //   std::cout << " 此时 180675 的 tree.MaxDiePair() = " << tree.MaxDiePair() << std::endl;
      //   std::cout << " 此时 group 的 ID = " << i << std::endl;
      //   std::cout << tree.Changed() << std::endl;
      //   std::cout << myMap.find(i)->second.first << " " << myMap.find(i)->second.second << std::endl;
      // }
      // std::cout << tree.netId() << std::endl;
      //  对这些树进行处理，初始化
      // tree.Init(database, graph, database.net(tree.id()));
      // 开始交换路径
      if (tree.Changed() == false && tree.thoughEdge(myMap[i].first, myMap[i].second) == false &&
          tree.MaxDiePair() == i) {
        // tree.Init(database, graph, database.net(tree.id()));
        //  获取并保存需要更换的路径
        std::vector<IndexType> parent_res;
        parent_res.reserve(3);
        size_t    sz1 = tree.maxPath().size();
        IndexType begin;
        IndexType end;
        auto      it = myMap[i];
        // if (tree.netId() == 180675) {
        //   std::cout << " 进入对180675的替换 " << std::endl;
        //   std::cout << " 当前变换边为 " << it->second.first << " " << it->second.second << std::endl;
        // }
        // 检查maxpath
        //  for (IndexType j = 0; j < sz1; j++) {
        //    std::cout << tree.maxPath()[j] << " ";
        //  }
        //  std::cout << std::endl;
        //  找到开始边和结束边
        for (IndexType j = 0; j < sz1; j++) {
          // auto it = myMap.find(i);
          if (tree.maxPath()[j] == it.first) {
            // parent_res.push_back(tree.maxPath()[j]);
            begin = j;
          }
          if (tree.maxPath()[j] == it.second) {
            end = j;
          }
        }
        // std::cout << begin << " " << end << std::endl;
        // if (end - begin == 1) {
        //   tree.setChanged();
        //   // trees.trees()[group[i][sz - 1 - j]].setChanged();

        //   // std::cout << " 找到开始边和结束边为： " << tree.maxPath()[begin] << " to " << tree.maxPath()[end];
        // } else {
        if (end - begin != 1) {
          for (IndexType j = begin; j <= end; j++) {
            // std::cout << " 找到开始边和结束边为： " << tree.maxPath()[begin] << " to " << tree.maxPath()[end] <<
            // std::endl; std::cout << tree.maxPath()[j] << " ";
            parent_res.push_back(tree.maxPath()[j]);
          }
          // std::cout << std::endl;
          //}

          // 对关键路径进行替换
          // if (tree.Changed() == false && end - begin != 1) {
          // 寻找一个最小延迟树
          IndexType replace_id;
          bool      replace_exist = false;
          for (n; n >= 0; n--) {
            auto &rtree = trees.trees()[group[i][n]];
            if (rtree.Changed() == false && rtree.maxDelay() < tree.maxDelay() &&
                rtree.thoughEdge(it.first, it.second) && rtree.CurrentDelay() + 100 < tree.maxDelay()) {
              replace_id    = group[i][n];
              // trees.trees()[group[i][n]].setChanged();
              replace_exist = true;
              break;
            }
          }
          // if (tree.netId() == 536455 && replace_exist) {
          //   std::cout << trees.trees()[replace_id].netId() << std::endl;
          // } else if (tree.netId() == 536455 && !replace_exist) {
          //   std::cout << " 搜索不到对应的小延迟路径 " << std::endl;
          // }
          // 保存这个最小延迟树需要更换的路径
          size_t                 p = parent_res.size() - 1;
          std::vector<IndexType> parent_res_swap;
          parent_res_swap.push_back(it.first);
          parent_res_swap.push_back(it.second);
          // parent_res_swap.reserve(3);
          // size_t    sz2 = trees.trees()[replace_id].maxPath().size();
          // IndexType begin_swap;
          // IndexType end_swap;
          // for (IndexType j = 0; j < sz2; j++) {
          //   // auto it = myMap.find(i);
          //   if (trees.trees()[replace_id].maxPath()[j] == it->second.first) {
          //     // parent_res.push_back(tree.maxPath()[j]);
          //     begin_swap = j;
          //   }
          //   if (trees.trees()[replace_id].maxPath()[j] == it->second.second) {
          //     end_swap = j;
          //   }
          // }
          // for (IndexType j = begin_swap; j <= end_swap; j++) {
          //   // std::cout << " 找到开始边和结束边为： " << tree.maxPath()[begin] << " to " << tree.maxPath()[end] <<
          //   // std::endl; std::cout << tree.maxPath()[j] << " ";
          //   parent_res_swap.push_back(trees.trees()[replace_id].maxPath()[j]);
          // }
          auto &replace_tree = trees.trees()[replace_id];
          if (tree.parent()[parent_res[p]] != replace_tree.parent()[parent_res[p]] && replace_exist == true &&
              parent_res_swap.size() == 2) {
            // if (trees.trees()[replace_id].netId() == 627147) {
            //   std::cout << " 627147 被作为小延迟交换路径 " << std::endl;
            //   std::cout << " 当前变换边为 " << it->second.first << " " << it->second.second << std::endl;
            // }
            // 检查parent_res_swap是否成功被记录
            // 更改大延迟的父节点
            for (IndexType m = parent_res_swap.size() - 1; m > 0; m--) {
              // if(tree.netId() == 327281)
              // std::cout << parent_res_swap[m] << " " ;
              // 确定parent_res_swap[m]在大延迟树路径的位置
              IndexType med_site  = 10000;
              IndexType count_med = 0;
              for (auto &f : tree.maxPath()) {
                if (f == parent_res_swap[m]) {
                  med_site = count_med;
                } else {
                  count_med++;
                }
              }
              if (med_site >= begin) {
                tree.changeParent(parent_res_swap[m], parent_res_swap[m - 1]);
              }
            }
            // if(tree.netId() == 327281)
            // std::cout << std::endl;
            // 更改小延迟的父节点
            // if (trees.trees()[replace_id].netId() == 278237) {
            //   std::cout << " 此时交换边为[" << it->second.first << " " << it->second.second << "]" << std::endl;
            // }
            for (IndexType m = p; m > 0; m--) {
              // std::cout << parent_res[m] << " ";
              // 获取小延迟树的路径
              IndexType path_id = replace_tree.thoughEdgePathId(it.first, it.second);
              // 确定parent_res[m]在最小延迟树路径的位置
              IndexType begin_site;
              IndexType med_site    = 10000;
              IndexType count_med   = 0;
              IndexType count_begin = 0;
              for (auto &f : replace_tree.paths()[path_id]) {
                if (f == parent_res[m]) {
                  med_site = count_med;
                } else {
                  count_med = count_med + 1;
                }

                if (f == it.first) {
                  begin_site = count_begin;
                } else {
                  count_begin = count_begin + 1;
                }
              }

              if (parent_res[m] != replace_tree.sourceDieId() && med_site > begin_site) {
                replace_tree.changeParent(parent_res[m], parent_res[m - 1]);
              }
              // if (trees.trees()[replace_id].netId() == 278237) {
              //   std::cout << "正在交换 " << parent_res[m] << " 的父节点" << std::endl;
              //   std::cout << "parent_res[m]的位置为： path[" << path_id << "]的第" << med_site <<"位" << std::endl;
              //   std::cout << "左边begin的位置为: path[" << path_id << "]的第" << begin_site <<"位" << std::endl;
              // }
            }
            tree.setChanged();
            replace_tree.setChanged();
            // if (trees.trees()[replace_id].netId() == 411345) {
            //   if (trees.trees()[replace_id].Changed() == true) {
            //     std::cout << " 411345 作为小延迟路径交换成功 " << std::endl;
            //   } else {
            //     std::cout << " 411345 作为小延迟路径交换失败 " << std::endl;
            //   }
            // }
            // if (tree.netId() == 16920) {
            //   for (IndexType i = 0; i < 12; i++) {
            //     for (IndexType j = 0; j < 12; j++) {
            //       std::cout << tree.thoughEdge(i,j) << " ";
            //     }
            //     std::cout << std::endl;
            //   }
            //   std::cout << std::endl;
            // }
          }
        }
        // std::cout << std::endl;
      }
      // if (tree.netId() == 536455) {
      //   if (tree.Changed() == true) {
      //     std::cout << " 536455 作为大延迟路径交换成功 " << std::endl;
      //   } else {
      //     std::cout << " 536455 作为大延迟路径交换失败 " << std::endl;
      //   }
      // }
    }
  }
}


// 检查分类是否正确
// for (IndexType i = 0; i < die_size; i++) {
//   for (IndexType j = 0; j < die_size; j++) {
//     if (edge_to_group_id[i][j] != 0) {
//       std::cout << i << " to " << j << " : " << edge_to_group_id[i][j] << std::endl;
//     }
//   }
// }
// for (IndexType i = 0; i < 2; i++) {
//   if (!group[i].empty()) {
//     std::cout << "group[" << i << "] : ";
//     // for (IndexType j = 0; j < group[i].size(); j++) {
//     //   std::cout << database.net(group[i][j]).lineNum() << " ";
//     // }
//     std::cout << std::endl;
//   }
// }
}   // namespace router
    // namespace router
/*
namespace router {
void Router::run(database::Database &database) {
  auto &params = database.params();
  buildRouteGraph(database);
  Dijkstra   ds;
  tdmassign  as;
  RouteTrees trees;
  CostType   critical_delay = 0;
  CostType   a              = 0;
  trees.trees().resize(database.netlist().size());
  std::vector<database::Net> &netlist      = database.netlist();
  IndexType                   netlist_size = netlist.size();
  std::vector<IndexType>      net_ids(netlist_size);
  std::ofstream               iterfile("iter.result");
  FILE                       *out_file = fopen("iter.result", "w");
  for (IndexType i = 0; i < netlist_size; i++) net_ids[i] = i;
  for (int32_t iter = 0; iter < params.max_iteration; iter++) {
    // for (IndexType x = 0; x < database.networks().size(); x++) {
    //   for (IndexType y = 0; y < database.networks()[x].size(); y++) {
    //     if (database.network(x, y) != 0) {
    //       routegraph_.edge(x, y).setNum();
    //       if (routegraph_.edge(x, y).Type() == 0) {
    //         std::cout << " TDM ";
    //       } else {
    //         std::cout << " SLL ";
    //       }
    //       std::cout << " from die: " << routegraph_.edge(x, y).fromNodeId()
    //                 << " to die: " << routegraph_.edge(x, y).toNodeId()
    //                 << " share num: " << routegraph_.edge(x, y).share_num()
    //                 << " cn: " << routegraph_.edge(x, y).congestPunish() << " total num "
    //                 << routegraph_.edge(x, y).share_num() + routegraph_.edge(y, x).share_num() << std::endl;
    //     }
    //   }
    // }
    for (IndexType i = 0; i < netlist_size; i++) {
      // auto &net = database.net(i);
      auto &net = database.net(net_ids[i]);
      // std::cout << net.id() << " " ;
      if (net.needsRerouting()) {
        RouteTree tree;
        tree.Init(database, routegraph_, net);
        tree.setId(net.id());
        tree.setNetId(net.lineNum());
        IndexType              source_die_id = database.node(net.drivenNodeId()).DieId();
        std::vector<IndexType> target_die_id;
        target_die_id.resize(net.loadenNodeId().size());
        size_t size_loaden = net.loadenNodeId().size();
        for (IndexType j = 0; j < size_loaden; j++) {
          target_die_id[j] = database.node(net.loadenNodeId()[j]).DieId();
        }
        tree.setRootId(source_die_id);
        // 评估当前net的关键性并且根据此调整graph
        CostType Aij;
        if (iter != 0 && iter != 1) {
          Aij = (critical_delay == 0 ? 0 : net.netMaxDelay() / critical_delay);
        } else {
          Aij = 1;
        }
        // Aij = 0;
        ds.dijkstra_router(source_die_id, target_die_id, database, routegraph_, Aij, tree, iter);
        trees.setRouteTrees(net.id(), tree);
      }
    }
    // resultStream.str("");
    fprintf(out_file, "第%d次迭代\n", iter);
    for (IndexType x = 0; x < database.networks().size(); x++) {
      for (IndexType y = 0; y < database.networks()[x].size(); y++) {
        if (database.network(x, y) != 0) {
          routegraph_.edge(x, y).setNum();
          fprintf(out_file,
                  "%s from die:%d to die:%d share num:%d cn:%1.f total num:%d %s\n",
                  routegraph_.edge(x, y).Type() ? "SLL" : "TDM",
                  routegraph_.edge(x, y).fromNodeId(),
                  routegraph_.edge(x, y).toNodeId(),
                  routegraph_.edge(x, y).share_num(),
                  routegraph_.edge(x, y).congestPunish(),
                  routegraph_.edge(x, y).share_num() + routegraph_.edge(y, x).share_num(),
                  (routegraph_.edge(x, y).share_num() + routegraph_.edge(y, x).share_num() > database.network(x, y) &&
                   routegraph_.edge(x, y).Type())
                          ? "违例"
                          : " ");
          // if (routegraph_.edge(x, y).Type() == 0) {
          //   std::cout << " TDM ";
          // } else {
          //   std::cout << " SLL ";
          // }
          // std::cout << " from die: " << routegraph_.edge(x, y).fromNodeId()
          //           << " to die: " << routegraph_.edge(x, y).toNodeId()
          //           << " share num: " << routegraph_.edge(x, y).share_num()
          //           << " cn: " << routegraph_.edge(x, y).congestPunish() << " total num "
          //           << routegraph_.edge(x, y).share_num() + routegraph_.edge(y, x).share_num() << std::endl;
          if (routegraph_.edge(x, y).share_num() + routegraph_.edge(y, x).share_num() > database.network(x, y) &&
              routegraph_.edge(x, y).Type() && iter != 0) {
            IndexType totalNum = routegraph_.edge(y, x).share_num() + routegraph_.edge(x, y).share_num();
            CostType  cn       = (totalNum / database.network(x, y) *
                                   static_cast<CostType>(routegraph_.edge(x, y).share_num()) / totalNum +
                           routegraph_.edge(x, y).History() / 1000) *
                          (2 * iter + 1);
            routegraph_.edge(x, y).setCongestPunish(cn);
            routegraph_.edge(x, y).addHistory();
            // resultStream << " 违例 " << std::endl;
          } else if (routegraph_.edge(x, y).share_num() + routegraph_.edge(y, x).share_num() < database.network(x, y) &&
                     routegraph_.edge(x, y).Type() && iter != 0) {
            IndexType totalNum = routegraph_.edge(y, x).share_num() + routegraph_.edge(x, y).share_num();
            CostType  cn       = (totalNum / database.network(x, y) *
                                   static_cast<CostType>(routegraph_.edge(x, y).share_num()) / totalNum +
                           routegraph_.edge(x, y).History() / 1000);
            routegraph_.edge(x, y).setCongestPunish(cn);
          } else {
            if (routegraph_.edge(x, y).Type() == 0) {
              CostType cn = 4.5;
              routegraph_.edge(x, y).setCongestPunish(cn);
            } else {
              CostType cn = 1;
              routegraph_.edge(x, y).setCongestPunish(cn);
            }
          }
        }
      }
    }
    // resultContent = resultStream.str();
    // Router::writeTxTResult("iter.design.route.out", resultContent);
    bool success   = true;
    critical_delay = 0;
    for (auto &result_tree : trees.trees()) {
      bool congestion = true;
      // 判断result_tree中是否有路径全为TDM wire 或者 设定一个最大的max_delay值，如果路径delay大于该值，重新布线。
      // for (auto &edge : result_tree.tree_edge()) {
      //   if (iter == 0) {
      //     congestion = false;
      //     success    = false;
      //   } else if (routegraph_.edge(edge.fromNodeId(), edge.toNodeId()).share_num() +
      //                              routegraph_.edge(edge.toNodeId(), edge.fromNodeId()).share_num() >
      //                      database.network(edge.toNodeId(), edge.fromNodeId()) &&
      //              edge.Type()) {
      //     congestion = false;
      //     success    = false;
      //   }
      // }
      // if (congestion == false) {
      //   for (auto &edge : result_tree.tree_edge()) {
      //     routegraph_.edge(edge.fromNodeId(), edge.toNodeId()).subShareNum();
      //   }
      //   database.net(result_tree.id()).setneedsRerouting(true);
      // } else {
      //   database.net(result_tree.id()).setneedsRerouting(false);
      // }
      result_tree.setMaxDelay(0);
      // IndexType tdm_num_max = 0;
      size_t size_path_1 = result_tree.path().size();
      for (IndexType i = 0; i < size_path_1; i++) {
        // IndexType tdm_num = 0;
        CostType  delay       = 0;
        size_t    size_path_2 = result_tree.path()[i].size();
        IndexType d           = 0;

        for (IndexType j = 0; j < size_path_2 - 1; j++) {
          if (routegraph_.edge(result_tree.path()[i][j], result_tree.path()[i][j + 1]).Type() == 1) {
            delay++;
          } else {
            d = 1 + 2 *
                            (routegraph_.edge(result_tree.path()[i][j], result_tree.path()[i][j + 1]).share_num() +
                             routegraph_.edge(result_tree.path()[i][j + 1], result_tree.path()[i][j]).share_num()) /
                            database.network(result_tree.path()[i][j], result_tree.path()[i][j + 1]);
            delay      = delay + 4 + 0.5 * d;
            CostType a = 4 + 0.5 * d;
            routegraph_.edge(result_tree.path()[i][j], result_tree.path()[i][j + 1]).setRDealy(a);
          }
        }
        for (IndexType j = 0; j < size_path_2 - 1; j++) {
          if (delay >
              result_tree.edgeDelay()[routegraph_.edge(result_tree.path()[i][j], result_tree.path()[i][j + 1]).id()]) {
            // std::cout << routegraph_.edge(result_tree.path()[i][j], result_tree.path()[i][j + 1]).id() << " " <<
            // delay << std::endl;
            result_tree.setEdgeDelay(routegraph_.edge(result_tree.path()[i][j], result_tree.path()[i][j + 1]).id(),
                                     delay);
            result_tree.setEdgePath(routegraph_.edge(result_tree.path()[i][j], result_tree.path()[i][j + 1]).id(), i);
          }
        }

        result_tree.setPathDelay(i, delay);

        if (delay > result_tree.maxDelay()) {
          result_tree.setMaxDelay(delay);
          database.net(result_tree.id()).setMaxDelay(delay);
          result_tree.setMaxPath(result_tree.path()[i]);
        }
        if (delay > critical_delay) {
          critical_delay = delay;
        }
      }
      for (auto &edge : result_tree.tree_edge()) {
        if (iter == 0) {
          congestion = false;
          success    = false;
        } else if (routegraph_.edge(edge.fromNodeId(), edge.toNodeId()).share_num() +
                                   routegraph_.edge(edge.toNodeId(), edge.fromNodeId()).share_num() >
                           database.network(edge.toNodeId(), edge.fromNodeId()) &&
                   edge.Type()) {
          congestion = false;
          success    = false;
        }
      }
      if (congestion == false) {
        for (auto &edge : result_tree.tree_edge()) {
          routegraph_.edge(edge.fromNodeId(), edge.toNodeId()).subShareNum();
        }
        database.net(result_tree.id()).setneedsRerouting(true);
        // std::cout << result_tree.id() << " ";
      } else {
        database.net(result_tree.id()).setneedsRerouting(false);
      }
    }
    std::cout << "第" << iter << "次迭代 "
              << "max_delay " << critical_delay << std::endl;
    // std::cout << database.nodes().size() << " ";
    // std::cout << " a = " << a << std::endl;
    // for (auto &result_tree : trees.trees()) {
    //   std::cout << result_tree.net_id() << ":" << result_tree.maxDelay() << std::endl;
    // }

    if (iter != params.max_iteration - 1 && success) {
      // if (iter != params.max_iteration - 1 && success) {
      std::cout << "布线成功" << std::endl;
      break;
    }
    // priority_queue = database.netlist();
    auto cmp = [&](IndexType x, IndexType y) {
      return database::Database::netCompareDelay(database.net(x), database.net(y));
    };
    std::sort(net_ids.begin(), net_ids.end(), cmp);
    // for(auto& id:net_ids){
    //   std::cout << id << " ";
    // }
    /// fprintf(out_file,"拆线结果\n");
    for (IndexType x = 0; x < routegraph_.edges().size(); x++) {
      for (IndexType y = 0; y < routegraph_.edges()[x].size(); y++) {
        if (database.network(x, y) != 0) {
          routegraph_.edge(x, y).setSharenum();

          if (routegraph_.edge(x, y).Type() == 0 && iter != 0) {
            CostType cn =
                    (4 + 0.5 * (1 + 2 * (routegraph_.edge(x, y).share_num() / 4 / database.network(x, y)))) * 1000;
            // CostType cn = 20000;
            routegraph_.edge(x, y).setCongestPunish(cn);
          }
          // fprintf(out_file,
          //         "%s from die:%d to die:%d share num:%d cn:%1.f \n",
          //         routegraph_.edge(x, y).Type() ? "SLL" : "TDM",
          //         routegraph_.edge(x, y).fromNodeId(),
          //         routegraph_.edge(x, y).toNodeId(),
          //         routegraph_.edge(x, y).share_num(),
          //         routegraph_.edge(x, y).congestPunish());
        }
      }
    }
  }
  std::cout << " 开始优化调整 " << std::endl;
  testMethod(database, routegraph_, trees);
  fclose(out_file);
  std::cout << " 开始TDM ratio assignment " << std::endl;
  as.tdmAssign(database, routegraph_, trees);
  CostType design_max_delay = 0;
  std::cout << " Delay 计算 " << std::endl;
  for (auto &tree : trees.trees()) {
    size_t   sz1       = tree.path().size();
    CostType max_delay = 0;
    for (IndexType i = 0; i < sz1; i++) {
      size_t   sz_2  = tree.path()[i].size();
      CostType delay = 0;
      for (IndexType j = 0; j < sz_2 - 1; j++) {
        if (routegraph_.edge(tree.path()[i][j], tree.path()[i][j + 1]).Type() == 1) {
          delay++;
        } else {
          delay = delay + 0.5 * (1 + 2 * (tree.edgeRatio()[tree.path()[i][j]][tree.path()[i][j + 1]]));
          // tdm_num = tdm_num + 1;
        }
      }
      tree.setPathDelay(i, delay);

      if (delay > max_delay) {
        max_delay = delay;
        tree.setMaxDelay(delay);
        // tree.setMaxPath(tree.path()[i]);
      }
    }
    design_max_delay = std::max(design_max_delay, max_delay);
  }
  std::cout << "Max delay: " << design_max_delay << std::endl;
  IndexType              tree_size = trees.trees().size();
  std::vector<IndexType> tree_ids(tree_size);
  for (IndexType i = 0; i < tree_size; i++) tree_ids[i] = i;
  auto tree_id_cmp = [&](IndexType x, IndexType y) {
    return RouteTrees::compareDelay(trees.trees()[x], trees.trees()[y]);
  };
  std::sort(tree_ids.begin(), tree_ids.end(), tree_id_cmp);
  // TDM ratio assignment
  //   std::stringstream resultStream;
  //   resultStream.str("");
  std::cout << " 开始输出结果文件 " << std::endl;
  // 输出路径结果
  //   std::ofstream afile("design.route.out");
  FILE *out_file_2 = fopen("design.route.out", "w");
  for (IndexType i = 0; i < tree_size; i++) {
    auto &tree = trees.trees()[tree_ids[i]];
    // afile << "[";
    // afile << tree.net_id() << "]" << std::endl;
    fprintf(out_file_2, "[%d]\n", tree.net_id());
    size_t sz1 = tree.path().size();
    for (IndexType i = 0; i < sz1; i++) {
      //   afile << "[";
      fprintf(out_file_2, "[");
      size_t sz_2 = tree.path()[i].size();
      for (IndexType j = 0; j < sz_2; j++) {
        // if (j != sz_2 - 1) {
        //   afile << tree.path()[i][j] << " ";
        // } else {
        //   afile << tree.path()[i][j];
        // }
        fprintf(out_file_2, "%d%s", tree.path()[i][j], j == sz_2 - 1 ? "]" : " ");
      }
      //   afile << "]";
      //   afile << "[";
      //   afile << tree.pathDelay()[i] << "]";
      //   afile << std::endl;
      //   int32_t delay = tree.pathDelay()[i]
      fprintf(out_file_2, "[%.1lf]\n", tree.pathDelay()[i]);
    }
  }
  fclose(out_file_2);
  //   afile.close();
  //   std::string resultContent = resultStream.str();
  //   Router::writeTxTResult("design.route.out", resultContent);

  // TDM ratio assignment
}

void Router::buildRouteGraph(database::Database &database) { routegraph_.build(database); }
void Router::writeTxTResult(std::string const &file_name, std::string const &content) {
  std::ofstream outputFile(file_name, std::ios::app);   // 以追加方式打开文件
  if (!outputFile.is_open()) {
    std::cerr << "无法打开文件 " << file_name << std::endl;
    return;
  }
  outputFile << content << std::endl;
  outputFile.close();
}

void Router::adjustWeights(database::Net &net, CostType &delay, RouteGraph &graph) {
  CostType Aij = net.netMaxDelay() / delay;
  for (IndexType x = 0; x < graph.edges().size(); x++) {
    for (IndexType y = 0; y < graph.edges()[x].size(); y++) {
      if (graph.edge(x, y).Type() == 1) {
        CostType Cn = 1000 * Aij + (1 - Aij) * graph.edge(x, y).congestPunish();
        graph.edge(x, y).setCongestPunish(Cn);
      }
    }
  }
}

void Router::testMethod(database::Database &database, RouteGraph &graph, RouteTrees &trees) {
  // 检查所有的tree中保存的最大路径中是否有4-x-x-5的组合
  // 如果有，分为一组，按照延迟进行排序
  // 先尝试在此交换
  // 后续可以考虑在优先队列里通过更改父节点的方式更改或其他更有效率的方式
  std::vector<router::RouteTree> four_to_five_tree;
  for (auto &tree : trees.trees()) {
    size_t sz1 = tree.maxPath().size();
    if (!tree.maxPath().empty() && tree.maxPath()[0] == 4) {
      for (IndexType i = 1; i < sz1; i++) {
        if (tree.maxPath()[i] == 5) {
          four_to_five_tree.emplace_back(tree);
        }
      }
    }
  }
  // 根据延迟对这些树进行排序
  auto tree_delay_cmp = [&](RouteTree x, RouteTree y) { return RouteTrees::compareDelay(x, y); };
  std::sort(four_to_five_tree.begin(), four_to_five_tree.end(), tree_delay_cmp);

  // 对这些树进行处理，先全部拆除它们的线
  for (auto &tree : four_to_five_tree) {
    for (auto &edge : tree.tree_edge()) {
      routegraph_.edge(edge.fromNodeId(), edge.toNodeId()).subShareNum();
    }
  }

  // // 检查原树的parent列表
  // FILE *out_file_3 = fopen("unchange_tree_parent.log", "w");
  // for (auto &tree : four_to_five_tree) {
  //   for (IndexType i = 0; i < tree.parent().size(); i++) {
  //     fprintf(out_file_3, "%d ", tree.parent()[i]);
  //   }
  //   fprintf(out_file_3, "\n");
  // }
  // fclose(out_file_3);
  // 交换路径
  std::vector<bool> visit;
  size_t            sz = four_to_five_tree.size();
  visit.resize(sz);
  for (IndexType i = 0; i < sz; i++) {
    if (visit[i] == 0) {
      // 获取并保存了需要更换的路径
      std::vector<IndexType> parent_res;
      size_t                 sz1 = four_to_five_tree[i].maxPath().size();
      for (IndexType j = 0; j < sz1; j++) {
        parent_res.push_back(four_to_five_tree[i].maxPath()[j]);
        if (four_to_five_tree[i].maxPath()[j] == 5) {
          break;
        }
      }
      // 对关键路径替换
      for (IndexType m = parent_res.size(); m > 0; m--) {
        four_to_five_tree[i].changeParent(parent_res[m], four_to_five_tree[sz - 1 - i].parent()[parent_res[m]]);
        four_to_five_tree[sz - 1 - i].changeParent(parent_res[m], parent_res[m - 1]);
      }

      visit[i]          = 1;
      visit[sz - 1 - i] = 1;
    }
  }
  // // 检查替换后的树的parent列表
  // FILE *out_file_4 = fopen("change_tree_parent.log", "w");
  // for (auto &tree : four_to_five_tree) {
  //   for (IndexType i = 0; i < tree.parent().size(); i++) {
  //     fprintf(out_file_4, "%d ", tree.parent()[i]);
  //   }
  //   fprintf(out_file_4, "\n");
  // }
  // fclose(out_file_4);

  for (auto &tree : four_to_five_tree) {
    auto                  &net = database.net(tree.id());
    tree.Init(database, routegraph_, net);
    std::vector<IndexType> target_die_id;
    target_die_id.resize(net.loadenNodeId().size());
    size_t size_loaden = net.loadenNodeId().size();
    for (IndexType j = 0; j < size_loaden; j++) {
      target_die_id[j] = database.node(net.loadenNodeId()[j]).DieId();
      tree.setTreeId(j, target_die_id[j]);
    }

    auto &paths = tree.path();
    paths.clear();
    paths.resize(target_die_id.size());
    size_t sz1 = tree.path().size();

    for (IndexType i = 0; i < sz1; i++) {
      IndexType currentNode = target_die_id[i];
      auto     &path        = paths[i];
      while (currentNode != -1) {
        path.push_back(currentNode);
        currentNode = tree.parent()[currentNode];
      }
      reverse(path.begin(), path.end());
      tree.setpath(i, path);

      tree.initEdge();
      for (IndexType j = 0; j < path.size() - 1; j++) {
        bool repeat = false;
        for (auto &edge : tree.tree_edge()) {
          if (edge.fromNodeId() == path[j] && edge.toNodeId() == path[j + 1]) {
            repeat = true;
          }
        }
        if (repeat == false) {
          routegraph_.edge(path[j], path[j + 1]).addShareNum();
          tree.setEdge(routegraph_.edge(path[j], path[j + 1]));
        }
      }
    }

    CostType max_delay = 0;
    for (IndexType i = 0; i < sz1; i++) {
      size_t    sz_2  = tree.path()[i].size();
      CostType  delay = 0;
      IndexType d     = 0;
      for (IndexType j = 0; j < sz_2 - 1; j++) {
        if (routegraph_.edge(tree.path()[i][j], tree.path()[i][j + 1]).Type() == 1) {
          delay++;
        } else {
          d = 1 + 2 *
                          (routegraph_.edge(tree.path()[i][j], tree.path()[i][j + 1]).share_num() +
                           routegraph_.edge(tree.path()[i][j + 1], tree.path()[i][j]).share_num()) /
                          database.network(tree.path()[i][j], tree.path()[i][j + 1]);
          delay      = delay + 4 + 0.5 * d;
          CostType a = 4 + 0.5 * d;
          routegraph_.edge(tree.path()[i][j], tree.path()[i][j + 1]).setRDealy(a);
          // tdm_num = tdm_num + 1;
        }
      }
      for (IndexType j = 0; j < sz_2 - 1; j++) {
        if (delay >
            tree.edgeDelay()[routegraph_.edge(tree.path()[i][j], tree.path()[i][j + 1]).id()]) {
          // std::cout << routegraph_.edge(result_tree.path()[i][j], result_tree.path()[i][j + 1]).id() << " " <<
          // delay << std::endl;
          tree.setEdgeDelay(routegraph_.edge(tree.path()[i][j], tree.path()[i][j + 1]).id(),
                                   delay);
          tree.setEdgePath(routegraph_.edge(tree.path()[i][j], tree.path()[i][j + 1]).id(), i);
        }
      }
      tree.setPathDelay(i, delay);

      if (delay > max_delay) {
        max_delay = delay;
        tree.setMaxDelay(delay);
        database.net(tree.id()).setMaxDelay(delay);
        tree.setMaxPath(tree.path()[i]);
      }
    }
    trees.setRouteTrees(net.id(), tree);
  }


  // 先输出结果检验分组是否成功
  FILE  *out_file_3 = fopen("test.log", "w");
  size_t sz2        = four_to_five_tree.size();
  if (sz2 == 0) {
    fprintf(out_file_3, " 无路径 ");
    // std::cout << " 无路径 " << std::endl;
  } else {
    for (IndexType j = 0; j < sz2; j++) {
      fprintf(out_file_3, "[%d]\n", four_to_five_tree[j].net_id());
      size_t sz3 = four_to_five_tree[j].maxPath().size();
      for (IndexType m = 0; m < sz3; m++) {
        // std::cout << four_to_five_tree[j].maxPath()[m] << " ";
        fprintf(out_file_3, "%d ", four_to_five_tree[j].maxPath()[m]);
      }
      // std::cout << "[" << four_to_five_tree[j].maxDelay() << "]" << std::endl;
      fprintf(out_file_3, "[%.1f]\n", four_to_five_tree[j].maxDelay());
    }
  }

  fclose(out_file_3);
}
}   // namespace router
*/