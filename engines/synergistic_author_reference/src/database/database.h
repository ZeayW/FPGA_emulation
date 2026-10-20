#ifndef DATABASE_H_
#define DATABASE_H_

#include "../utils.h"
#include "die.h"
#include "net.h"
#include "node.h"
#include "wire.h"
// #include "wire.h"

namespace database {
struct RouteParameters {
  ParameterType max_iteration = 50;
  // CostType history_n = 1;
};
class Database {
 public:
  Database() {}
  ~Database() {}
  RouteParameters                     &params() { return params_; }

  std::vector<Net>                    &netlist() { return net_; }
  std::vector<Node>                   &nodes() { return nodes_; }
  std::vector<Die>                    &dies() { return dies_; }
  std::vector<std::vector<IndexType> > &networks() { return line_num; }
  std::vector<std::vector<Wire> >      &wires() { return wires_; }
  // std::vector<std::vector<Wire>> &wires() { return wires_;}

  Net                                 &net(IndexType id) { return net_[id]; }
  Node                                &node(IndexType id) { return nodes_[id]; }
  Die                                 &die(IndexType id) { return dies_[id]; }
  IndexType                           &network(IndexType id0, IndexType id1) { return line_num[id0][id1]; }
  // Wire &wire(IndexType x,IndexType y) { return wires_[x][y]; }

  void                                 setnetworks(std::vector<std::vector<IndexType> > martix) { line_num = martix; }

  static bool                          netCompareDelay(Net const &a, Net const& b) { return a.netMaxDelay() > b.netMaxDelay(); }

  IndexType                            memory() const;

 protected:
  RouteParameters                     params_;
  std::vector<Net>                    net_;     // net
  std::vector<Node>                   nodes_;   // node
  std::vector<Die>                    dies_;    // die
  std::vector<std::vector<Wire> >      wires_;
  // std::vector<std::vector<Wire>> wires_;

  std::vector<std::vector<IndexType> > line_num;   // die之间的连接关系
};
}   // namespace database
#endif
