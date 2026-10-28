#include "physical/openparf/placement_records.hpp"
#include <algorithm>
#include <optional>
#include <tuple>
namespace emuflow::core::openparf_placement {
namespace {
std::string ReprString(const std::string &s) {
  const char quote =
      s.find('\'') != std::string::npos && s.find('"') == std::string::npos
          ? '"'
          : '\'';
  std::string out(1, quote);
  constexpr char hex[] = "0123456789abcdef";
  for (unsigned char c : s) {
    if (c == '\\' || c == quote) {
      out += '\\';
      out += c;
    } else if (c == '\n')
      out += "\\n";
    else if (c == '\r')
      out += "\\r";
    else if (c == '\t')
      out += "\\t";
    else if (c < 32 || c == 127) {
      out += "\\x";
      out += hex[c >> 4];
      out += hex[c & 15];
    } else
      out += c;
  }
  return out + quote;
}
std::string Bit(MappedPhysicalSemantics &mapped, const std::string &name,
                const std::string &port) {
  const auto bits = mapped.Bits(name, port);
  Check(bits.size() <= 1, "FF control pin is not scalar");
  if (bits.empty())
    return "None";
  if (auto n = std::get_if<I>(&bits.front()))
    return std::to_string(*n);
  return ReprString(std::get<std::string>(bits.front()));
}
struct Control {
  std::string signature, enable;
};
Control Controls(MappedPhysicalSemantics &mapped, const std::string &name,
                 const std::string &type) {
  Check(Ff(type) && mapped.CellType(name) == type,
        "FF control source type disagrees");
  const std::string port = type == "FDCE"   ? "CLR"
                           : type == "FDPE" ? "PRE"
                           : type == "FDRE" ? "R"
                                            : "S";
  const auto parameters = R::Parse(mapped.Parameters(name));
  std::string kept;
  I count = 0;
  for (const auto &p : std::get<O>(parameters.value))
    if (p.first.rfind("IS_", 0) == 0) {
      if (count++)
        kept += ", ";
      kept += "(" + ReprString(p.first) + ", " +
              ReprString(Record(p.second).Encode()) + ")";
    }
  if (count == 1)
    kept += ",";
  return {"(" + Bit(mapped, name, "C") + ", (" + ReprString(type) + ", " +
              ReprString(port) + ", " + Bit(mapped, name, port) + ", (" + kept +
              ")))",
          Bit(mapped, name, "CE")};
}
struct State {
  std::optional<std::string> signature;
  std::array<std::optional<std::string>, 2> enable;
  std::uint16_t occupied = 0;
};
using Key = std::tuple<I, I, I>;
using Coordinate = std::pair<I, I>;
struct Ff {
  std::string name, instance, type;
  I x, y, z;
  Control control;
};
} // namespace
R LegalizeFf(SqliteDatabase &db, MappedPhysicalSemantics &mapped,
             const volatile std::sig_atomic_t *flag) {
  std::set<Coordinate> sites, exclusive;
  std::map<Key, State> states;
  std::vector<Ff> moved;
  auto site = db.Prepare(
      "SELECT r.dense_x,r.dense_y FROM site.resources r WHERE r.resource='FF' "
      "AND r.capacity>=16 AND (SELECT count(*) FROM site.physical_sites p "
      "WHERE p.dense_x=r.dense_x AND p.dense_y=r.dense_y AND "
      "p.resource='FF')=1 ORDER BY r.dense_x,r.dense_y");
  while (site.Step())
    sites.emplace(site.TypedInteger(0), site.TypedInteger(1));
  Check(!sites.empty(), "site database has no complete FF sites");
  auto carry = db.Prepare("SELECT p.x,p.y FROM raw p JOIN names.atoms a "
                          "USING(openparf) WHERE a.resource='CARRY8'");
  while (carry.Step())
    exclusive.emplace(carry.TypedInteger(0), carry.TypedInteger(1));
  auto rows =
      db.Prepare("SELECT a.openparf,a.instance,a.cell_type,p.x,p.y,p.z FROM "
                 "raw p JOIN names.atoms a USING(openparf) WHERE "
                 "a.resource='FF' ORDER BY p.x,p.y,(p.z>=8),a.openparf");
  std::vector<Ff> half;
  I ff_count = 0, illegal = 0;
  auto flush = [&] {
    if (half.empty())
      return;
    std::map<std::string, std::array<std::map<std::string, I>, 2>> candidates;
    for (const auto &ff : half)
      ++candidates[ff.control.signature][ff.z % 2][ff.control.enable];
    std::string selected;
    I best = -1;
    for (const auto &c : candidates) {
      I score = 0;
      for (const auto &lane : c.second) {
        I most = 0;
        for (const auto &n : lane)
          most = std::max(most, n.second);
        score += most;
      }
      if (score > best) {
        best = score;
        selected = c.first;
      }
    }
    State state;
    state.signature = selected;
    for (unsigned lane = 0; lane < 2; ++lane) {
      I most = -1;
      for (const auto &n : candidates.at(selected)[lane])
        if (n.second > most) {
          most = n.second;
          state.enable[lane] = n.first;
        }
    }
    bool changed = false;
    for (const auto &ff : half) {
      if (ff.control.signature == selected &&
          state.enable[ff.z % 2] == ff.control.enable)
        state.occupied |= 1U << ff.z;
      else {
        moved.push_back(ff);
        changed = true;
      }
    }
    illegal += changed;
    states.emplace(Key{half.front().x, half.front().y, half.front().z / 8},
                   std::move(state));
    half.clear();
  };
  while (rows.Step()) {
    Stop(flag);
    Ff ff{rows.TypedText(0),
          rows.TypedText(1),
          rows.TypedText(2),
          rows.TypedInteger(3),
          rows.TypedInteger(4),
          rows.TypedInteger(5),
          {}};
    Check(sites.count({ff.x, ff.y}) && ff.z >= 0 && ff.z < 16,
          "FF placement uses an invalid slot");
    if (!half.empty() &&
        Key{ff.x, ff.y, ff.z / 8} !=
            Key{half.front().x, half.front().y, half.front().z / 8})
      flush();
    ff.control = Controls(mapped, ff.instance, ff.type);
    half.push_back(std::move(ff));
    ++ff_count;
  }
  flush();
  std::sort(moved.begin(), moved.end(), [](const auto &a, const auto &b) {
    return std::tie(a.x, a.y, a.name) < std::tie(b.x, b.y, b.name);
  });
  I minx = INT64_MAX, maxx = INT64_MIN, miny = INT64_MAX, maxy = INT64_MIN;
  for (const auto &s : sites) {
    minx = std::min(minx, s.first);
    maxx = std::max(maxx, s.first);
    miny = std::min(miny, s.second);
    maxy = std::max(maxy, s.second);
  }
  I distance = 0, maximum = 0;
  auto update = db.Prepare("UPDATE raw SET x=?,y=?,z=? WHERE openparf=?");
  for (const auto &ff : moved) {
    Stop(flag);
    bool accepted = false;
    const auto radius_max =
        std::max(std::abs(ff.x - minx), std::abs(ff.x - maxx)) +
        std::max(std::abs(ff.y - miny), std::abs(ff.y - maxy));
    for (I radius = 0; radius <= radius_max && !accepted; ++radius) {
      Stop(flag);
      for (I dy = -radius; dy <= radius && !accepted; ++dy) {
        const auto dx = radius - std::abs(dy);
        const std::vector<I> xs =
            dx ? std::vector<I>{ff.x - dx, ff.x + dx} : std::vector<I>{ff.x};
        for (I x : xs) {
          const auto y = ff.y + dy;
          if (!sites.count({x, y}) || exclusive.count({x, y}))
            continue;
          for (I h : {ff.z / 8, 1 - ff.z / 8}) {
            auto &state = states[Key{x, y, h}];
            if (state.signature && *state.signature != ff.control.signature)
              continue;
            for (I lane : {ff.z % 2, 1 - ff.z % 2}) {
              if (state.enable[lane] && *state.enable[lane] != "None" &&
                  *state.enable[lane] != ff.control.enable)
                continue;
              for (I offset = 0; offset < 4; ++offset) {
                const auto z = h * 8 + lane + 2 * offset;
                if (state.occupied & (1U << z))
                  continue;
                state.signature = ff.control.signature;
                state.enable[lane] = ff.control.enable;
                state.occupied |= 1U << z;
                update.Reset();
                update.BindInteger(1, x);
                update.BindInteger(2, y);
                update.BindInteger(3, z);
                update.BindText(4, ff.name);
                update.Execute();
                accepted = true;
                distance += radius;
                maximum = std::max(maximum, radius);
                break;
              }
              if (accepted)
                break;
            }
            if (accepted)
              break;
          }
          if (accepted)
            break;
        }
      }
    }
    Check(accepted,
          "UltraScale+ FF control-set legalization exhausted all slice sites");
  }
  return O{{"schema", "emuflow.openparf-ff-control-legalization/v1"},
           {"status", "pass"},
           {"provider", "nearest-compatible-ultrascaleplus-half-slice-v1"},
           {"ff_atoms", ff_count},
           {"illegal_halves", illegal},
           {"moved_ff_atoms", moved.size()},
           {"total_manhattan_distance", distance},
           {"maximum_manhattan_distance", maximum}};
}
} // namespace emuflow::core::openparf_placement
