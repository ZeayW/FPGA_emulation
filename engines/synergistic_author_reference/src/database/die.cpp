#include "die.h"

namespace database {

IndexType Die::memory() const { return sizeof(id_) + sizeof(fpga_id_); }
}   // namespace database