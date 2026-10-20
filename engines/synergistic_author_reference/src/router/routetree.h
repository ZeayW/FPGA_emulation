#ifndef ROUTER_ROUTETREE_H_
#define ROUTER_ROUTETREE_H_

#include "../database/database.h"
#include "../utils.h"
#include "./routegraph.h"

namespace router {

struct InfoPathsEdge {
  bool      visited      = false;
  IndexType edge_path_id = -1;   // 经过该edge最长的路径id
  IndexType edge_ratio   = 0;    // 该edge对应的ratio
};

class RouteTree {
 public:
  RouteTree() {}
  RouteTree(IndexType &v1) : id_(v1) {}
  ~RouteTree() {}

  IndexType const                     &id() const { return id_; }          // 树的位置id
  IndexType const                     &netId() const { return net_id_; }   // 树对应的net的id编号
  IndexType const                     &sourceDieId() const { return source_die_id_; }
  // IndexType const                           &root_die_id() const { return source_die_id_; }
  // CostType                                  &maxDelay() { return max_delay; }
  CostType const                      &maxDelay() const { return delays[max_path_id]; }
  CostType const                      &delay(IndexType i) const { return delays[i]; }
  std::vector<IndexType> const              &targetsDieId() const { return targets_die_id_; }
  // std::vector<IndexType> const              &tree_die_id() const { return tree_die_id_; }
  // std::vector<std::vector<IndexType>> const &paths() const { return paths_; }
  std::vector<std::vector<IndexType>> &paths() { return paths_; }
  // std::vector<RouteGraphEdge> const         &tree_edge() const { return tree_edge_; }
  // // std::vector<RouteGraphEdge> const &tree_edge_tdm() const { return tree_edge_tdm; }
  // std::vector<CostType> const               &pathDelay() const { return delay; }
  // std::vector<std::vector<IndexType>> const &edgeRatio() const { return tree_edge_ratio_; }
  // std::vector<CostType> const               &edgeDelay() const { return tree_edge_delay; }
  // std::vector<IndexType> const              &edgePath() const { return tree_edge_path_id; }
  std::vector<IndexType> const        &maxPath() const { return paths_[max_path_id]; }
  IndexType const &maxPathId() const{ return max_path_id;}
  std::vector<IndexType> const        &parent() const { return parent_; }
  bool                                &thoughEdge(IndexType x, IndexType y) { return tree_edge_info[x][y].visited; }
  IndexType &thoughEdgePathId(IndexType x, IndexType y) { return tree_edge_info[x][y].edge_path_id; }
  bool      &Changed() { return changed; }
  CostType &CurrentDelay() {return current_edge_delay; }
  IndexType &MaxDiePair(){ return max_pair_group; }


  void       Init(database::Database &database, RouteGraph &graph, database::Net &net);
  void       TrackBack();
  // void                                 NewTrackBack();
  void       calculateDelay(database::Database &database, RouteGraph &graph);
  // void                                       setId(IndexType v1) { id_ = v1; }
  //  void                                       setNetId(IndexType id) { net_id_ = id; }
  void       setpath(IndexType i, std::vector<IndexType> targetpath) { paths_[i] = targetpath; }
  void       setChanged() { changed = true; }
  void       reChanged() { changed = false; }
  // void setMaxPath(std::vector<IndexType> targetpath) { max_path = targetpath; }
  //  void setRootId(IndexType id) { source_die_id_ = id; }
  //  void setTreeId(IndexType v0, IndexType v1) { tree_die_id_[v0] = v1; }
  //   void setEdge(RouteGraphEdge edge) { tree_edge_.push_back(edge); }
  //  void initEdge() { tree_edge_.resize(tree_edge_delay.size()); }
  //  void setMaxDelay(CostType d) { max_delay = d; }
  //  void setPathDelay(CostType a) {delay.emplace_back(a);}
  void       setPathDelay(IndexType i, CostType a) { delays[i] = a; }
  // void setEdgeRatio(IndexType x, IndexType y, IndexType ratio) { tree_edge_ratio_[x][y] = ratio; }
  // void setEdgeDelay(IndexType id, CostType delay) { tree_edge_delay[id] = delay; }
  // void setEdgePath(IndexType id, IndexType path) { tree_edge_path_id[id] = path; }
  void       setParent(std::vector<IndexType> parent) { parent_ = parent; }
  void       changeParent(IndexType i, IndexType j) { parent_[i] = j; }
void setCurrentDelay(CostType c) {current_edge_delay = c;}
void setMaxGroup(IndexType n) { max_pair_group = n; }
 protected:
  IndexType                               id_;
  IndexType                               net_id_;
  IndexType                               source_die_id_;
  std::vector<IndexType>                  targets_die_id_;
  std::vector<IndexType>                  parent_;
  std::vector<std::vector<IndexType>>     paths_;
  std::vector<CostType>                   delays;
  IndexType                               max_path_id;
  // struct InfoPathsEdge {
  //   bool      visited      = false;
  //   IndexType edge_path_id = 0;   // 经过该edge最长的路径id
  //   IndexType edge_ratio   = 0;   // 该edge对应的ratio
  // };
  std::vector<std::vector<InfoPathsEdge>> tree_edge_info;
  bool                                    changed = false;
  CostType current_edge_delay = 0;
  IndexType max_pair_group;
  // CostType               max_delay = 0;
  // std::vector<CostType>               delay;
  // std::vector<IndexType>              tree_die_id;
  // std::vector<RouteGraphEdge>         tree_edge_;
  // std::vector<std::vector<IndexType>> tree_edge_ratio_;
  // std::vector<CostType>               tree_edge_delay;
  // std::vector<IndexType>              tree_edge_path_id;
  // std::vector<IndexType> max_path;
};

class RouteTrees {
 public:
  RouteTrees() {}
  ~RouteTrees() {}
  void                    clear() { trees_.clear(); }

  std::vector<RouteTree> &trees() { return trees_; }

  RouteTree              &gettree(IndexType id) { return trees_[id]; }

  void                    setRouteTrees(IndexType id, RouteTree tr) { trees_[id] = tr; }
  static bool             compareDelay(RouteTree const &a, RouteTree const &b) { return a.maxDelay() > b.maxDelay(); }

 protected:
  std::vector<RouteTree> trees_;
};
}   // namespace router

#endif