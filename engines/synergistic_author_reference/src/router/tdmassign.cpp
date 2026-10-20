#include "tdmassign.h"

// #include <omp.h>

namespace router {
void TDMAssigner::tdmAssign(database::Database &database, RouteGraph &routegraph, RouteTrees &routetrees) {
  std::chrono::high_resolution_clock::time_point as_s, as_t;
  as_s = std::chrono::high_resolution_clock::now();
  initConnectionAndEdge(database, routegraph, routetrees);
  IndexType connection_size = connections.size();
  IndexType netlist_size    = database.netlist().size();
  IndexType tdm_edge_num    = edges.size();

  // std::cout << connection_size << ' ' << netlist_size << ' ' << tdm_edge_num << std::endl;
  // if (netlist_size < params.multi_thread_thres) omp_set_num_threads(1);

  std::vector<CostType> lambda(connection_size);
  for (IndexType i = 0; i < connection_size; i++) lambda[i] = 1.0 / connection_size;

  net_edge_ratio.resize(tdm_edge_num);
  for (IndexType i = 0; i < tdm_edge_num; i++) net_edge_ratio[i].resize(netlist_size, 0);

  IndexType                          max_iter = 100;
  std::vector<std::vector<CostType>> hist_delay_bar(connection_size);
  for (IndexType i = 0; i < connection_size; i++) hist_delay_bar[i].resize(params.window_size, 0);
  for (IndexType iter = 0; iter < max_iter; iter++) {

// #pragma omp parallel for
    for (IndexType j = 0; j < tdm_edge_num; j++) {
      std::vector<CostType> pi(netlist_size, 0.0);
      for (IndexType i = 0; i < connection_size; i++) {
        pi[connections[i].net_id] += connection_edge_usage[j][i] * lambda[i];
      }
      CostType total_sqrt_pi = 0;
      for (IndexType i = 0; i < netlist_size; i++) pi[i] = sqrt(pi[i]);
      for (IndexType i = 0; i < netlist_size; i++) {
        total_sqrt_pi += pi[i];
      }
      // std::cout << j << ' ' << total_sqrt_pi << std::endl;
      // CostType ratio_sum = 0;
      for (IndexType i = 0; i < netlist_size; i++) {
        if (pi[i] != 0) {
          net_edge_ratio[j][i] = total_sqrt_pi / (pi[i] * (edges[j].cap - 1));
          // ratio_sum += 1.0 / net_edge_ratio[j][i];
        }
      }
    }
    // if (iter % 5 == 4)
    // legalizeTDMRatio(database, routegraph, routetrees);

    CostType max_delay  = 0;
    CostType lambda_sum = 0;

// #pragma omp parallel for
    for (IndexType i = 0; i < connection_size; i++) {
      CostType delay = connections[i].sll_delay;
      for (IndexType j = 0; j < tdm_edge_num; j++)
        delay += (0.5 + net_edge_ratio[j][connections[i].net_id]) * connection_edge_usage[j][i];
      // max_delay                    = std::max(max_delay, delay);
      connections[i].connect_delay = delay;
      // lambda_sum += delay * lambda[i];
    }

// #pragma omp parallel for reduction(+ : lambda_sum)
    for (IndexType i = 0; i < connection_size; i++) {
      // max_delay = std::max(max_delay, connections[i].connect_delay);
      lambda_sum += connections[i].connect_delay * lambda[i];
    }

// #pragma omp parallel for reduction(max : max_delay)
    for (IndexType i = 0; i < connection_size; i++) {
      if (connections[i].connect_delay > max_delay) max_delay = connections[i].connect_delay;
    }

// #pragma omp parallel for
    for (IndexType i = 0; i < connection_size; i++) {
      connections[i].delay_bar = connections[i].connect_delay / max_delay;
      CostType x               = 1.0;
      if (iter) {
        CostType sum = 0;
        for (IndexType j = 0; j < params.window_size && j < iter; j++) sum += hist_delay_bar[i][j];
        CostType avg = sum / std::min(params.window_size, iter), std_dev = 0;
        for (IndexType j = 0; j < params.window_size && j < iter; j++)
          std_dev += (hist_delay_bar[i][j] - avg) * (hist_delay_bar[i][j] - avg);
        std_dev /= std::min(params.window_size, iter);
        std_dev = sqrt(std_dev);
        x       = (connections[i].delay_bar - avg);
      }
      hist_delay_bar[i][iter % params.window_size] = connections[i].delay_bar;
      CostType k                                   = (params.alpha - 1.0) * (1.0 / (1.0 * exp(-params.beta * x))) + 1;
      // getchar();
      lambda[i]                                    = lambda[i] * pow(connections[i].delay_bar, k);
      // std::cout << connections[i].delay_bar << ' ' << k << ' ' << lambda[i] <<  std::endl;
    }
    CostType sum_lambda = 0;
// #pragma omp parallel for reduction(+ : sum_lambda)
    for (IndexType i = 0; i < connection_size; i++) sum_lambda += lambda[i];
// #pragma omp parallel for
    for (IndexType i = 0; i < connection_size; i++) lambda[i] /= sum_lambda;

    // legalizeTDMRatio(database, routegraph, routetrees);
    if (iter % 1 == 0) {
      printf("Iter #%d, primal: %lf, dual: %lf\n, offset: %lf\n",
             iter,
             max_delay,
             lambda_sum,
             (max_delay - lambda_sum) / lambda_sum);
    }
    if ((max_delay - lambda_sum) / lambda_sum <= params.break_percent) break;
  }
  as_t = std::chrono::high_resolution_clock::now();
  std::chrono::duration<double, std::ratio<1, 1>> d_as(as_t - as_s);
  std::cout << "TDM assignment: " << d_as.count() << "s" << std::endl;

  std::chrono::high_resolution_clock::time_point lg_s, lg_t;
  lg_s = std::chrono::high_resolution_clock::now();
  legalizeTDMRatio(database, routegraph, routetrees);
  lg_t = std::chrono::high_resolution_clock::now();
  std::chrono::duration<double, std::ratio<1, 1>> d_lg(lg_t - lg_s);
  std::cout << "TDM legalization: " << d_lg.count() << "s" << std::endl;
}

void TDMAssigner::initConnectionAndEdge(database::Database &database, RouteGraph &routegraph, RouteTrees &routetrees) {
  IndexType connection_size = 0;
  IndexType vertex_num      = routegraph.vertexNum();
  for (auto &tree : routetrees.trees()) {
    std::vector<IndexType> visited(vertex_num);
    visited[tree.sourceDieId()] = true;
    for (auto dieId : tree.targetsDieId()) {
      if (visited[dieId]) continue;
      visited[dieId] = true;
      connections.emplace_back(tree.sourceDieId(), dieId, tree.id());
      connection_size++;
    }
  }
  // std::cout << connection_size << std::endl;
  IndexType                           tdm_edge_num = 0;
  std::vector<std::vector<IndexType>> edge_id(vertex_num);
  for (IndexType i = 0; i < vertex_num; i++) edge_id[i].resize(vertex_num, 0);
  for (IndexType i = 0; i < vertex_num; i++) {
    for (IndexType j = i + 1; j < vertex_num; j++) {
      if (routegraph.edge(i, j).Type() == TDM_EDGE) {
        edges.emplace_back(i, j, database.network(i, j));
        edge_id[i][j] = edge_id[j][i] = tdm_edge_num;
        tdm_edge_num++;
      }
    }
  }
  connection_edge_usage.resize(tdm_edge_num);
  for (IndexType i = 0; i < tdm_edge_num; i++) connection_edge_usage[i].resize(connection_size);
// #pragma omp parallel for
  for (IndexType i = 0; i < connection_size; i++) {
    IndexType now  = connections[i].to;
    auto     &tree = routetrees.gettree(connections[i].net_id);
    while (tree.parent()[now] != -1) {
      IndexType fa = tree.parent()[now];
      if (routegraph.edge(fa, now).Type() == TDM_EDGE) {
        connection_edge_usage[edge_id[fa][now]][i] = true;
        connections[i].connect_delay += (routegraph.edge(fa, now).shareNum() + routegraph.edge(now, fa).shareNum()) /
                                                database.network(fa, now) +
                                        0.5;
      } else {
        connections[i].sll_delay += 1;
        connections[i].connect_delay += 1;
      }
      now = fa;
    }
  }
}
struct LGNode {
  CostType  criticality, ratio;
  IndexType net_id;

