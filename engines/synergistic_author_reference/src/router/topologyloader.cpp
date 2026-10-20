#include "topologyloader.h"

#include "tdmassign.h"

namespace router {

void loadContestRouterResult(std::string const&  file_name,
                             database::Database& database,
                             RouteGraph&         routegraph,
                             RouteTrees&         routetrees) {
  std::unordered_map<IndexType, IndexType> net_line_to_id;
  routegraph.build(database);
  IndexType netlist_size = database.netlist().size();
  routetrees.trees().resize(netlist_size);
  for (IndexType i = 0; i < netlist_size; i++) {
    auto&      net  = database.net(i);
    RouteTree& tree = routetrees.trees()[i];
    tree.Init(database, routegraph, net);
    net_line_to_id[net.lineNum()] = net.id();
  }
  IndexType     vertex_num = routegraph.vertexNum();
  std::ifstream ifs(file_name);
  std::string   s;
  IndexType     net_id;
  while (getline(ifs, s)) {
    if (s.find("][") == std::string::npos && s.find("] [") == std::string::npos) {
      sscanf(s.c_str(), "[%d]", &net_id);
      net_id = net_line_to_id[net_id];
      continue;
    }
    int l   = s.size();
    int pre = -1, now = 0;
    for (int i = 1; i < l; i++) {
      if (s[i] == ']') {
        if (pre != -1) {
          // if (now == 0) std::cout << pre << ' '  std::endl;
          if (routetrees.trees()[net_id].parent()[now] != pre) {
            routetrees.trees()[net_id].changeParent(now, pre);
            routegraph.edge(pre, now).addShareNum();
          }
        }
        break;
      } else if (s[i] == ',' || s[i] == ' ') {
        if (pre != -1) {
          if (routetrees.trees()[net_id].parent()[now] != pre) {
            routetrees.trees()[net_id].changeParent(now, pre);
            routegraph.edge(pre, now).addShareNum();
          }
        }
        pre = now;
        now = 0;
      } else
        now = now * 10 + s[i] - '0';
    }
  }
  ifs.close();
  for (auto& tree : routetrees.trees()) {
    tree.TrackBack();
  }
  TDMAssigner assigner;
  assigner.tdmAssign(database, routegraph, routetrees);
  assigner.outputFile(database, routegraph, routetrees);
}

}   // namespace router