#include "./Dijkstra.h"

#include "./router.h"
#include "./routetree.h"

namespace router {
void Dijkstra::dijkstra_router(database::Database &database,
                               RouteGraph         &routegraph,
                               RouteTree          &tree,
                               IndexType          &state) {
  const CostType         INF      = std::numeric_limits<CostType>::max();
  IndexType              numNodes = routegraph.vertexNum();
  std::vector<CostType>  distance(numNodes, INF);
  std::vector<bool>      visited(numNodes, false);
  std::vector<IndexType> parent(numNodes, -1);

  Router                 routerwrite;

  distance[tree.sourceDieId()] = 0;

  struct PathNode {
    IndexType head_vertex;
    // IndexType prev_vertex;
    CostType  node_cost;
    PathNode() {}
    PathNode(IndexType v, IndexType p, CostType c) : head_vertex(v), node_cost(c) {}
  };

  auto pathNodeCmp = [](const PathNode &a, const PathNode &b) { return a.node_cost > b.node_cost; };
  std::priority_queue<PathNode, std::vector<PathNode>, decltype(pathNodeCmp)> q(pathNodeCmp);
  q.push(PathNode(tree.sourceDieId(), -1, 0));
  IndexType vertexNum = routegraph.vertexNum();

  while (!q.empty()) {
    auto now = q.top();
    q.pop();
    IndexType now_v = now.head_vertex;
    if (visited[now_v]) {
      continue;
    }
    visited[now_v] = true;
// 遍历所有该点可连接的顶点
// #pragma omp parallel for
    for (IndexType i = 0; i < vertexNum; i++) {
      // if(database.network(now_v,i) != 0){
      if (database.network(now_v, i) != 0 && state == 0 ||
          (database.network(now_v, i) != 0 && state == 1 &&
                   ((routegraph.edge(now.head_vertex, i).shareNum() + routegraph.edge(i, now.head_vertex).shareNum()) <
                            database.network(now_v, i) &&
                    routegraph.edge(now.head_vertex, i).Type() == 1) ||
           routegraph.edge(now.head_vertex, i).Type() == 0)) {
        CostType cost = routegraph.edge(now.head_vertex, i).congestPunish();
        if (distance[i] > distance[now.head_vertex] + cost) {
          distance[i] = distance[now.head_vertex] + cost;
          q.push(PathNode(i, now_v, distance[i]));
          // parent[i] = now_v;
          tree.changeParent(i, now_v);
        }
      }
      // if (database.network(now_v, i) != 0 && state == 1 &&
      //     ((routegraph.edge(now.head_vertex, i).shareNum() + routegraph.edge(i, now.head_vertex).shareNum()) <
      //              database.network(now_v, i) &&
      //      routegraph.edge(now.head_vertex, i).Type() == 1 || routegraph.edge(now.head_vertex, i).Type() == 0)) {
      //   std::cout << tree.netId() << std::endl;
      //   std::cout << now.head_vertex << " " << i << " " << routegraph.edge(now.head_vertex, i).shareNum() << " "
      //             << routegraph.edge(i, now.head_vertex).shareNum() << " " << database.network(now_v, i) <<
      //             std::endl;
      // }
    }
  }
  // tree.setParent(parent);
}


/*
void Dijkstra::dijkstra_router(IndexType               source_node_die_id,
                               std::vector<IndexType> &target_die_id,
                               database::Database     &database,
                               RouteGraph             &routegraph,
                               //  RouteGraph             &graph,
                               CostType                Aij,
                               RouteTree              &tree,
                               IndexType              &iter) {

  const CostType         INF      = std::numeric_limits<CostType>::max();
  IndexType              numNodes = routegraph.vertexNum();
  std::vector<CostType>  distance(numNodes, INF);
  std::vector<bool>      visited(numNodes, false);
  std::vector<IndexType> parent(numNodes, -1);
  // std::vector<IndexType> parent;

  // CostType Aij = net.netMaxDelay() /
  Router                 routerwrite;

  for (IndexType m = 0; m < target_die_id.size(); m++) {
    tree.setTreeId(m, target_die_id[m]);
  }

  // 输入源节点
  distance[source_node_die_id] = 0;

  // for (IndexType i = 0; i < numNodes - 1; ++i) {
  //   CostType minDist  = INF;
  //   int      minIndex = -1;

  //   for (int j = 0; j < numNodes; ++j) {
  //     if (!visited[j] && distance[j] < minDist)   // 遍历剩下的节点，如果剩下的节点未被访问并且距离小于最小距离，
  //     {
  //       minDist  = distance[j];
  //       minIndex = j;
  //     }
  //   }

  //   visited[minIndex] = true;

  //   for (int j = 0; j < numNodes; ++j) {

  //     if (database.network(minIndex, j) != 0 && !visited[j] && distance[minIndex] != INF) {
  //       // distance[minIndex] + routegraph.edge(minIndex, j).congestPunish() < distance[j]) {
  //       CostType cost = routegraph.edge(minIndex, j).congestPunish();
  //       if (iter > 1 && routegraph.edge(minIndex, j).Type() == 1) cost = Aij + (1 - Aij) * cost;
  //       if (distance[j] > distance[minIndex] + cost) {
  //         distance[j] = distance[minIndex] + cost;
  //         parent[j]   = minIndex;
  //       }
  //     }
  //   }
  // }

  // ----------------------------------
  // Reserve for futrue optimization
  // ----------------------------------
  struct PathNode {
    IndexType head_vertex;
    IndexType prev_vertex;
    CostType  node_cost;
    PathNode() {}
    PathNode(IndexType v, IndexType p, CostType c) : head_vertex(v), prev_vertex(p), node_cost(c) {}
  };

  auto pathNodeCmp = [](const PathNode &a, const PathNode &b) { return a.node_cost > b.node_cost; };
  std::priority_queue<PathNode, std::vector<PathNode>, decltype(pathNodeCmp)> q(pathNodeCmp);
  q.push(PathNode(source_node_die_id, -1, 0));
  IndexType vertexNum = routegraph.vertexNum();
  while (!q.empty()) {
    auto now = q.top();
    q.pop();
    IndexType now_v = now.head_vertex;
    if (visited[now_v]) {
      continue;
    }
    visited[now_v] = true;
    // 遍历所有该点可连接的顶点
    for (IndexType i = 0; i < vertexNum; i++) {
      if(database.network(now_v,i) != 0){
      CostType cost = routegraph.edge(now.head_vertex, i).congestPunish();
      if (iter > 1 && routegraph.edge(now.head_vertex, i).Type() == 1) cost = Aij + (1 - Aij) * cost;
      if (distance[i] > distance[now.head_vertex] + cost) {
        distance[i] = distance[now.head_vertex] + cost;
        q.push(PathNode(i, now_v, distance[i]));
        parent[i] = now_v;
      }
    }
    }
  }

tree.setParent(parent);

    auto &paths = tree.path();
    paths.resize(target_die_id.size());
    for (IndexType i = 0; i < target_die_id.size(); i++) {
      IndexType currentNode = target_die_id[i];
      auto     &path        = paths[i];
      while (currentNode != -1) {
        path.push_back(currentNode);
        currentNode = parent[currentNode];
      }
      reverse(path.begin(), path.end());
      //tree.setpath(i,path);

      for (IndexType j = 0; j < path.size() - 1; j++) {
        bool repeat = false;
        for (auto &edge : tree.tree_edge()) {
          if (edge.fromNodeId() == path[j] && edge.toNodeId() == path[j + 1]) {
            repeat = true;
          }
        }
        if (repeat == false) {
          routegraph.edge(path[j], path[j + 1]).addShareNum();
          tree.setEdge(routegraph.edge(path[j], path[j + 1]));
          if (iter == 0 || iter == 1) {
            if (routegraph.edge(path[j], path[j + 1]).Type() == 0) {
              CostType cn = 4 + 0.5 * (1 + 2 * ((routegraph.edge(path[j], path[j + 1]).share_num() +
                                                 routegraph.edge(path[j + 1], path[j]).share_num()) /
                                                4 / database.network(path[j], path[j + 1])));
              routegraph.edge(path[j], path[j + 1]).setCongestPunish(cn);
              // std::cout << path[j] << " " << path[j+1] << " " << routegraph.edge(path[j], path[j + 1]).share_num() <<
              // " " << routegraph.edge(path[j+1], path[j]).share_num() << " " << cn << std::endl;
            }
          } else {
            if (routegraph.edge(path[j], path[j + 1]).Type() == 0) {
              CostType cn = (4 + 0.5 * (1 + 2 * ((routegraph.edge(path[j], path[j + 1]).share_num() +
                                                  routegraph.edge(path[j + 1], path[j]).share_num()) /
                                                 4 / database.network(path[j], path[j + 1])))) *
                            1000;
              routegraph.edge(path[j], path[j + 1]).setCongestPunish(cn);
            } else if (routegraph.edge(path[j], path[j + 1]).Type()) {
              IndexType congestion_punish = (routegraph.edge(path[j], path[j + 1]).share_num() +
                                             routegraph.edge(path[j + 1], path[j]).share_num()) /
                                            15000;
              CostType cn =
                      routegraph.edge(path[j], path[j + 1]).congestPunish() +
                      routegraph.edge(path[j], path[j + 1]).History() / 100000 * congestion_punish * (1 + 2 * iter);
              // CostType cn = routegraph.edge(path[j], path[j + 1]).congestPunish() + (routegraph.edge(path[j], path[j
              // + 1]).share_num() + routegraph.edge(path[j + 1], path[j]).share_num()) / 20000 * iter;
              routegraph.edge(path[j], path[j + 1]).setCongestPunish(cn);
            }
          }
        }
      }
    }
  }
*/
}   // namespace router