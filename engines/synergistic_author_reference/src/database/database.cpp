#include "database.h"

namespace database {

IndexType Database::memory() const {
  IndexType result = sizeof(net_) + sizeof(nodes_) + sizeof(dies_) + sizeof(line_num);
  for (auto& net : net_) result += net.memory();
  for (auto& node : nodes_) result += node.memory();
  for (auto& die : dies_) result += die.memory();

  return result;
}
}   // namespace database