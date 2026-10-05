#include "custom_data/chain_info/src/chain_info.h"

#include <fstream>
#include <limits>
#include <queue>
#include <string>
#include <vector>

OPENPARF_BEGIN_NAMESPACE

using database::Design;
using database::Inst;
using database::Layout;
using database::Model;
using database::Net;
using database::Pin;
using database::PlaceDB;

namespace chain_info {

ChainInfoVec MakeChainInfoVecFromPlaceDB(const PlaceDB &placedb) {
  ChainInfoVec       rv;
  const auto         db              = placedb.db();
  const Design &     design          = db->design();
  const Layout &     layout          = db->layout();
  auto               top_module_inst = design.topModuleInst();
  auto const &       netlist         = top_module_inst->netlist();
  const auto &       place_params    = placedb.place_params();
  const std::string &module_name     = place_params.carry_chain_module_name_;
  int32_t            module_id       = design.modelId(module_name);

  openparfAssert(module_id != std::numeric_limits<decltype(module_id)>::max());

  int32_t           num_insts = netlist.numInsts();
  int32_t           count     = 0;
  std::vector<bool> visited_mark(num_insts, false);
  auto              IsVisited = [&visited_mark](const Inst &inst) -> bool { return visited_mark[inst.id()] == true; };
  auto              IsChain   = [&module_id](const Inst &inst) -> bool { return inst.attr().modelId() == module_id; };

  openparfPrint(kDebug, "carry_chain_module_name: %s(%i)\n", module_name.c_str(), module_id);

  for (int32_t new_inst_id = 0; new_inst_id < num_insts; new_inst_id++) {
    int32_t     old_inst_id = placedb.oldInstId(new_inst_id);
    const Inst &inst        = netlist.inst(old_inst_id);
    if (!IsChain(inst) || IsVisited(inst)) continue;
    rv.push_back(detail::ExtractChainFromOneInst(placedb, module_id, inst, visited_mark));
  }

  if (false) {
    std::ofstream of("chain_info.txt");
    for (const auto &chain_info : rv) {
      of << "===" << std::endl;
      for (const auto &new_id : chain_info.cla_new_ids()) {
        int32_t     old_id = placedb.oldInstId(new_id);
        const auto &inst   = netlist.inst(old_id);
        of << inst.attr().name() << "(" << new_id << ") ";
      }
      of << std::endl;
      for (const auto &new_id : chain_info.lut_new_ids()) {
        int32_t     old_id = placedb.oldInstId(new_id);
        const auto &inst   = netlist.inst(old_id);
        of << inst.attr().name() << "(" << new_id << ") ";
      }
      of << std::endl;
    }
    of.close();
  }
  return rv;
}

std::tuple<FlatNestedVectorInt, FlatNestedVectorInt> MakeNestedNewIdx(const ChainInfoVec &chain_info_vec) {
  FlatNestedVectorInt nested_cla_ids, nested_lut_ids;
  {
    int32_t count = 0;
    for (auto const &chain_info : chain_info_vec) {
      const auto &new_ids = chain_info.cla_new_ids();
      nested_cla_ids.pushBackIndexBegin(count);
      for (auto const &new_id : new_ids) {
        nested_cla_ids.pushBack(new_id);
      }
      count += new_ids.size();
    }
  }
  {
    int32_t count = 0;
    for (auto const &chain_info : chain_info_vec) {
      const auto &new_ids = chain_info.lut_new_ids();
      nested_lut_ids.pushBackIndexBegin(count);
      for (auto const &new_id : new_ids) {
        nested_lut_ids.pushBack(new_id);
      }
      count += new_ids.size();
    }
  }
  return {nested_cla_ids, nested_lut_ids};
}

namespace detail {

ChainInfo ExtractChainFromOneInst(const PlaceDB &    placedb,
                                  int32_t            module_id,
                                  const Inst &       source_inst,
                                  std::vector<bool> &visited_mark) {
  const Design &       design          = placedb.db()->design();
  const Layout &       layout          = placedb.db()->layout();
  auto                 top_module_inst = design.topModuleInst();
  auto const &         netlist         = top_module_inst->netlist();
  const Model &        model           = design.model(module_id);
  ChainInfo            chain;
  auto &               ordinal_cla_ids = chain.cla_ids();
  auto &               ordinal_lut_ids = chain.lut_ids();
  auto &               new_cla_ids     = chain.cla_new_ids();
  auto &               new_lut_ids     = chain.lut_new_ids();
  std::vector<int32_t> reversed_inst_ids;
  std::queue<int32_t>  que;
  auto IsVisited       = [&visited_mark](const Inst &inst) -> bool { return visited_mark[inst.id()] == true; };
  auto IsChain         = [&module_id](const Inst &inst) -> bool { return inst.attr().modelId() == module_id; };
  auto SetVisited      = [&visited_mark](const Inst &inst) { visited_mark[inst.id()] = true; };
  auto IsCascadedInPin = [&model](const Pin &pin) {
    const auto &model_pin = model.modelPin(pin.modelPinId());
    return model_pin.signalDirect() == SignalDirection::kInput && model_pin.signalType() == SignalType::kCascade;
  };
  auto IsCascadedOutPin = [&model](const Pin &pin) {
    const auto &model_pin = model.modelPin(pin.modelPinId());
    return model_pin.signalDirect() == SignalDirection::kOutput && model_pin.signalType() == SignalType::kCascade;
  };
  const bool is_carry8 = model.name() == "CARRY8";
  const int32_t associated_luts_per_unit = is_carry8 ? 8 : 4;
  auto AssociatedLutPinId = [&model, is_carry8](const Pin &pin) {
    // XArch CLA4 associates PROP[0:3].  UltraScale+ CARRY8 associates
    // one LUT6_2 with each S[0:7]; DI[0:7] is checked separately below.
    const auto &       model_pin = model.modelPin(pin.modelPinId());
    const std::string &name      = model_pin.name();
    const std::string  prefix    = is_carry8 ? "S" : "PROP";
    if (name.rfind(prefix, 0) != 0) {
      return -1;
    }
    int32_t lb = name.find("[");
    int32_t rb = name.find("]");
    return std::stoi(name.substr(lb + 1, rb - lb - 1));
  };

  SetVisited(source_inst);
  openparfAssert(IsChain(source_inst));

  /* only search from cascaded output pin to cascaded input pin */ {
    ordinal_cla_ids.clear();
    que.push(source_inst.id());
    ordinal_cla_ids.push_back(source_inst.id());
    while (!que.empty()) {
      auto        inst_id = que.front();
      const Inst &inst    = netlist.inst(inst_id);
      que.pop();
      for (auto pin_id : inst.pinIds()) {
        const Pin &pin = netlist.pin(pin_id);
        if (!IsCascadedOutPin(pin)) continue;
        const Net &net = netlist.net(pin.netId());
        for (auto adjacent_pin_id : net.pinIds()) {
          const Pin & adjacent_pin     = netlist.pin(adjacent_pin_id);
          auto        adjacent_inst_id = adjacent_pin.instId();
          const Inst &adjacent_inst    = netlist.inst(adjacent_inst_id);
          if (!IsChain(adjacent_inst) || IsVisited(adjacent_inst) || !IsCascadedInPin(adjacent_pin)) {
            continue;
          }
          SetVisited(adjacent_inst);
          que.push(adjacent_inst_id);
          ordinal_cla_ids.push_back(adjacent_inst_id);
        }
      }
    }
  }

  /* only search form cascaded input pin to cascaded output pin */ {
    reversed_inst_ids.clear();
    que.push(source_inst.id());
    while (!que.empty()) {
      auto        inst_id = que.front();
      const Inst &inst    = netlist.inst(inst_id);
      que.pop();
      for (auto pin_id : inst.pinIds()) {
        const Pin &pin = netlist.pin(pin_id);
        if (!IsCascadedInPin(pin)) continue;
        const Net &net = netlist.net(pin.netId());
        for (auto adjacent_pin_id : net.pinIds()) {
          const Pin & adjacent_pin     = netlist.pin(adjacent_pin_id);
          auto        adjacent_inst_id = adjacent_pin.instId();
          const Inst &adjacent_inst    = netlist.inst(adjacent_inst_id);
          if (!IsChain(adjacent_inst) || IsVisited(adjacent_inst) || !IsCascadedOutPin(adjacent_pin)) {
            continue;
          }
          SetVisited(adjacent_inst);
          que.push(adjacent_inst_id);
          reversed_inst_ids.push_back(adjacent_inst_id);
        }
      }
    }
  }

  // combine the instances in two directions.
  std::reverse(reversed_inst_ids.begin(), reversed_inst_ids.end());
  reversed_inst_ids.insert(reversed_inst_ids.end(), ordinal_cla_ids.begin(), ordinal_cla_ids.end());
  ordinal_cla_ids = reversed_inst_ids;

  // get the associated lut ids
  ordinal_lut_ids.clear();
  for (int32_t cla_id : ordinal_cla_ids) {
    const Inst &inst         = netlist.inst(cla_id);
    int32_t     current_size = ordinal_lut_ids.size();
    ordinal_lut_ids.resize(current_size + associated_luts_per_unit, -1);
    for (auto pin_id : inst.pinIds()) {
      const Pin &pin     = netlist.pin(pin_id);
      const Net &net     = netlist.net(pin.netId());
      int32_t    lut_slot = AssociatedLutPinId(pin);
      if (lut_slot < 0) {
        continue;
      }
      openparfAssert(0 <= lut_slot && lut_slot < associated_luts_per_unit);
      openparfAssert(net.pinIds().size() == 2);
      for (auto adjacent_pin_id : net.pinIds()) {
        const Pin & adjacent_pin     = netlist.pin(adjacent_pin_id);
        auto        adjacent_inst_id = adjacent_pin.instId();
        const Inst &adjacent_inst    = netlist.inst(adjacent_inst_id);
        if (IsChain(adjacent_inst)) {
          continue;
        }
        openparfAssert(ordinal_lut_ids[current_size + lut_slot] == -1);
        SetVisited(adjacent_inst);
        ordinal_lut_ids[current_size + lut_slot] = adjacent_inst_id;
      }
    }
    for (int32_t lut_slot = 0; lut_slot < associated_luts_per_unit;
         ++lut_slot) {
      openparfAssert(ordinal_lut_ids[current_size + lut_slot] >= 0);
    }
    if (is_carry8) {
      // Each normalized CARRY8 bit must receive DI from O5 and S from O6 of
      // the same LUT6_2.  The Bookshelf library does not encode dual-output
      // ownership, so prove it from the two independent mapped nets here.
      for (auto pin_id : inst.pinIds()) {
        const Pin &pin = netlist.pin(pin_id);
        const auto &model_pin = model.modelPin(pin.modelPinId());
        const std::string &name = model_pin.name();
        if (name.rfind("DI", 0) != 0) continue;
        int32_t lb = name.find("[");
        int32_t rb = name.find("]");
        int32_t lut_slot = std::stoi(name.substr(lb + 1, rb - lb - 1));
        openparfAssert(0 <= lut_slot && lut_slot < 8);
        const Net &net = netlist.net(pin.netId());
        openparfAssert(net.pinIds().size() == 2);
        int32_t adjacent_lut_id = -1;
        for (auto adjacent_pin_id : net.pinIds()) {
          const Pin &adjacent_pin = netlist.pin(adjacent_pin_id);
          if (adjacent_pin.instId() != inst.id()) {
            adjacent_lut_id = adjacent_pin.instId();
          }
        }
        openparfAssert(
            adjacent_lut_id == ordinal_lut_ids[current_size + lut_slot]);
      }
    }
  }

  // transfer the old ids to the new ids
  new_cla_ids.clear();
  new_lut_ids.clear();
  for (auto const &old_id : ordinal_cla_ids) {
    auto new_inst_id = placedb.newInstId(old_id);
    new_cla_ids.push_back(new_inst_id);
  }
  for (auto const &old_id : ordinal_lut_ids) {
    auto new_inst_id = placedb.newInstId(old_id);
    new_lut_ids.push_back(new_inst_id);
  }
  return chain;
}
}   // namespace detail

}   // namespace chain_info

OPENPARF_END_NAMESPACE
