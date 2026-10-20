#include "routetree.h"

// #include <omp.h>
namespace router {

void RouteTree::Init(database::Database &database, RouteGraph &graph, database::Net &net) {
  size_t size_loaden = net.loadenNodeId().size();
  // tree_edge_ratio_.resize(database.networks().size());
  // tree_edge_path_id.resize(graph.edgeNum());
  // tree_edge_delay.resize(graph.edgeNum(), 0);
  // for (IndexType i = 0; i < database.networks().size(); i++) {
  //   tree_edge_ratio_[i].resize(database.networks().size(), 0);
  // }
  // parent_.resize(database.dies().size(),-1);
  delays.resize(size_loaden, 0);
  // update 这些信息可以在这里创建存储
  id_            = net.id();
  net_id_        = net.lineNum();
  source_die_id_ = database.node(net.drivenNodeId()).DieId();
  targets_die_id_.resize(size_loaden);
  // changed = false;
  for (IndexType i = 0; i < size_loaden; i++) {
    targets_die_id_[i] = database.node(net.loadenNodeId()[i]).DieId();
  }

  size_t size_edge = database.dies().size();
  parent_.resize(size_edge, -1);
  tree_edge_info.resize(size_edge);
  for (IndexType i = 0; i < size_edge; i++) {
    tree_edge_info[i].resize(size_edge);
  }

  for (IndexType i = 0; i < size_edge; i++) {
    for (IndexType j = 0; j < size_edge; j++) {
      tree_edge_info[i][j].visited = false;
      tree_edge_info[i][j].edge_path_id = -1;
    }
  }
}


// for (IndexType i = 0; i < size_edge; i++) {
//   for (IndexType j = 0; j < size_edge; j++) {
//     InfoPathsEdge info;
//     tree_edge_info.emplace_back(info);
//   }
// }
void RouteTree::TrackBack() {
  // std::vector<std::vector<IndexType>>().swap(paths_);
  // paths_.resize(targets_die_id_.size());
  paths_.clear();
  paths_.resize(targets_die_id_.size());
  // tree_edge_info.clear();
  IndexType target_dies_id_size = targets_die_id_.size();
// #pragma omp parallel for
  for (IndexType i = 0; i < target_dies_id_size; i++) {
    IndexType              currentNode = targets_die_id_[i];
    // std::vector<IndexType> path;
    // path.reserve(20);
    // std::cout << currentNode << std::endl;
    while (currentNode != -1) {
      paths_[i].push_back(currentNode);
      currentNode = parent_[currentNode];
    }
    // reverse(path.begin(), path.end());
    reverse(paths_[i].begin(), paths_[i].end());
    // std::vector<IndexType>().swap(path);
  }

  // if (net_id_ == 16920) {
  //   for (IndexType i = 0; i < 12; i++) {
  //     for (IndexType j = 0; j < 12; j++) {
  //       std::cout << tree_edge_info[i][j].visited << " ";
  //     }
  //     std::cout << std::endl;
  //   }
  //   std::cout << std::endl;
  // }
  for (IndexType i = 0; i < paths_.size(); i++) {
    for (IndexType j = 0; j < paths_[i].size() - 1; j++) {
      if (tree_edge_info[paths_[i][j]][paths_[i][j + 1]].visited == false) {
        tree_edge_info[paths_[i][j]][paths_[i][j + 1]].visited = true;
      }
    }
  }
}
/*
void RouteTree::NewTrackBack() {

  //std::cout << " NewTrackBack 错误" << std::endl;
  std::vector<std::vector<IndexType>>().swap(paths_);
  paths_.resize(targets_die_id_.size());
  // tree_edge_info.clear();
  std::cout << targets_die_id_.size() << std::endl;
  for (IndexType i = 0; i < targets_die_id_.size(); i++) {
    IndexType              currentNode = targets_die_id_[i];
    std::vector<IndexType> path;
    path.reserve(20);
    std::cout << currentNode << std::endl;
    while (currentNode != -1) {
      path.push_back(currentNode);
      currentNode = parent_[currentNode];
    }
    reverse(path.begin(), path.end());
    for (IndexType j = 0; j < path.size(); j++) {
      std::cout << path[j] << " ";
    }
    std::cout << std::endl;
    paths_[i] = path;
    std::vector<IndexType>().swap(path);
  }

  for (IndexType i = 0; i < targets_die_id_.size(); i++) {
    for (IndexType j = 0; j < paths_[i].size() - 1; j++) {
      if (tree_edge_info[paths_[i][j]][paths_[i][j + 1]].visited == false) {
        tree_edge_info[paths_[i][j]][paths_[i][j + 1]].visited = true;
      }
    }
  }
}
*/
void RouteTree::calculateDelay(database::Database &database, RouteGraph &graph) {
  CostType maxDelay = 0;
  for (IndexType i = 0; i < paths_.size(); i++) {
    CostType delay = 0;
    for (IndexType j = 0; j < paths_[i].size() - 1; j++) {
      if (graph.edge(paths_[i][j], paths_[i][j + 1]).Type() == 1) {
        delay++;
      } else {
        delay = delay + 4 +
                0.5 * (1 + 2 * ((graph.edge(paths_[i][j], paths_[i][j + 1]).shareNum() +
                                 graph.edge(paths_[i][j + 1], paths_[i][j]).shareNum()) /
                                database.network(paths_[i][j], paths_[i][j + 1])));
      }
    }
    delays[i] = delay;
    if (delay > maxDelay) {
      maxDelay    = delay;
      max_path_id = i;
      database.net(id_).setMaxDelay(delay);
    }
    for (IndexType j = 0; j < paths_[i].size() - 1; j++) {
      CostType origin_path_id = tree_edge_info[paths_[i][j]][paths_[i][j + 1]].edge_path_id;
      if (delay >= delays[origin_path_id] && origin_path_id != -1) {
        tree_edge_info[paths_[i][j]][paths_[i][j + 1]].edge_path_id = i;
      } else if (origin_path_id == -1) {
        tree_edge_info[paths_[i][j]][paths_[i][j + 1]].edge_path_id = i;
      }
    }
  }
}

}   // namespace router