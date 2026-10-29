#include "storage/store.hpp"
#include "transport/chimew/bank_store.hpp"
#include <functional>
#include <limits>
#include <stdexcept>
using namespace emuflow::core;
using namespace emuflow::native;
namespace {
void Reject(const std::function<void()> &f) {
  try {
    f();
  } catch (const StageError &) {
    return;
  }
  throw std::runtime_error("invalid bank certificate/schema was accepted");
}
ChimewBankInput Model(int n) {
  ChimewBankInput in;
  in.cost_scale = 1000;
  ChimewBankPair b;
  b.index = 0;
  b.domain = 0;
  b.bank_a = {0, 0};
  b.bank_b = {100, 0};
  for (int c = 0; c < 3; ++c) {
    b.channels.push_back(c);
    in.channels.push_back({c, 0, c, {0, 0}, {100, 0}});
  }
  in.banks.push_back(b);
  for (int i = 0; i < n; ++i) {
    ChimewBankGroup g;
    g.index = i;
    g.domain = 0;
    g.kind = 0;
    g.direction = i % 2;
    g.expected_members = 1;
    ChimewBankMember m;
    m.direction = g.direction;
    m.fanout = g.direction ? ChimewPoint{100, 0} : ChimewPoint{0, 0};
    m.fanins.push_back(g.direction ? ChimewPoint{0, 0} : ChimewPoint{100, 0});
    g.members.push_back(m);
    in.groups.push_back(g);
  }
  return in;
}
} // namespace
int main() {
  for (int n : {1, 2}) {
    const auto model = Model(n);
    const auto good = AssignChimewBankRecords(model);
    ValidateChimewBankSolution(model, good);
    auto changed = good;
    changed.stage1.cost_for_left[0]++;
    Reject([&] { ValidateChimewBankSolution(model, changed); });
    changed = good;
    changed.chosen_priority[0] = 1;
    Reject([&] { ValidateChimewBankSolution(model, changed); });
    changed = good;
    changed.alternatives[0].second.assignment.total_cost++;
    Reject([&] { ValidateChimewBankSolution(model, changed); });
    changed = good;
    for (std::size_t i = 1; i < changed.stage1.potentials.size(); ++i)
      changed.stage1.potentials[i] = 1;
    Reject([&] { ValidateChimewBankSolution(model, changed); });
    changed = good;
    changed.stage1.potentials[1] = std::numeric_limits<std::int64_t>::max();
    changed.stage1.potentials[2] = std::numeric_limits<std::int64_t>::min();
    Reject([&] { ValidateChimewBankSolution(model, changed); });
    auto invalid = model;
    invalid.groups[0].direction = 3;
    Reject([&] { ValidateChimewBankSolution(invalid, good); });
    invalid = model;
    invalid.channels[0].bank = 8;
    Reject([&] { ValidateChimewBankSolution(invalid, good); });
    invalid = model;
    invalid.groups[0].members[0].fanins.clear();
    Reject([&] { ValidateChimewBankSolution(invalid, good); });
    if (n == 2) {
      changed = good;
      auto &a = changed.alternatives[0].first.assignment;
      a.right_for_left[1] = a.right_for_left[0];
      Reject([&] { ValidateChimewBankSolution(model, changed); });
    }
  }
  const auto ddl =
      "CREATE TABLE facts(value TEXT NOT NULL CHECK(value <> 'a  b'))";
  SqliteDatabase db(":memory:", SqliteDatabase::Mode::kCreateExclusive);
  db.Execute(
      "CREATE TABLE facts ( value TEXT NOT NULL CHECK ( value <> 'a  b' ) )");
  RequireSqliteDDL(db, ddl);
  Reject([&] {
    RequireSqliteDDL(
        db, "CREATE TABLE facts(value TEXT NOT NULL CHECK(value <> 'a b'))");
  });
  Reject([&] {
    RequireSqliteDDL(db,
                     "CREATE TABLE facts(value TEXT CHECK(value <> 'a  b'))");
  });
}
