#include "net.h"

namespace database {

IndexType Net::memory() const {
  IndexType sum = sizeof(id_) + sizeof(net_node_ids_s) + sizeof(net_node_ids_l);
  return sum;
}

}   // namespace database