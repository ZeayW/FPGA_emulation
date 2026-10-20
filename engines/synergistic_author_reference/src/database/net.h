#ifndef DATABASE_NET_H_
#define DATABASE_NET_H_
#include "../utils.h"

namespace database {

class Net {
 public:
  Net() {}
  Net(IndexType id) : id_(id) {}
  ~Net() {}

  IndexType const              &id() const { return id_; }
  IndexType const              &lineNum() const { return line_num; }
  IndexType const              &drivenNodeId() const { return net_node_ids_s; }
  CostType                     &netMaxDelay() { return max_delay; }
  CostType const               &netMaxDelay() const { return max_delay; }
  CostType                     &slackRatio() { return slack_ratio; }
  CostType                     &edgeDelay() { return edge_delay; }
  CostType const               &edgeDelay() const { return edge_delay; }
  // IndexType  &tdmNum() { return tdm_num; }
  std::vector<IndexType> const &loadenNodeId() const { return net_node_ids_l; }

  bool                          needsRerouting() const { return needsRerouting_; }
  void                          setid(IndexType const &id) { id_ = id; }
  void                          setLineNum(IndexType const &num) { line_num = num; }
  void                          setNodeIds_s(IndexType const &id) { net_node_ids_s = id; }
  void                          setNodeIds_l(IndexType const &v1) { net_node_ids_l.push_back(v1); }
  void                          setneedsRerouting(bool const &y) { needsRerouting_ = y; }
  void                          setMaxDelay(CostType const &i) { max_delay = i; }
  void                          setSlackRatio(CostType const &ratio) { slack_ratio = ratio; }
  // void settdmNum( IndexType const &num ) { tdm_num = num; }
  void                          setEdgeDelay(CostType const &delay) { edge_delay = delay; }
  static bool                   DelayCompare(const Net &a, const Net &b) { return a.edgeDelay() > b.edgeDelay(); }

  IndexType                     memory() const;

 protected:
  IndexType              id_;              // net的位置
  IndexType              line_num;         // net所在的行号
  IndexType              net_node_ids_s;   // net的驱动节点编号
  std::vector<IndexType> net_node_ids_l;   // net的负载节点编号
  CostType               max_delay = 0;    // net的当前最大延迟
  CostType               slack_ratio;
  CostType               edge_delay;
  // IndexType tdm_num;

  bool                   needsRerouting_ = true;
};
}   // namespace database
#endif