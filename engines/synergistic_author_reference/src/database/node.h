#ifndef DATABASE_NODE_H_
#define DATABASE_NODE_H_

#include "../utils.h"

namespace database {

class Node {
 public:
  Node() {}
  Node(IndexType id) : id_(id) {}
  ~Node() {}

  IndexType const &id() const { return id_; }

  IndexType const &DieId() const { return die_id_; }
  void             setDieId(IndexType const &die_id) { die_id_ = die_id; }

  IndexType        memory() const;

 protected:
  IndexType id_;       // node的编号
  IndexType die_id_;   // node所在的die的编号
};
}   // namespace database

#endif
