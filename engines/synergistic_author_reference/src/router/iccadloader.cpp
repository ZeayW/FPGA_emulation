#include "iccadloader.h"

#include "tdmassign.h"

namespace router {
void loadICCADRouterResult(std::string const&  file_name,
                           database::Database& database,
                           RouteGraph&         routegraph,
                           RouteTrees&         routetrees) {
  routegraph.build(database);
  IndexType        netlist_size = database.netlist().size();
  routetrees.trees().resize(netlist_size);
  for (IndexType i = 0; i < netlist_size; i++) {
    auto      &net  = database.net(i);
    RouteTree &tree = routetrees.trees()[i];
    tree.Init(database, routegraph, net);
  }

  IndexType              n = database.dies().size();
  IndexType              m = 0;
  std::vector<IndexType> v1, v2;
  std::vector<IndexType> tdm_edge_id;
  IndexType              tdm_edge_num = 0;
  for (IndexType i = 0; i < n; i++)
    for (IndexType j = i + 1; j < n; j++)
      if (database.network(i, j)) {
        m++;
        v1.push_back(i);
        v2.push_back(j);
        // std::cout << i << ' ' << j << std::endl;
        if (routegraph.edge(i, j).Type() == TDM_EDGE) {
          tdm_edge_id.push_back(tdm_edge_num);
          tdm_edge_num++;
        } else {
          tdm_edge_id.push_back(-1);
        }
      }
  std::vector<int> net_ids;
  for (auto& net : database.netlist()) {
    std::vector<int> vis(n, 0);
    bool             flag = 0;
    for (auto node_id : net.loadenNodeId()) {
      IndexType die_id = database.node(node_id).DieId();
      if (die_id != database.node(net.drivenNodeId()).DieId()) {
        flag = 1;
      }
    }
    if (flag) net_ids.push_back(net.id());
    // if (!avi[net.id()]) netlist_size--;
  }

  std::vector<std::vector<CostType>> net_edge_ratio(tdm_edge_num);
  for (IndexType i = 0; i < tdm_edge_num; i++) net_edge_ratio[i].resize(netlist_size, 0);
  std::ifstream ifs(file_name);
  int           k;
  IndexType     now = 0;
  while (ifs >> k) {
    std::vector<IndexType> edge_ids;
    for (IndexType i = 0; i < k; i++) {
      IndexType edge_id, edge_ratio;
      ifs >> edge_id >> edge_ratio;
      edge_ids.push_back(edge_id);
      if (tdm_edge_id[edge_id] != -1)
        net_edge_ratio[tdm_edge_id[edge_id]][net_ids[now]] =
                1.0 * edge_ratio / database.network(v1[edge_id], v2[edge_id]);
    }
    auto&                 net       = database.net(net_ids[now]);
    auto&                 routetree = routetrees.gettree(net_ids[now]);
    IndexType             source    = routetree.sourceDieId();
    std::queue<IndexType> q;
    routetree.changeParent(source, -1);
    q.push(source);
    while (!q.empty()) {
      auto v = q.front();
    //   std::cout << v << ' ' << now << std::endl;
    //   getchar();
      q.pop();
      for (auto edge_id : edge_ids) {
        // std::cout << edge_id << ' ' << v1[edge_id] << ' ' << v2[edge_id] << std::endl;
        if (v1[edge_id] == v && routetree.parent()[v] != v2[edge_id]) {
          routetree.changeParent(v2[edge_id], v);
          q.push(v2[edge_id]);
          routegraph.edge(v1[edge_id], v2[edge_id]).addShareNum();
        } else if (v2[edge_id] == v && routetree.parent()[v] != v1[edge_id]) {
          routetree.changeParent(v1[edge_id], v);
          q.push(v1[edge_id]);
          routegraph.edge(v1[edge_id], v2[edge_id]).addShareNum();
        }
      }
    }
    now++;
  }

  IndexType total_overflow = 0;
  for (IndexType i = 0; i < n; i++)
    for (IndexType j = 0; j < i; j++) {
      if (routegraph.edge(i, j).Type() == SLL_EDGE && routegraph.edge(i, j).shareNum() + routegraph.edge(j, i).shareNum() > database.network(i, j)) {
        total_overflow += routegraph.edge(i, j).shareNum() + routegraph.edge(j, i).shareNum() - database.network(i, j);
      }
    }
    std::cout << "total overflow: " << total_overflow << std::endl;

  TDMAssigner assigner;
  assigner.loadRatioAndLegalize(database, routegraph, routetrees, net_edge_ratio);
}
}   // namespace router