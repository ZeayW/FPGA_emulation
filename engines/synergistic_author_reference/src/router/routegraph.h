#ifndef ROUTER_GRAPH_H_
#define ROUTER_GRAPH_H_

#include "database/database.h"

namespace router {

const int TDM_EDGE = 0;
const int SLL_EDGE = 1;

class RouteGraphEdge {
 public:
  RouteGraphEdge() {}
  RouteGraphEdge(IndexType id) : id_(id), congest_punish_(1), share_num_(0) {}

  IndexType const &id() const { return id_; }
  void             setId(IndexType const &v) { id_ = v; }

  IndexType const &fromNodeId() const { return from_node_id_; }
  void             setFromNodeId(IndexType const &v) { from_node_id_ = v; }

  IndexType const &toNodeId() const { return to_node_id_; }
  void             setToNodeId(IndexType const &v) { to_node_id_ = v; }

  IndexType const &shareNum() const { return share_num_; }
  // IndexType        Num() { return num_; }
  // void             setNum() { num_ = share_num_; }
  void             addShareNum() { share_num_ = share_num_ + 1; }
  void             subShareNum() { share_num_ = share_num_ - 1; }
  //    void             subShareNum() {num_ = num_ - 1; }
  void             InitSharenum() { share_num_ = 0; }

  CostType const  &History() const { return history_; }
  void             addHistory() { history_ = history_ + share_num_; }
  void             addHistory(CostType v) { history_ += v; }

  void             setCongestPunish(CostType &cn) { congest_punish_ = cn; }
  CostType const  &congestPunish() const { return congest_punish_; }

  IndexType const &Type() const { return Type_; }
  void             setType(IndexType const& v = SLL_EDGE) { Type_ = v; }   // Type 0: TDM, Type 1:SLL

  CostType const  &RDealy() const { return ratio_delay; }
  void             setRDealy(CostType d) { ratio_delay = d; }

 protected:
  IndexType id_;
  IndexType from_node_id_;
  IndexType to_node_id_;
  IndexType share_num_ = 0;
  //  IndexType num_;
  IndexType Type_      = -1;
  CostType  congest_punish_;
  CostType  history_    = 0;
  CostType  ratio_delay = 0;
};

class RouteGraph {
 public:
  RouteGraph() {}

  database::Die                             &vertex(IndexType idx) { return vertices_[idx]; }

  RouteGraphEdge                            &edge(IndexType idx, IndexType idy) { return edges_[idx][idy]; }

  std::vector<database::Die>                &vertices() { return vertices_; }

  std::vector<std::vector<RouteGraphEdge> > &edges() { return edges_; }

  std::vector<RouteGraphEdge>               &edgeLine() { return edge_; }
  // std::vector<RouteGraphEdge> &tdmEdge() { return edge_tdm; }

  size_t                                     vertexNum() { return vertices_.size(); }

  size_t                                     edgeNum() { return edge_num; }

  void                                       setEdgeNum(IndexType &num) { edge_num = num; }
  void                                       build(database::Database &database);

 protected:
  std::vector<database::Die>                vertices_;
  std::vector<std::vector<RouteGraphEdge> > edges_;
  std::vector<RouteGraphEdge>               edge_;
  std::vector<RouteGraphEdge>               edge_tdm;
  IndexType                                 edge_num;
};

}   // namespace router

#endif   // ROUTER_ROUTEGRAPH_H_