#include "./routegraph.h"

namespace router {

void RouteGraph::build(database::Database &database) {
  IndexType edge_id = 0;
  edges_.resize(database.dies().size());
  for (IndexType n = 0; n < database.dies().size(); n++) {
    edges_[n].resize(database.dies().size(), 0);
  }

  for (IndexType i = 0; i < database.networks().size(); i++) {
    for (IndexType j = 0; j < database.networks()[i].size(); j++) {
      if (database.network(i, j) != 0) {
        RouteGraphEdge edge;
        edge.setId(edge_id);
        edge.setFromNodeId(i);
        edge.setToNodeId(j);
        if (database.die(i).fpgaId() != database.die(j).fpgaId()) {
          CostType cn = 4.5;
          edge.setCongestPunish(cn);
          edge.setType(TDM_EDGE);
        } else {
          CostType cn = 1;
          edge.setCongestPunish(cn);
          edge.setType(SLL_EDGE);
        }
        edges_[i][j] = edge;
        edge_.push_back(edge);
        // std::cout << " i = " << i << " j = " << j << " Type = " <<  edge.Type() << std::endl;
        edge_id++;
      }
    }
  }
  // for (IndexType x = 0; x < database.networks().size(); x++)
  // {
  //     for (IndexType y = 0; y < database.networks()[x].size(); y++)
  //     {
  //         if (edges_[x][y].Type() == 1 && database.network(x,y)!=0)
  //         {
  //             std::cout << "x = " << x << " y = " << y << " punish = " <<edges_[x][y].congestPunish() << std::endl;
  //         }
  //     }
  // }
  setEdgeNum(edge_id);

  for (auto &vertice : database.dies()) {
    vertices_.emplace_back(vertice);
  }
}

}   // namespace router
