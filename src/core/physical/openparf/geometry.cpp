#include "physical/openparf/geometry.hpp"
#include "storage/schema/openparf_input_schema.hpp"
#include <algorithm>
#include <cmath>
#include <queue>
#include <regex>
#include <set>
#include <tuple>
namespace emuflow::core::openparf_input {
namespace {
int Priority(const std::string &r) {
  return r == "LUT" ? 0 : r == "FF" ? 1 : 2;
}
using Signature = std::vector<std::pair<std::string, I>>;
Signature SignatureOf(const Capacities &resources) {
  Signature s(resources.begin(), resources.end());
  std::sort(s.begin(), s.end(), [](const auto &a, const auto &b) {
    return std::make_pair(Priority(a.first), a.first) <
           std::make_pair(Priority(b.first), b.first);
  });
  return s;
}
struct Profile {
  std::set<std::string> lut, ff, dual;
  I carry = 0;
  std::map<std::string, std::set<std::string>> mux;
  Capacities hard;
};
struct Box {
  I xl = INT64_MAX, yl = INT64_MAX, xh = -1, yh = -1;
  void add(I x, I y) {
    xl = std::min(xl, x);
    yl = std::min(yl, y);
    xh = std::max(xh, x);
    yh = std::max(yh, y);
  }
  bool contains(I x, I y) const {
    return xl <= x && x <= xh && yl <= y && y <= yh;
  }
  A array() const { return {xl, yl, xh, yh}; }
};
} // namespace
Geometry BuildGeometry(PhysicalDevice &device, const Capacities &demand,
                       bool need_mux, bool need_carry, const R &allowed,
                       const volatile std::sig_atomic_t *flag) {
  auto &db = device.database();
  Geometry g;
  std::map<std::string, Profile> profiles;
  std::map<std::pair<I, I>, Tile> grouped;
  std::set<std::pair<I, I>> logic;
  std::map<std::tuple<std::string, std::string, std::string>, I> buckets;
  I selected_count = 0;
  bool all_tile = true;
  std::set<std::string> allowed_slrs, known;
  if (!allowed.null()) {
    for (auto &r : allowed.array())
      Check(allowed_slrs.insert(r.text()).second,
            "allowed SLR list is duplicated");
    Check(!allowed_slrs.empty(), "allowed SLR list is empty");
  }
  std::set<std::string> hard;
  for (auto &d : demand)
    if (Hard(d.first))
      hard.insert(d.first);
  Capacities hard_capacity;
  std::map<std::string, std::set<std::string>> mux_bels{
      {"MUXF7", {"F7MUX_AB", "F7MUX_CD", "F7MUX_EF", "F7MUX_GH"}},
      {"MUXF8", {"F8MUX_BOT", "F8MUX_TOP"}},
      {"MUXF9", {"F9MUX"}}};
  auto sites =
      db.Prepare("SELECT "
                 "s.name,s.type,s.base,t.x,t.y,t.site_index,t.has_tile,s.slr,s."
                 "clock_region,s.region FROM pd_sites s JOIN pd_site_tiles t "
                 "ON t.name=s.name ORDER BY s.name");
  while (sites.Step()) {
    Stop(flag);
    auto name = sites.TypedText(0), type = sites.TypedText(1),
         base = sites.TypedText(2), slr = sites.TypedText(7),
         clock = sites.TypedText(8), region = sites.TypedText(9);
    bool slice = type.rfind("SLICE", 0) == 0;
    auto found = profiles.find(base);
    if (found == profiles.end()) {
      Profile p;
      auto bels = db.Prepare("SELECT bel,cells FROM pd_bels WHERE base=?");
      bels.BindText(1, base);
      while (bels.Step()) {
        auto bel = bels.TypedText(0);
        auto compatible = R::Parse(bels.TypedText(1));
        for (auto &v : compatible.array()) {
          auto t = v.text();
          if (Lut(t) && bel.size() == 5 && bel.substr(1) == "6LUT" &&
              bel[0] >= 'A' && bel[0] <= 'H')
            p.lut.insert(bel);
          if (t == "LUT6_2" && bel.size() == 5 && bel.substr(1) == "6LUT")
            p.dual.insert(bel);
          if (Ff(t) && bel[0] >= 'A' && bel[0] <= 'H' &&
              (bel.substr(1) == "FF" || bel.substr(1) == "FF2"))
            p.ff.insert(bel);
          if (Mux(t))
            p.mux[t].insert(bel);
          if (Hard(t))
            ++p.hard[t];
          if (t == "CARRY8" && bel == "CARRY8")
            ++p.carry;
        }
      }
      found = profiles.emplace(base, std::move(p)).first;
    }
    auto &p = found->second;
    Capacities resources;
    if (slice) {
      Check(p.lut.size() == 8 && p.ff.size() == 16,
            "slice site " + name +
                " does not expose the required 8 6LUT and 16 FF BELs");
      if (need_carry)
        Check(p.carry == 1 && p.dual.size() == 8,
              "slice lacks complete CARRY8/LUT6_2 physical model");
      resources = {{"LUT", 16}, {"FF", 16}};
      if (need_carry)
        resources["CARRY8"] = 1;
      if (need_mux)
        for (auto &m : mux_bels) {
          Check(p.mux[m.first] == m.second,
                "slice does not expose exact " + m.first + " BEL set");
          resources[m.first] = m.second.size();
        }
    } else {
      for (auto &h : hard) {
        I expected = h == "RAMB18E2" ? 2 : 1;
        if (p.hard[h] == expected) {
          resources[h] = expected;
          hard_capacity[h] += expected;
        }
      }
      Check(resources.size() <= 1 ||
                (resources.size() == 2 && resources.count("RAMB18E2") &&
                 resources.count("RAMB36E2")),
            "site is ambiguous for demanded hard resources");
    }
    if (resources.empty())
      continue;
    if (region != "null") {
      Check(slr != "@unspecified-slr" && !slr.empty() && !clock.empty(),
            "physical_region is incomplete");
      known.insert(slr);
    }
    if (!allowed.null() && !allowed_slrs.count(slr))
      continue;
    I x = sites.TypedInteger(3), y = sites.TypedInteger(4),
      site_index = sites.TypedInteger(5);
    all_tile &= sites.TypedInteger(6) == 1;
    auto &tile = grouped[{x, y}];
    if (tile.names.empty()) {
      tile.x = x;
      tile.y = y;
      tile.slr = region == "null" ? "" : slr;
      tile.clock = clock;
    } else
      Check(tile.slr == (region == "null" ? "" : slr) && tile.clock == clock,
            "sites at one physical tile disagree on region");
    if (slice)
      Check(logic.insert({x, y}).second,
            "coincident physical resources: multiple logic sites at one "
            "physical tile");
    for (auto &r : resources) {
      tile.resources[r.first] += r.second;
      tile.names[r.first].push_back({site_index, name});
    }
    if (region != "null")
      ++buckets[{slr, clock, type}];
    ++selected_count;
  }
  if (!allowed.null())
    for (auto &slr : allowed_slrs)
      Check(known.count(slr),
            "allowed SLR list contains unknown region " + slr);
  Check(!logic.empty(), "ArchitectureDB has no slice sites");
  for (auto &h : hard)
    Check(hard_capacity[h] > 0,
          "ArchitectureDB has no unique sites for hard resources " + h);
  std::set<I> lx, ly;
  for (auto p : logic) {
    lx.insert(p.first);
    ly.insert(p.second);
  }
  Check(lx.size() >= 2 && ly.size() >= 2,
        "requires a non-degenerate two-dimensional logic-site region");
  std::map<I, I> xi, yi;
  I i = 0;
  for (auto x : lx)
    xi[x] = i++;
  i = 0;
  for (auto y : ly)
    yi[y] = i++;
  std::set<std::pair<I, I>> occupied, reached;
  for (auto p : logic)
    occupied.insert({xi[p.first], yi[p.second]});
  std::vector<std::pair<I, I>> frontier{*occupied.begin()};
  reached.insert(frontier[0]);
  while (!frontier.empty()) {
    auto p = frontier.back();
    frontier.pop_back();
    for (auto n : {std::make_pair(p.first - 1, p.second),
                   std::make_pair(p.first + 1, p.second),
                   std::make_pair(p.first, p.second - 1),
                   std::make_pair(p.first, p.second + 1)})
      if (occupied.count(n) && reached.insert(n).second)
        frontier.push_back(n);
  }
  Check(reached == occupied,
        "requires one connected logic-site region; disconnected islands");
  g.placement = O{{"logic_sites", occupied.size()},
                  {"dense_width", lx.size()},
                  {"dense_height", ly.size()},
                  {"occupied_fraction",
                   double(occupied.size()) / double(lx.size() * ly.size())},
                  {"available_logic_sites", logic.size()}};
  if (!allowed.null())
    g.placement["allowed_slrs"] = allowed;
  g.headroom = O{{"status", buckets.empty() ? "not-applicable" : "pass"},
                 {"policy", "full-qualified-site-domain"},
                 {"limit", buckets.empty() ? R() : R(1.0)},
                 {"groups", buckets.size()},
                 {"original_sites", selected_count},
                 {"available_sites", selected_count},
                 {"reserved_sites", 0}};
  if (!buckets.empty()) {
    I minimum = INT64_MAX, maximum = 0;
    for (auto &p : buckets) {
      minimum = std::min(minimum, p.second);
      maximum = std::max(maximum, p.second);
    }
    g.headroom["minimum_group_available_sites"] = minimum;
    g.headroom["maximum_group_available_sites"] = maximum;
  }
  std::set<int> directions;
  auto edges =
      db.Prepare("SELECT a.x,a.y,b.x,b.y FROM pd_adjacency e JOIN "
                 "pd_site_tiles a ON a.name=e.source JOIN pd_site_tiles b ON "
                 "b.name=e.target WHERE e.kind='CARRY_NEXT'");
  while (edges.Step()) {
    Check(edges.Integer(0) == edges.Integer(2) &&
              edges.Integer(1) != edges.Integer(3),
          "CARRY_NEXT is not a directed vertical tile edge");
    directions.insert(edges.Integer(3) > edges.Integer(1) ? 1 : -1);
  }
  Check(directions.size() <= 1,
        "CARRY_NEXT edges disagree on physical Y orientation");
  if (directions.count(-1))
    g.order = "descending";
  std::set<I> xs;
  I low = INT64_MAX, high = -1;
  std::map<std::string, I> slr_starts;
  for (auto &p : grouped) {
    xs.insert(p.first.first);
    low = std::min(low, p.first.second);
    high = std::max(high, p.first.second);
    if (!p.second.slr.empty()) {
      auto r = slr_starts.emplace(p.second.slr, p.first.second);
      r.first->second = std::min(r.first->second, p.first.second);
    }
  }
  if (slr_starts.size() > 1) {
    std::vector<I> starts;
    for (auto &p : slr_starts)
      starts.push_back(p.second);
    std::sort(starts.begin(), starts.end());
    std::set<I> pitches;
    for (std::size_t k = 1; k < starts.size(); ++k)
      pitches.insert(starts[k] - starts[k - 1]);
    Check(pitches.size() == 1, "SLR row pitch is inconsistent");
    auto end = low + *pitches.begin() * I(starts.size()) - 1;
    Check(high <= end, "SLR sites exceed row pitch");
    high = end;
  }
  Check(high - low < 1048576 && xs.size() < 1048576,
        "device coordinate extent exceeds bound");
  g.xs.assign(xs.begin(), xs.end());
  for (I y = low; y <= high; ++y)
    g.ys.push_back(y);
  if (g.order == "descending")
    std::reverse(g.ys.begin(), g.ys.end());
  xi.clear();
  yi.clear();
  i = 0;
  for (auto x : g.xs)
    xi[x] = i++;
  i = 0;
  for (auto y : g.ys)
    yi[y] = i++;
  for (auto &p : grouped) {
    auto &t = p.second;
    t.dx = xi[t.x];
    t.dy = yi[t.y];
    for (auto &names : t.names)
      std::sort(names.second.begin(), names.second.end());
    g.tiles.push_back(t);
  }
  std::sort(g.tiles.begin(), g.tiles.end(), [](const auto &a, const auto &b) {
    return std::tie(a.dx, a.dy) < std::tie(b.dx, b.dy);
  });
  std::set<Signature> signatures;
  for (auto &t : g.tiles)
    signatures.insert(SignatureOf(t.resources));
  std::map<Signature, std::string> signature_names;
  i = 0;
  for (auto &sig : signatures) {
    auto name = "EMUFLOW_SITE_" + std::to_string(i++);
    signature_names[sig] = name;
    g.prefix += "SITE " + name + "\n";
    for (auto r : sig)
      g.prefix += "  " + r.first + " " + std::to_string(r.second) + "\n";
    g.prefix += "END SITE\n\n";
  }
  g.suffix = "END RESOURCES\n\nSITEMAP " + std::to_string(g.xs.size()) + " " +
             std::to_string(g.ys.size()) + "\n";
  bool blocked = false;
  for (std::size_t k = 0; k < g.tiles.size(); ++k) {
    auto &t = g.tiles[k];
    I next = k + 1 < g.tiles.size() && g.tiles[k + 1].dx == t.dx
                 ? g.tiles[k + 1].dy
                 : I(g.ys.size());
    I upper =
        t.resources.count("LUT") || t.resources.count("FF") ? t.dy + 1 : next;
    t.px = t.dx + 0.5;
    t.py = (t.dy + upper) * 0.5;
    g.suffix += std::to_string(t.dx) + " " + std::to_string(t.dy) + " " +
                signature_names.at(SignatureOf(t.resources)) + "\n";
    if (upper < next) {
      blocked = true;
      g.suffix += std::to_string(t.dx) + " " + std::to_string(upper) +
                  " EMUFLOW_UNAVAILABLE\n";
    }
  }
  if (blocked)
    g.prefix += "SITE EMUFLOW_UNAVAILABLE\nEND SITE\n\n";
  g.prefix += "RESOURCES\n";
  g.suffix += "END SITEMAP\n";
  if (all_tile)
    g.coordinate_type = "native-device-tile-column-row-v1";
  bool regions = false, missing = false;
  for (auto &t : g.tiles) {
    regions |= !t.slr.empty();
    missing |= t.slr.empty();
  }
  Check(!regions || !missing, "clock-region coverage is partial");
  if (regions) {
    struct Region {
      I sx, sy, x, y;
      std::string name, slr;
      Box observed, box;
    };
    std::map<std::string, Region> by_name;
    std::regex pattern("^X([0-9]+)Y([0-9]+)$");
    std::map<std::string, Box> by_slr;
    for (auto &t : g.tiles) {
      std::smatch match;
      Check(std::regex_match(t.clock, match, pattern),
            "clock region is not canonical");
      Check(std::regex_match(t.slr, std::regex("[A-Za-z_][A-Za-z0-9_\\-]*")),
            "SLR name is not canonical");
      auto r = by_name.emplace(t.clock, Region{std::stoll(match[1]),
                                               std::stoll(match[2]),
                                               0,
                                               0,
                                               t.clock,
                                               t.slr,
                                               {},
                                               {}});
      Check(r.first->second.slr == t.slr, "clock region is inconsistent");
      r.first->second.observed.add(t.dx, t.dy);
      by_slr[t.slr].add(t.dx, t.dy);
    }
    I minx = INT64_MAX, miny = INT64_MAX, width = 0, height = 0;
    for (auto &p : by_name) {
      minx = std::min(minx, p.second.sx);
      miny = std::min(miny, p.second.sy);
    }
    for (auto &p : by_name) {
      auto &r = p.second;
      r.x = r.sx - minx;
      r.y = r.sy - miny;
      width = std::max(width, r.x + 1);
      height = std::max(height, r.y + 1);
    }
    Check(width * height == I(by_name.size()),
          "clock-region grid is incomplete");
    for (int axis = 0; axis < 2; ++axis) {
      std::map<I, std::pair<I, I>> bands;
      for (auto &p : by_name) {
        auto &r = p.second;
        I key = axis ? r.y : r.x, l = axis ? r.observed.yl : r.observed.xl,
          h = axis ? r.observed.yh : r.observed.xh;
        auto v = bands.emplace(key, std::make_pair(l, h));
        v.first->second.first = std::min(v.first->second.first, l);
        v.first->second.second = std::max(v.first->second.second, h);
      }
      std::vector<std::pair<I, std::pair<I, I>>> ordered(bands.begin(),
                                                         bands.end());
      std::sort(ordered.begin(), ordered.end(),
                [](auto a, auto b) { return a.second < b.second; });
      std::map<I, std::pair<I, I>> bounds;
      I start = 0;
      for (std::size_t k = 0; k < ordered.size(); ++k) {
        I end = axis ? I(g.ys.size()) - 1 : I(g.xs.size()) - 1;
        if (k + 1 < ordered.size()) {
          Check(ordered[k].second.second < ordered[k + 1].second.first,
                "clock regions are not rectangular bands");
          end = axis && g.order == "descending"
                    ? ordered[k].second.second
                    : ordered[k + 1].second.first - 1;
        }
        Check(start <= ordered[k].second.first &&
                  end >= ordered[k].second.second,
              "clock region ownership would move a real site");
        bounds[ordered[k].first] = {start, end};
        start = end + 1;
      }
      for (auto &p : by_name) {
        auto &r = p.second;
        auto b = bounds.at(axis ? r.y : r.x);
        if (axis) {
          r.box.yl = b.first;
          r.box.yh = b.second;
        } else {
          r.box.xl = b.first;
          r.box.xh = b.second;
        }
      }
      // Source indices are reassigned in increasing dense geometry.
      for (std::size_t k = 0; k < ordered.size(); ++k)
        for (auto &p : by_name) {
          auto &r = p.second;
          if ((axis ? r.sy - miny : r.sx - minx) == ordered[k].first) {
            if (axis)
              r.y = k;
            else
              r.x = k;
          }
        }
    }
    for (auto &t : g.tiles)
      Check(by_name.at(t.clock).box.contains(t.dx, t.dy),
            "clock region is not rectangular");
    std::vector<Region> sorted;
    for (auto &p : by_name)
      sorted.push_back(p.second);
    std::sort(sorted.begin(), sorted.end(), [](auto &a, auto &b) {
      return std::tie(a.x, a.y) < std::tie(b.x, b.y);
    });
    A contracts;
    g.suffix += "\nCLOCKREGIONS " + std::to_string(width) + " " +
                std::to_string(height) + "\n";
    for (auto &r : sorted) {
      auto name = "X" + std::to_string(r.x) + "Y" + std::to_string(r.y);
      auto &b = r.box;
      g.suffix += "  CLOCKREGION " + name + " : " + std::to_string(b.xl) + " " +
                  std::to_string(b.yl) + " " + std::to_string(b.xh) + " " +
                  std::to_string(b.yh) + " " +
                  std::to_string(b.yl + (b.yh - b.yl + 1) / 2) + " " +
                  std::to_string(b.xl) + "\n";
      R c = O{{"name", name},
              {"x", r.x},
              {"y", r.y},
              {"slr", r.slr},
              {"bbox", b.array()}};
      if (name != r.name)
        c["source_name"] = r.name;
      contracts.push_back(c);
    }
    g.suffix += "END CLOCKREGIONS\n";
    g.clock = O{
        {"width", width},
        {"height", height},
        {"geometry", "complete-dense-grid-empty-seams-no-fabricated-sites-v1"},
        {"maximum_clocks_per_region", 24},
        {"native_enforcement", "disabled"},
        {"native_enforcement_reason",
         "OpenPARF UTPlaceFX cannot safely represent the current "
         "multi-resource UltraScale+ site model or discover its clock sources"},
        {"regions", contracts}};
    for (auto &p : by_slr)
      for (auto &t : g.tiles)
        Check(!p.second.contains(t.dx, t.dy) || p.first == t.slr,
              "SLR is not rectangular");
    std::vector<std::pair<std::string, Box>> slrs(by_slr.begin(), by_slr.end());
    std::sort(slrs.begin(), slrs.end(), [](auto &a, auto &b) {
      return std::tie(a.second.yl, a.first) < std::tie(b.second.yl, b.first);
    });
    if (slrs.size() > 1) {
      I pitch = slrs[1].second.yl - slrs[0].second.yl;
      Check(pitch > 0, "invalid SLR pitch");
      for (std::size_t k = 0; k < slrs.size(); ++k) {
        auto &b = slrs[k].second;
        if (k)
          Check(b.yl - slrs[k - 1].second.yl == pitch,
                "SLR y-axis pitch is inconsistent");
        I l = I(k) * pitch, h = l + pitch - 1;
        Check(b.yl >= l && b.yh <= h, "SLR exceeds its y-axis pitch");
      }
      for (std::size_t k = 0; k < slrs.size(); ++k) {
        slrs[k].second.yl = I(k) * pitch;
        slrs[k].second.yh = I(k + 1) * pitch - 1;
      }
    }
    std::set<std::pair<I, I>> xspans, yspans;
    for (auto &p : slrs) {
      xspans.insert({p.second.xl, p.second.xh});
      yspans.insert({p.second.yl, p.second.yh});
    }
    Check(xspans.size() * yspans.size() == slrs.size(),
          "SLR grid is incomplete");
    auto span = [](auto &spans, I extent) {
      I prev = -1, size = -1;
      for (auto p : spans) {
        Check(p.first == prev + 1, "SLR axis is not contiguous");
        I n = p.second - p.first + 1;
        Check(n > 0 && (size < 0 || n == size),
              "requires equal-size rectangular SLRs");
        size = n;
        prev = p.second;
      }
      Check(prev == extent - 1, "SLR axis does not cover device");
      return size;
    };
    auto sw = span(xspans, g.xs.size()), sh = span(yspans, g.ys.size());
    A regions_out;
    g.suffix += "\nSUPERLOGICREGIONS " + std::to_string(xspans.size()) + " " +
                std::to_string(yspans.size()) + "\n";
    I sx = 0;
    for (auto x : xspans) {
      I sy = 0;
      for (auto y : yspans) {
        auto p = std::find_if(slrs.begin(), slrs.end(), [&](auto &p) {
          auto &b = p.second;
          return b.xl == x.first && b.xh == x.second && b.yl == y.first &&
                 b.yh == y.second;
        });
        Check(p != slrs.end(), "SLR grid is incomplete");
        g.suffix += "  SUPERLOGICREGION " + p->first +
                    " ULTRASCALE_PLUS : " + std::to_string(sx) + " " +
                    std::to_string(sy) + " " + std::to_string(sw) + " " +
                    std::to_string(sh) + "\n";
        regions_out.emplace_back(O{{"name", p->first},
                                   {"x", sx},
                                   {"y", sy++},
                                   {"bbox", p->second.array()}});
      }
      ++sx;
    }
    g.suffix += "END SUPERLOGICREGIONS\n";
    g.slr =
        O{{"width", xspans.size()},
          {"height", yspans.size()},
          {"native_enforcement", slrs.size() > 1 ? "enabled" : "not-required"},
          {"regions", regions_out}};
  }
  R::O density;
  for (auto &d : demand) {
    I unit = 0, total = 0;
    for (auto &t : g.tiles) {
      auto r = t.resources.find(d.first);
      I n = r == t.resources.end() ? 0 : r->second;
      if (d.first == "LUT")
        n = std::min<I>(n, 8);
      total += n;
      unit = std::max(unit, n);
    }
    Check(unit > 0, "no " + d.first + " model capacity");
    double movable = double(d.second) / unit, placeable = double(total) / unit,
           target = 0.75 * placeable;
    Check(movable > 0 && movable < target && total > d.second,
          "density domain has no finite headroom for " + d.first);
    g.units[d.first] = unit;
    density[d.first] = O{{"movable_area", movable},
                         {"placeable_area", placeable},
                         {"target_area", target},
                         {"filler_units", total - d.second}};
  }
  g.density = density;
  return g;
}
R WriteSiteStore(const std::filesystem::path &path, const Geometry &g,
                 const std::string &sha) {
  {
    SqliteDatabase db(path.string(), SqliteDatabase::Mode::kCreateExclusive);
    db.Execute(kOpenparfSitesDDL);
    db.Execute("BEGIN");
    auto meta = db.Prepare("INSERT INTO metadata VALUES(?,?)");
    for (auto p : std::map<std::string, std::string>{
             {"schema", "emuflow.openparf-atomic-site-database/v1"},
             {"site_count", std::to_string(g.tiles.size())},
             {"architecture_sha256", sha}}) {
      meta.BindText(1, p.first);
      meta.BindText(2, p.second);
      meta.Execute();
      meta.Reset();
    }
    if (!g.coordinate_type.empty()) {
      meta.BindText(1, "coordinate_system");
      meta.BindText(2, g.coordinate_type);
      meta.Execute();
      meta.Reset();
    }
    for (auto &t : g.tiles) {
      std::set<std::string> names;
      for (auto &p : t.names)
        for (auto &n : p.second)
          names.insert(n.second);
      auto site = db.Prepare("INSERT INTO sites VALUES(?,?,?,?,?,?,?)");
      site.BindInteger(1, t.dx);
      site.BindInteger(2, t.dy);
      site.BindInteger(3, t.x);
      site.BindInteger(4, t.y);
      site.BindReal(5, t.px);
      site.BindReal(6, t.py);
      site.BindText(7, *names.begin());
      site.Execute();
      for (auto &p : t.resources) {
        auto r = db.Prepare("INSERT INTO resources VALUES(?,?,?,?)");
        r.BindInteger(1, t.dx);
        r.BindInteger(2, t.dy);
        r.BindText(3, p.first);
        r.BindInteger(4, p.second);
        r.Execute();
      }
      for (auto &p : t.names) {
        I slot = 0;
        for (auto &n : p.second) {
          auto r = db.Prepare("INSERT INTO physical_sites VALUES(?,?,?,?,?)");
          r.BindText(1, p.first);
          r.BindText(2, n.second);
          r.BindInteger(3, t.dx);
          r.BindInteger(4, t.dy);
          r.BindInteger(5, slot++);
          r.Execute();
        }
      }
    }
    db.Execute("COMMIT");
  }
  auto ref = ArtifactRef::Bind(path.string(), "openparf-site-store-v1");
  return O{{"schema", "emuflow.openparf-atomic-site-database/v1"},
           {"file", path.filename().string()},
           {"sites", g.tiles.size()},
           {"sha256", FileHash(ref)}};
}
std::map<std::string, std::vector<R>> BramCandidates(SqliteDatabase &db,
                                                     const Geometry &g) {
  std::map<std::string, std::vector<R>> result{{"RAMB18E2", {}},
                                               {"RAMB36E2", {}}};
  for (auto &t : g.tiles)
    for (auto &resource : {"RAMB18E2", "RAMB36E2"}) {
      auto names = t.names.find(resource);
      if (names == t.names.end())
        continue;
      for (auto &n : names->second) {
        auto rows = db.Prepare(
            "SELECT b.role,b.bel,b.site,b.site_type,b.tile FROM pd_bram b "
            "WHERE b.anchor=? AND ((?='RAMB36E2' AND b.role='whole') OR "
            "(?='RAMB18E2' AND b.role IN ('lower','upper'))) ORDER BY b.role");
        rows.BindText(1, n.second);
        rows.BindText(2, resource);
        rows.BindText(3, resource);
        I count = 0;
        while (rows.Step()) {
          auto role = rows.TypedText(0), tile = rows.TypedText(4);
          A claims;
          if (role == "whole")
            claims = {"bram:" + tile + ":lower", "bram:" + tile + ":upper"};
          else
            claims = {"bram:" + tile + ":" + role};
          result[resource].emplace_back(O{{"anchor", n.second},
                                          {"bel", rows.TypedText(1)},
                                          {"claims", claims},
                                          {"placement_mode", rows.TypedText(3)},
                                          {"resource", resource},
                                          {"role", role},
                                          {"site", rows.TypedText(2)},
                                          {"tile", tile},
                                          {"x", t.px},
                                          {"y", t.py},
                                          {"z", role == "upper" ? 1 : 0}});
          ++count;
        }
        Check(count == (std::string(resource) == "RAMB18E2" ? 2 : 1),
              "native BRAM candidates do not cover anchors");
      }
    }
  for (auto &p : result)
    std::sort(p.second.begin(), p.second.end(), [](auto &a, auto &b) {
      return std::make_tuple(a.at("anchor").text(), a.at("z").integer(),
                             a.at("site").text()) <
             std::make_tuple(b.at("anchor").text(), b.at("z").integer(),
                             b.at("site").text());
    });
  return result;
}
} // namespace emuflow::core::openparf_input
