#ifndef DATABASE_DIE_H_
#define DATABASE_DIE_H_

#include "../utils.h"

namespace database {

class Die {
 public:
  Die() {}
  Die(IndexType id) : id_(id) {}
  ~Die() {}

  IndexType const   &id() const { return id_; }

  std::string const &name() const { return name_; }
  void               setname(std::string const &name) { name_ = name; }

  IndexType const   &fpgaId() const { return fpga_id_; }
  void               setfpgaId(IndexType const &fpga_id) { fpga_id_ = fpga_id; }

  IndexType const   &cap() const { return cap_; }
  void               setCap(IndexType const &v) { cap_ = v; }

  IndexType          memory() const;

 protected:
  IndexType   id_;        // die的编号
  std::string name_;      // die的名字
  IndexType   fpga_id_;   // die所在的fpga的编号
  IndexType   cap_;
};
}   // namespace database
#endif