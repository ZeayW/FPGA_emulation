#include "mazerouter.h"

namespace router {
bool MazeRouter::run(MazeRoutingParams const& p,
                     database::Database&      database,
                     RouteGraph&              routegraph,
                     RouteTrees&              routetrees) {
  params                         = p;
  bool                   res     = true;
  auto&                  netlist = database.netlist();
  IndexType              n       = netlist.size();
  std::vector<IndexType> net_ids(n);
  for (IndexType i = 0; i < n; i++) {
    net_ids[i] = i;
  }
  if (params.need_resort) {
    for (auto& tree : routetrees.trees()) {
      tree.TrackBack();
      tree.calculateDelay(database, routegraph);
    }
    auto cmp = [&](IndexType x, IndexType y) {
      return database::Database::netCompareDelay(database.net(x), database.net(y));
    };
    sort(net_ids.begin(), net_ids.end(), cmp);
    // std::reverse(net_ids.begin(), net_ids.end());
  }
  if (params.need_total_reroute) {
    IndexType vertex_num = routegraph.vertexNum();
    for (auto& tree : routetrees.trees()) {
      for (IndexType i = 0; i < vertex_num; i++) {
        if (tree.parent()[i] != -1) {
          routegraph.edge(tree.parent()[i], i).subShareNum();
          tree.changeParent(i, -1);
        }
      }
    }
  }
  for (IndexType i = 0; i < n; i++) {
    auto& net = netlist[net_ids[i]];
    checkCongAndRipup(database, routegraph, routetrees.gettree(net.id()));
    res &= routeSingleNet(database, routegraph, routetrees.gettree(net.id()), net);
    // assert(res == true);
  }
  bool cong = checkCong(database, routegraph, routetrees);
  if (cong) std::cout << "result with congestion!" << std::endl;
  if (!res) std::cout << "net delay too long!" << std::endl;
  return (!res) | cong;
}

bool MazeRouter::routeSingleNet(database::Database& database,
                                RouteGraph&         routegraph,
                                RouteTree&          routetree,
                                database::Net&      net) {
  IndexType source = database.node(net.drivenNodeId()).DieId();
  auto&     sinks  = net.loadenNodeId();
  // checkCongAndRipup(database, routegraph)
  for (auto sinkNodeId : sinks) {
    IndexType sink = database.node(sinkNodeId).DieId();
    if (routetree.parent()[sink] == -1 && sink != source) {
      bool res = routeSingleSink(database, routegraph, routetree, sink);
      // getchar();
      if (!res) return false;
    }
  }
  return true;
}

bool MazeRouter::runConnectionRoute(MazeRoutingParams const& p,
                                    database::Database&      database,
                                    RouteGraph&              routegraph,
                                    RouteTrees&              routetrees) {
  IndexType vertex_num   = routegraph.vertexNum();
  IndexType netlist_size = database.netlist().size();
  params                 = p;
  if (params.need_total_reroute) {
    IndexType vertex_num = routegraph.vertexNum();
    for (auto& tree : routetrees.trees()) {
      for (IndexType i = 0; i < vertex_num; i++) {
        if (tree.parent()[i] != -1) {
          routegraph.edge(tree.parent()[i], i).subShareNum();
          tree.changeParent(i, -1);
        }
      }
    }
  }
  std::vector<std::vector<CostType>> optim_dist(vertex_num);
  for (IndexType i = 0; i < vertex_num; i++) {
    optim_dist[i].resize(vertex_num, 65536);
    optim_dist[i][i] = 0;
  }


  struct Connection {
    IndexType from, to;
    IndexType net_id;
    Connection() {}
    Connection(IndexType const& v0, IndexType const& v1, IndexType const& v2) : from(v0), to(v1), net_id(v2) {}
  };
  std::vector<Connection> connections;
  std::vector<IndexType>  net_sink_size(netlist_size, 0);

  for (IndexType i = 0; i < netlist_size; i++) {
    auto&                  tree = routetrees.gettree(i);
    std::vector<IndexType> visited(vertex_num, 0);
    visited[tree.sourceDieId()] = 1;
    for (auto target_die_id : tree.targetsDieId()) {
      if (!visited[target_die_id]) {
        visited[target_die_id] = 1;
        connections.emplace_back(tree.sourceDieId(), target_die_id, i);
        net_sink_size[i] += optim_dist[tree.sourceDieId()][target_die_id];
      }
    }
  }
  // std::cout << connections.size() << ' ' << netlist_size << ' ' << connections.size() / vertex_num << ' ' <<
  // netlist_size / vertex_num << std::endl; std::cout << connections.size() << std::endl; getchar();
  if (netlist_size / vertex_num < 4000) {
    params.sll_delay_estimate = 0.1;
    params.is_small_case      = true;
  }
  if (netlist_size / vertex_num < 50) {
    params.is_tiny_case = true;
    params.sll_delay_estimate = 0.5;
  }
  if (netlist_size / vertex_num > 10000) params.sll_delay_estimate = 9.0;

  for (IndexType i = 0; i < vertex_num; i++)
    for (IndexType j = 0; j < vertex_num; j++)
      if (netlist_size / vertex_num > 10000) {
        if (routegraph.edge(i, j).Type() == TDM_EDGE) optim_dist[i][j] = 1;
        else if (routegraph.edge(i, j).Type() == SLL_EDGE)
          optim_dist[i][j] = vertex_num + 1;
      } else {
        if (routegraph.edge(i, j).Type() == SLL_EDGE) optim_dist[i][j] = 1;
        else if (routegraph.edge(i, j).Type() == TDM_EDGE)
          optim_dist[i][j] = vertex_num + 1;
      }

  for (IndexType k = 0; k < vertex_num; k++)
    for (IndexType i = 0; i < vertex_num; i++)
      for (IndexType j = 0; j < vertex_num; j++)
        optim_dist[i][j] = std::min(optim_dist[i][j], optim_dist[i][k] + optim_dist[k][j]);
  auto connectionCmp = [&](Connection const& x, Connection const& y) {
    if (optim_dist[x.from][x.to] == optim_dist[y.from][y.to]) {
      if (net_sink_size[x.net_id] == net_sink_size[y.net_id]) return x.net_id < y.net_id;
      return net_sink_size[x.net_id] < net_sink_size[y.net_id];
    }
    if (p.use_estimate_delay) return optim_dist[x.from][x.to] < optim_dist[y.from][y.to];
    return optim_dist[x.from][x.to] > optim_dist[y.from][y.to];
  };

  sort(connections.begin(), connections.end(), connectionCmp);

  for (auto& connection : connections) {
    // std::cout << connection.from << ' ' << connection.to << ' ' << connection.net_id << std::endl;
    // getchar();
    if (routetrees.gettree(connection.net_id).parent()[connection.to] == -1)
      if (!routeSingleSink(database, routegraph, routetrees.gettree(connection.net_id), connection.to)) {
        std::cout << "route target " << connection.to << " from net " << connection.net_id << " failed!" << std::endl;
        getchar();
        return false;
      }
  }
  return true;
}

bool MazeRouter::routeSingleSink(database::Database& database,
                                 RouteGraph&         routegraph,
                                 RouteTree&          routetree,
                                 IndexType           sink) {
  struct PathNode {
    CostType  delay_cost;
    CostType  cong_cost;
    IndexType prev_node_id;
    IndexType head_vertex_id;

    PathNode() {}
    PathNode(IndexType v1, CostType v2, CostType v3, IndexType v4)
        : head_vertex_id(v1),
          delay_cost(v2),
          cong_cost(v3),
          prev_node_id(v4) {}
  };
  std::vector<PathNode> path_nodes;
  path_nodes.reserve(1000);
  auto cmp = [&](const IndexType& a, const IndexType& b) {
    return params.delay_crititcal * path_nodes[a].delay_cost + path_nodes[a].cong_cost >
           params.delay_crititcal * path_nodes[b].delay_cost + path_nodes[b].cong_cost;
  };

  std::priority_queue<IndexType, std::vector<IndexType>, decltype(cmp)> q(cmp);


  IndexType                                                             source = routetree.sourceDieId();
  path_nodes.emplace_back(source, 0, 0, -1);
  q.push(0);
  IndexType             vertex_num = routegraph.vertexNum();
  std::vector<CostType> dis(vertex_num, std::numeric_limits<CostType>::max());
  dis[source]       = 0;
  // if (routetree.id() == 98334 && sink == 4)
  // std::cout << source << std::endl;
  auto getDelayCost = [&](IndexType const& v1, IndexType const& v2) {
    if (routegraph.edge(v1, v2).Type() == SLL_EDGE) {
      // return 1.0;
      // if (params.use_estimate_delay) return 1.0;
      // if (params.is_small_case)
      //   return params.sll_delay_estimate +
      //          (((2.0 + routegraph.edge(v1, v2).shareNum() + routegraph.edge(v2, v1).shareNum()) /
      //            database.network(v1, v2)));
      if (routetree.parent()[v2] == v1 && !params.is_tiny_case) return 0.5 * params.sll_delay_estimate;
      return params.sll_delay_estimate;
    } else {
      if (routetree.parent()[v2] == v1 && !params.is_tiny_case)
        return 0.5 * (4.5 + 1.0 * (((routegraph.edge(v1, v2).shareNum() + routegraph.edge(v2, v1).shareNum()) /
                             database.network(v1, v2))));
      // if (params.ignore_sll_cap) return 1.0 + 2.0 * routegraph.edge(v1, v2).shareNum();
      // if (params.use_estimate_delay)
      //   return std::max(0.5 + estimate_delay_[v1][v2],
      //                   4.5 + 1.0 * (((routegraph.edge(v1, v2).shareNum() + routegraph.edge(v2, v1).shareNum()) /
      //                                 database.network(v1, v2))));
      return 4.5 + 1.0 * (((routegraph.edge(v1, v2).shareNum() + routegraph.edge(v2, v1).shareNum()) /
                           database.network(v1, v2)));
      // if (routetree.parent()[v2] == v1)  return 0.0;
      // return 1.0 / database.network(v1, v2);
    }
    return 0.0;
  };
  auto getCongCost = [&](IndexType const& v1, IndexType const& v2) {
    if (params.ignore_sll_cap) return 0.0;
    if (routetree.parent()[v2] == v1) return 0.0;
    auto& edge = routegraph.edge(v1, v2);
    if (edge.Type() == SLL_EDGE) {
      if (edge.shareNum() + routegraph.edge(v2, v1).shareNum() >= database.network(v1, v2)) {
        return (edge.shareNum() + routegraph.edge(v2, v1).shareNum() - database.network(v1, v2) + 1) * params.pres_fac;
      } else {
        return 1.0;
      }
    } else {
      if (edge.shareNum() + routegraph.edge(v2, v1).shareNum() >= params.tdm_max_capacity * database.network(v1, v2)) {
        return (edge.shareNum() + routegraph.edge(v2, v1).shareNum() -
                params.tdm_max_capacity * database.network(v1, v2) + 1) *
               params.pres_fac;
      } else {
        return 1.0;
      }
    }
    return 0.0;
  };
  while (!q.empty()) {
    IndexType now_path_node_id = q.top();
    q.pop();
    auto&     now   = path_nodes[now_path_node_id];
    // if (routetree.netId() == 292)
    // std::cout << now.head_vertex_id << ' ' << now.cong_cost << ' ' << now.delay_cost << ' ' << now.prev_node_id
    //           << std::endl;
    IndexType now_v = now.head_vertex_id;
    if (now_v == sink) {
      while (path_nodes[now_path_node_id].prev_node_id != -1 &&
             routetree.parent()[path_nodes[now_path_node_id].head_vertex_id] == -1) {
        // std::cout << path_nodes[now_path_node_id].head_vertex_id << ' '
        //           << path_nodes[path_nodes[now_path_node_id].prev_node_id].head_vertex_id << std::endl;
        routetree.changeParent(path_nodes[now_path_node_id].head_vertex_id,
                               path_nodes[path_nodes[now_path_node_id].prev_node_id].head_vertex_id);
        routegraph
                .edge(path_nodes[path_nodes[now_path_node_id].prev_node_id].head_vertex_id,
                      path_nodes[now_path_node_id].head_vertex_id)
                .addShareNum();
        now_path_node_id = path_nodes[now_path_node_id].prev_node_id;
      }
      // break;
      return true;
    }
    for (IndexType i = 0; i < vertex_num; i++) {
      // std::cout << now_v << ' ' << i << ' ' << database.network(now_v, i) << std::endl;
      if (database.network(now_v, i) == 0) continue;
      // if (i == 5 && sink == 4 && routetree.id() == 98334) std::cout << "BBB " << routetree.parent()[i] << std::endl;
      if (routetree.parent()[i] != -1 && routetree.parent()[i] != now_v) continue;
      if (params.cap_constr_strict) {
        if (routegraph.edge(now_v, i).Type() == SLL_EDGE &&
            routegraph.edge(now_v, i).shareNum() + routegraph.edge(i, now_v).shareNum() >= database.network(now_v, i) &&
            routetree.parent()[i] != now_v)
          continue;
      }
      CostType delay_cost = getDelayCost(now_v, i);
      CostType cong_cost  = getCongCost(now_v, i);
      // if (routetree.netId() == 292)
      //  std::cout << ' ' << now_v << ' ' << i << ' ' <<  delay_cost << ' ' << cong_cost  <<std::endl;
      // if (now.delay_cost + delay_cost > params.path_max_delay) continue;
      if (dis[i] > dis[now_v] + cong_cost + params.delay_crititcal * delay_cost) {
        dis[i] = dis[now_v] + cong_cost + params.delay_crititcal * delay_cost;
        path_nodes.emplace_back(i, now.delay_cost + delay_cost, now.cong_cost + cong_cost, now_path_node_id);
        q.push(path_nodes.size() - 1);
      }
    }
  }

  return false;
}


void MazeRouter::checkCongAndRipup(database::Database& database, RouteGraph& routegraph, RouteTree& routetree) {
  IndexType vertex_num   = routegraph.vertexNum();
  bool      is_congested = false;
  for (IndexType i = 0; i < vertex_num; i++) {
    IndexType fa = routetree.parent()[i];
    if (fa == -1) continue;
    auto& edge     = routegraph.edge(fa, i);
    auto& edge_inv = routegraph.edge(i, fa);
    if (edge.Type() == SLL_EDGE) {
      if (edge.shareNum() + edge_inv.shareNum() > database.network(fa, i)) {
        is_congested = true;
        break;
      }
    } else if (edge.Type() == TDM_EDGE) {
      if (edge.shareNum() + edge_inv.shareNum() > params.tdm_max_capacity * database.network(fa, i)) {
        is_congested = true;
        break;
      }
    }
  }
  if (is_congested) {
    for (IndexType i = 0; i < vertex_num; i++) {
      IndexType fa = routetree.parent()[i];
      if (fa == -1) continue;
      routetree.changeParent(i, -1);
      routegraph.edge(fa, i).subShareNum();
    }
  }
}

bool MazeRouter::checkCong(database::Database& database, RouteGraph& routegraph, RouteTrees& routetrees) {
  IndexType vertex_num   = routegraph.vertexNum();
  bool      is_congested = false;
  for (auto& routetree : routetrees.trees()) {
    for (IndexType i = 0; i < vertex_num; i++) {
      IndexType fa = routetree.parent()[i];
      if (fa == -1) continue;
      auto& edge     = routegraph.edge(fa, i);
      auto& edge_inv = routegraph.edge(i, fa);
      if (edge.Type() == SLL_EDGE) {
        if (edge.shareNum() + edge_inv.shareNum() > database.network(fa, i)) {
          is_congested = true;
          std::cout << "SLL edge " << fa << " " << i << " congest " << edge.shareNum() + edge_inv.shareNum() << ' '
                    << database.network(fa, i) << std::endl;
          break;
        }
      } else if (edge.Type() == TDM_EDGE) {
        if (edge.shareNum() + edge_inv.shareNum() > params.tdm_max_capacity * database.network(fa, i)) {
          is_congested = true;
          std::cout << "TDM edge " << fa << " " << i << " congest " << edge.shareNum() + edge_inv.shareNum() << ' '
                    << database.network(fa, i) << std::endl;
          break;
        }
      }
    }
    if (is_congested) break;
  }
  return is_congested;
}

void MazeRouter::initializeEstimateDelay(database::Database& database, RouteGraph& routegraph) {
  IndexType vertex_num = routegraph.vertexNum();
  estimate_delay_.resize(vertex_num);
  for (IndexType i = 0; i < vertex_num; i++) estimate_delay_[i].resize(vertex_num);

  for (IndexType i = 0; i < vertex_num; i++)
    for (IndexType j = 0; j < i; j++) {
      auto& edge = routegraph.edge(i, j);
      if (edge.Type() == TDM_EDGE) {
        auto& edge_inv        = routegraph.edge(j, i);
        estimate_delay_[i][j] = estimate_delay_[j][i] =
                (edge.shareNum() + edge_inv.shareNum()) / database.network(i, j);
      }
    }
}
}   // namespace router