  LGNode() {}
  LGNode(CostType v1, CostType v2, IndexType v3) : criticality(v1), ratio(v2), net_id(v3) {}
};
bool operator<(const LGNode &x, const LGNode &y) {
  if (x.criticality == y.criticality) return x.ratio < y.ratio;
  return x.criticality < y.criticality;
}

void TDMAssigner::legalizeTDMRatio(database::Database &database, RouteGraph &routegraph, RouteTrees &routetrees) {
  IndexType                          connection_size = connections.size();
  IndexType                          tdm_edge_num    = edges.size();
  IndexType                          netlist_size    = database.netlist().size();

  IndexType total_cap = 0;
  for (IndexType i = 0; i < tdm_edge_num; i++) total_cap += edges[i].cap;
  if (total_cap < 100) params.remain_margin = 0.5;

  std::vector<std::vector<CostType>> edge_net_criticality(tdm_edge_num);
  for (IndexType j = 0; j < tdm_edge_num; j++) edge_net_criticality[j].resize(netlist_size, 0);
  for (IndexType i = 0; i < connection_size; i++) {
    CostType delay = connections[i].sll_delay;
    for (IndexType j = 0; j < tdm_edge_num; j++) {
      CostType up_ratio = (((int) std::ceil(net_edge_ratio[j][connections[i].net_id]) - 1) / 8 + 1) * 8;
      delay += (0.5 + up_ratio) * connection_edge_usage[j][i];
    }
    for (IndexType j = 0; j < tdm_edge_num; j++)
      if (connection_edge_usage[j][i])
        edge_net_criticality[j][connections[i].net_id] =
                std::max(edge_net_criticality[j][connections[i].net_id], delay);
  }
// #pragma omp parallel for
  for (IndexType j = 0; j < tdm_edge_num; j++) {
    std::vector<IndexType> edge_direction(netlist_size, 0);
    std::vector<IndexType> net_id(netlist_size);
    IndexType              from_edge_use = 0, to_edge_use = 0;
    // #pragma omp parallel for
    CostType               ratio_sum    = 0;
    CostType               to_ratio_sum = 0, from_ratio_sum = 0;
    for (IndexType i = 0; i < netlist_size; i++) {
      auto &tree = routetrees.gettree(i);
      net_id[i]  = i;
      if (net_edge_ratio[j][i] == 0) continue;
      ratio_sum += 1.0 / net_edge_ratio[j][i];
      if (tree.parent()[edges[j].from] == edges[j].to) {
        edge_direction[i] = 1;
        from_edge_use++;
        from_ratio_sum += 1.0 / net_edge_ratio[j][i];
      } else if (tree.parent()[edges[j].to] == edges[j].from) {
        edge_direction[i] = -1;
        to_edge_use++;
        to_ratio_sum += 1.0 / net_edge_ratio[j][i];
      } else {
        edge_direction[i] = 0;
      }
      // net_edge_ratio[j][i] = (((int) std::ceil(net_edge_ratio[j][i]) - 1) / 8 + 1) * 8;
    }
    // std::cout << ratio_sum << ' ' << from_ratio_sum << ' ' << to_ratio_sum << ' ' << from_ratio_sum + to_ratio_sum <<
    // edges[j].cap << std::endl;

    auto cmp = [&](IndexType const &x, IndexType const &y) {
      if (edge_direction[x] == edge_direction[y]) {
        if (net_edge_ratio[j][x] == net_edge_ratio[j][y]) {
          return edge_net_criticality[j][x] > edge_net_criticality[j][y];
        }
        return net_edge_ratio[j][x] < net_edge_ratio[j][y];
      }
      return edge_direction[x] < edge_direction[y];
    };
    CostType                    tdm_sum = 0;
    std::priority_queue<LGNode> pq_to, pq_from;
    CostType                    to_ratio_use = 0, from_ratio_use = 0;
    for (IndexType i = 0; i < netlist_size; i++) {
      // if (net_edge_ratio[j][i] == 0) continue;
      CostType up_ratio = (((int) std::ceil(net_edge_ratio[j][i]) - 1) / 8 + 1) * 8;
      if (edge_direction[i] == -1) {
        to_ratio_use += 1.0 / up_ratio;
        pq_to.emplace(edge_net_criticality[j][i], up_ratio, i);
      } else if (edge_direction[i] == 1) {
        // std::cout << net_edge_ratio[j][i] << ' ';
        from_ratio_use += 1.0 / up_ratio;
        pq_from.emplace(edge_net_criticality[j][i], up_ratio, i);
      }
      // std::cout << up_ratio << ' ' << net_edge_ratio[j][i] << std::endl;
      // getchar();
      // if (up_ratio - net_edge_ratio[j][i] < 3.75 || up_ratio == 8)
      net_edge_ratio[j][i] = up_ratio;
      // else net_edge_ratio[j][i] = std::max(8.0, up_ratio - 8);
    }
    // std::cout << std::endl << from_ratio_sum << ' ' << from_ratio_use << std::endl;
    tdm_sum = std::ceil(to_ratio_sum);
    CostType tdm_sum_from = 0;
    tdm_sum_from          = std::ceil(from_ratio_sum);
    // std::cout << to_ratio_use << ' ' << tdm_sum << std::endl;
    // std::cout << from_ratio_use << ' ' << tdm_sum_from << std::endl;
    while (!pq_to.empty() && tdm_sum - to_ratio_use > params.remain_margin) {
      auto now = pq_to.top();
      pq_to.pop();
      if (now.ratio <= 8) continue;
      to_ratio_use = to_ratio_use - 1.0 / now.ratio + 1.0 / (now.ratio - 8);
      net_edge_ratio[j][now.net_id] = now.ratio - 8;
      edge_net_criticality[j][now.net_id] -= 8;
      pq_to.emplace(now.criticality - 8, now.ratio - 8, now.net_id);
    }
    while (!pq_from.empty() && tdm_sum_from - from_ratio_use > params.remain_margin) {
      auto now = pq_from.top();
      pq_from.pop();
      if (now.ratio <= 8) continue;
      from_ratio_use = from_ratio_use - 1.0 / now.ratio + 1.0 / (now.ratio - 8);
      net_edge_ratio[j][now.net_id] = now.ratio - 8;
      edge_net_criticality[j][now.net_id] -= 8;
      pq_from.emplace(now.criticality - 8, now.ratio - 8, now.net_id);
    }
    sort(net_id.begin(), net_id.end(), cmp);
    // #pragma omp parallel for reduction(+ : tdm_sum)
    // std::cout << edge_direction[net_id[to_edge_use]] << std::endl;
    // for (IndexType i = 0; i < to_edge_use; i++) {
    //   // if (edge_direction[net_id[i]] != -1) {
    //   //   std::cout << "[error]" << i << ' ' << net_id[i] << std::endl;

    //   // }
    //   tdm_sum += 1.0 / net_edge_ratio[j][net_id[i]];
    // }
    std::vector<IndexType> to_edge_wire_ratio;
    IndexType              assigned_nets = 0;
    for (IndexType i = 0; i < to_edge_use && to_edge_wire_ratio.size() < tdm_sum; i += net_edge_ratio[j][net_id[i]]) {
      assigned_nets += net_edge_ratio[j][net_id[i]];
      IndexType r = net_edge_ratio[j][net_id[i]];
      // std::cout << r << ' ' << to_edge_use - i << ' ' << (((to_edge_use - i) - 1) / 8 + 1) * 8 << std::endl;
      if (r > to_edge_use - i) r = (((to_edge_use - i) - 1) / 8 + 1) * 8;
      to_edge_wire_ratio.push_back(r);
      // std::cout << r << std::endl;
    }
    // std::cout << to_edge_wire_ratio.size() << ' ' << tdm_sum << std::endl;
    while (to_edge_wire_ratio.size() < tdm_sum) {
      IndexType i = to_edge_wire_ratio.size() - 1;
      IndexType r = 0;
      while (i >= 0 && r + 8 < to_edge_wire_ratio[to_edge_wire_ratio.size() - 1]) {
        to_edge_wire_ratio[i] -= 8;
        r += 8;
      }
      to_edge_wire_ratio.push_back(r);
    }

    if (assigned_nets < to_edge_use) {
      IndexType add_ratio   = (to_edge_use - assigned_nets) / (8 * (IndexType) tdm_sum) * 8;
      IndexType remain_nets = to_edge_use - assigned_nets - add_ratio * tdm_sum;
      for (IndexType i = 0; i < to_edge_wire_ratio.size(); i++) to_edge_wire_ratio[i] += add_ratio;
      IndexType i = to_edge_wire_ratio.size() - 1;
      while (remain_nets > 0) {
        to_edge_wire_ratio[i] += 8;
        i--;
        remain_nets -= 8;
      }
    }
    sort(to_edge_wire_ratio.begin(), to_edge_wire_ratio.end());
    IndexType now = 0;
    for (IndexType i = 0; i < to_edge_wire_ratio.size(); i++) {
      // #pragma omp parallel for
      for (IndexType k = 0; k < to_edge_wire_ratio[i] && now + k < to_edge_use; k++)
        net_edge_ratio[j][net_id[now + k]] = to_edge_wire_ratio[i];
      now += to_edge_wire_ratio[i];
    }
    if (now < to_edge_use) {
      // std::cout << assigned_nets << ' ' << to_edge_use << ' ' << tdm_sum << ' ' << now << std::endl;
      for (IndexType i = 0; i < to_edge_wire_ratio.size(); i++) std::cout << to_edge_wire_ratio[i] << std::endl;
      std::cout << "[error] Nets using to edges unassigned ratio" << std::endl;
    }

    // #pragma omp parallel for reduction(+ : tdm_sum)
    // for (IndexType i = netlist_size - from_edge_use; i < netlist_size; i++) {
    //   // if (edge_direction[net_id[i]] != 1) {
    //   //   std::cout << "[error]" << i << ' ' << net_id[i] << std::endl;

    //   // }
    //   tdm_sum_from += 1.0 / net_edge_ratio[j][net_id[i]];
    // }
    // std::cout << tdm_sum << ' ' << tdm_sum_from << ' ' << tdm_sum + tdm_sum_from << ' ' <<
    // database.network(edges[j].from, edges[j].to) << ' ' << edges[j].cap << std::endl;
    std::vector<IndexType> from_edge_wire_ratio;
    assigned_nets = 0;
    for (IndexType i = netlist_size - from_edge_use; i < netlist_size && from_edge_wire_ratio.size() < tdm_sum_from;
         i += net_edge_ratio[j][net_id[i]]) {
      assigned_nets += net_edge_ratio[j][net_id[i]];
      IndexType r = net_edge_ratio[j][net_id[i]];
      if (r > netlist_size - i) r = (((netlist_size - i) - 1) / 8 + 1) * 8;
      from_edge_wire_ratio.push_back(r);
      // std::cout << r << std::endl;
    }
    // std::cout << assigned_nets << ' ' << from_edge_use << std::endl;
    if (assigned_nets < from_edge_use) {
      IndexType add_ratio   = (from_edge_use - assigned_nets) / (8 * (IndexType) tdm_sum_from) * 8;
      IndexType remain_nets = from_edge_use - assigned_nets - add_ratio * tdm_sum_from;
      for (IndexType i = 0; i < from_edge_wire_ratio.size(); i++) from_edge_wire_ratio[i] += add_ratio;
      IndexType i = from_edge_wire_ratio.size() - 1;
      while (remain_nets > 0) {
        from_edge_wire_ratio[i] += 8;
        i--;
        remain_nets -= 8;
      }
    }
    while (from_edge_wire_ratio.size() < tdm_sum_from) {
      IndexType i = from_edge_wire_ratio.size() - 1;
      IndexType r = 0;
      while (i >= 0 && r + 8 < from_edge_wire_ratio[from_edge_wire_ratio.size() - 1]) {
        from_edge_wire_ratio[i] -= 8;
        r += 8;
      }
      from_edge_wire_ratio.push_back(r);
    }
    sort(from_edge_wire_ratio.begin(), from_edge_wire_ratio.end());
    now = netlist_size - from_edge_use;
    for (IndexType i = 0; i < from_edge_wire_ratio.size(); i++) {
      // #pragma omp parallel for
      for (IndexType k = 0; k < from_edge_wire_ratio[i] && now + k < netlist_size; k++)
        net_edge_ratio[j][net_id[now + k]] = from_edge_wire_ratio[i];
      now += from_edge_wire_ratio[i];
    }
    if (now < netlist_size) {
      std::cout << "[error] Nets using from edges unassigned ratio" << std::endl;
    }
  }

  CostType max_delay = 0;
  for (IndexType i = 0; i < connection_size; i++) {
    CostType delay = connections[i].sll_delay;
    for (IndexType j = 0; j < tdm_edge_num; j++)
      delay += (0.5 + net_edge_ratio[j][connections[i].net_id]) * connection_edge_usage[j][i];
    max_delay                    = std::max(max_delay, delay);
    connections[i].connect_delay = delay;
  }
  std::cout << "Max delay after legalization: " << max_delay << std::endl;
}
void TDMAssigner::outputFile(database::Database &database, RouteGraph &routegraph, RouteTrees &routetrees) {
  std::vector<database::Net> &netlist      = database.netlist();
  IndexType                   netlist_size = netlist.size();
  std::vector<IndexType>      net_ids(netlist_size);
  for (IndexType i = 0; i < netlist_size; i++) net_ids[i] = i;

  // 输出design.route.out
  //  把connect结果更新到树
  for (auto &connection : connections) {
    auto  &tree = routetrees.trees()[connection.net_id];
    size_t sz   = tree.targetsDieId().size();
    if (connection.from == tree.sourceDieId()) {
      for (IndexType i = 0; i < sz; i++) {
        if (tree.targetsDieId()[i] == connection.to) {
          tree.setPathDelay(i, connection.connect_delay);
        }
      }
    } else if (connection.to == tree.sourceDieId()) {
      for (IndexType i = 0; i < sz; i++) {
        if (tree.targetsDieId()[i] == connection.from) {
          tree.setPathDelay(i, connection.connect_delay);
        }
      }
    }
  }

  // 确定每棵树的maxdelay
  for (auto &tree : routetrees.trees()) {
    size_t   sz        = tree.paths().size();
    CostType max_delay = 0;
    for (IndexType i = 0; i < sz; i++) {
      if (tree.delay(i) > max_delay) {
        max_delay = tree.delay(i);
      }
    }
    database.net(tree.id()).setMaxDelay(max_delay);
  }

  auto cmp = [&](IndexType x, IndexType y) {
    return database::Database::netCompareDelay(database.net(x), database.net(y));
  };
  std::sort(net_ids.begin(), net_ids.end(), cmp);

  FILE *out_file_1 = fopen("design.route.out", "w");
  for (auto id : net_ids) {
    auto &tree = routetrees.trees()[id];

    fprintf(out_file_1, "[%d]\n", database.net(tree.id()).lineNum());
    size_t sz = tree.paths().size();
    std::vector<IndexType> idxs(sz);
    for (IndexType i = 0; i < sz; i++) idxs[i] = i;
    auto cmp_path = [&](IndexType x, IndexType y) {
      return tree.delay(x) > tree.delay(y);
    };
    std::sort(idxs.begin(), idxs.end(), cmp_path);
    for (IndexType i = 0; i < sz; i++) {
      size_t sz_path = tree.paths()[idxs[i]].size();
      for (IndexType j = 0; j < sz_path; j++) {
        if (j == 0) {
          if (sz_path == 1) {
            fprintf(out_file_1, "[%d][%.1f]\n", tree.paths()[idxs[i]][j], 0.0);
          } else {
            fprintf(out_file_1, "[%d", tree.paths()[idxs[i]][j]);
          }
        } else if (j == sz_path - 1) {
          fprintf(out_file_1, " %d][%.1f]\n", tree.paths()[idxs[i]][j], tree.delay(i));
        } else {
          fprintf(out_file_1, " %d", tree.paths()[idxs[i]][j]);
        }
      }
    }
  }
  fclose(out_file_1);

  FILE* out_file_2 = fopen("design.newtopo", "w");
  IndexType fpga_num = database.dies().size();
  for (IndexType i = 0; i < fpga_num; i++) {
    fprintf(out_file_2, "%s: ", database.die(i).name().c_str());
    for (IndexType j = 0; j < fpga_num; j++) {
      fprintf(out_file_2, "%d%c", database.network(i, j), j == fpga_num - 1 ? '\n' : ' ');
    }
  }
  fclose(out_file_2);

  // design.tdm.out
  // FILE  *out_file_2    = fopen("design.tdm.out", "w");
  // size_t tdm_edge_size = edges.size();
  // for (IndexType j = 0; j < tdm_edge_size; j++) {
  //   std::vector<IndexType> edge_direction(netlist_size, 0);
  //   std::vector<IndexType> net_id(netlist_size);
  //   IndexType              from_edge_use = 0, to_edge_use = 0;
  //   for (IndexType i = 0; i < netlist_size; i++) {
  //     auto &tree = routetrees.gettree(i);
  //     net_id[i]  = i;
  //     if (net_edge_ratio[j][i] == 0) continue;
  //     if (tree.parent()[edges[j].from] == edges[j].to) {
  //       edge_direction[i] = 1;
  //       from_edge_use++;
  //     } else if (tree.parent()[edges[j].to] == edges[j].from) {
  //       edge_direction[i] = -1;
  //       to_edge_use++;
  //     } else {
  //       edge_direction[i] = 0;
  //     }
  //   }
  //   auto cmp = [&](IndexType const &x, IndexType const &y) {
  //     if (edge_direction[x] == edge_direction[y]) {
  //       if (net_edge_ratio[j][x] == net_edge_ratio[j][y]) {
  //         return x < y;
  //       }
  //       return net_edge_ratio[j][x] < net_edge_ratio[j][y];
  //     }
  //     return edge_direction[x] < edge_direction[y];
  //   };
  //   sort(net_id.begin(), net_id.end(), cmp);
  //   if (to_edge_use || from_edge_use) fprintf(out_file_2, "[Die%d,Die%d]\n", edges[j].from, edges[j].to);
  //   if (to_edge_use) {
  //     // fprintf(out_file_2, "[Die%d,Die%d]\n", edges[j].from, edges[j].to);
  //     // std::cout << j << ' ' << from_edge_use << ' ' << to_edge_use << std::endl;
  //     IndexType cnt = 0;
  //     for (IndexType i = 0; i < to_edge_use; i++) {
  //       if (i && net_edge_ratio[j][net_id[i]] != net_edge_ratio[j][net_id[i - 1]]) cnt = 0;
  //       if (!cnt) {
  //         fprintf(out_file_2, "[%d,", database.net(net_id[i]).lineNum());
  //         cnt++;
  //       } else if (cnt == net_edge_ratio[j][net_id[i]] - 1 || i == to_edge_use - 1 ||
  //                  net_edge_ratio[j][net_id[i + 1]] != net_edge_ratio[j][net_id[i]]) {
  //         fprintf(out_file_2, "%d] %d\n", database.net(net_id[i]).lineNum(), (IndexType) net_edge_ratio[j][net_id[i]]);
  //         cnt = 0;
  //       } else {
  //         fprintf(out_file_2, "%d,", database.net(net_id[i]).lineNum());
  //         cnt++;
  //       }
  //     }
  //     if (cnt) {
  //       fprintf(out_file_2, "] %d\n", (IndexType) net_edge_ratio[j][net_id[to_edge_use - 1]]);
  //     }
  //   }
  //   if (from_edge_use) {
  //     // fprintf(out_file_2, "[Die%d,Die%d]\n", edges[j].to, edges[j].from);
  //     IndexType cnt = 0;
  //     for (IndexType i = netlist_size - from_edge_use; i < netlist_size; i++) {
  //       if (net_edge_ratio[j][net_id[i]] != net_edge_ratio[j][net_id[i - 1]]) cnt = 0;
  //       if (!cnt) {
  //         fprintf(out_file_2, "[%d,", database.net(net_id[i]).lineNum());
  //         cnt++;
  //       } else if (cnt == net_edge_ratio[j][net_id[i]] - 1 || i == netlist_size - 1 ||
  //                  net_edge_ratio[j][net_id[i + 1]] != net_edge_ratio[j][net_id[i]]) {
  //         fprintf(out_file_2, "%d] %d\n", database.net(net_id[i]).lineNum(), (IndexType) net_edge_ratio[j][net_id[i]]);
  //         cnt = 0;
  //       } else {
  //         fprintf(out_file_2, "%d,", database.net(net_id[i]).lineNum());
  //         cnt++;
  //       }
  //     }
  //     if (cnt) {
  //       fprintf(out_file_2, "] %d\n", (IndexType) net_edge_ratio[j][net_id[netlist_size - 1]]);
  //     }
  //   }
  // }
  // fclose(out_file_2);
}

void TDMAssigner::loadRatioAndLegalize(database::Database                       &database,
                                       RouteGraph                               &routegraph,
                                       RouteTrees                               &routetrees,
                                       std::vector<std::vector<CostType>> const &init_ratios) {
  initConnectionAndEdge(database, routegraph, routetrees);
  IndexType tdm_edge_num = edges.size();
  IndexType netlist_size = database.netlist().size();
  for (IndexType i = 0; i < tdm_edge_num; i++) {
    net_edge_ratio.emplace_back();
    for (IndexType j = 0; j < netlist_size; j++) net_edge_ratio[i].push_back(init_ratios[i][j]);
  }
  legalizeTDMRatio(database, routegraph, routetrees);
}

}   // namespace router