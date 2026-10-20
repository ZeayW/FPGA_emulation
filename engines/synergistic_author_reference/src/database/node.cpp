#include "node.h"

namespace database {

IndexType Node::memory() const { return sizeof(id_) + sizeof(die_id_); }
}   // namespace database