#ifndef DATABASE_WIRE_H_
#define DATABASE_WIRE_H_
#include "../utils.h"
namespace database {
class Wire {
 public:
  Wire() {}
  Wire(IndexType id) : id_(id) {}
  ~Wire() {}

  IndexType const              &ratio() const { return ratio_; }
  std::vector<IndexType> const &netList() const { return net_id_list; }
  std::vector<CostType> const  &NetDelayList() const { return net_delay_list; }

  void                          setRatio(IndexType &a) { ratio_ = a; }
  void                          setNetList(IndexType netid) { net_id_list.push_back(netid); }
  void                          setNetDelayList(CostType a) { net_delay_list.push_back(a); }
  void                          setNetDelayList_(IndexType id, CostType a) { net_delay_list[id] = a; }
  void                          setWireMaxDelay(CostType v) { wire_max_delay_ = v; }
  CostType const               &wireMaxDelay() const { return wire_max_delay_; }

 protected:
  IndexType              id_;
  IndexType              ratio_ = 4;
  std::vector<IndexType> net_id_list;
  std::vector<CostType>  net_delay_list;
  CostType               wire_max_delay_ = 0;
};
}   // namespace database
#endif