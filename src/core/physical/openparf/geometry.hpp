#pragma once
#include "physical/openparf/input_records.hpp"
namespace emuflow::core::openparf_input {
using Capacities = std::map<std::string, I>;
struct Tile {
  I x = 0, y = 0, dx = 0, dy = 0;
  double px = 0, py = 0;
  Capacities resources;
  std::map<std::string, std::vector<std::pair<I, std::string>>> names;
  std::string slr, clock;
};
struct Geometry {
  std::vector<Tile> tiles;
  std::vector<I> xs, ys;
  std::string order = "ascending", coordinate_type;
  R placement, headroom, clock, slr, density;
  Capacities units;
  std::string prefix, suffix;
};
Geometry BuildGeometry(PhysicalDevice &device, const Capacities &demand,
                       bool mux, bool carry, const R &allowed,
                       const volatile std::sig_atomic_t *flag);
R WriteSiteStore(const std::filesystem::path &path, const Geometry &g,
                 const std::string &architecture_sha);
std::map<std::string, std::vector<R>> BramCandidates(SqliteDatabase &db,
                                                     const Geometry &g);
} // namespace emuflow::core::openparf_input
