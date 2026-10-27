// Copyright 2026 EmuFlow contributors
// SPDX-License-Identifier: Apache-2.0

#include <capnp/serialize.h>
#include <zlib.h>

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iostream>
#include <limits>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "interchange/DeviceResources.capnp.h"

namespace {

using CompatibilityMap =
    std::map<std::pair<std::string, std::string>, std::set<std::string>>;

struct BelRecord {
  std::string name;
  std::string type;
  std::uint32_t z;
  std::set<std::string> cells;
};

using SiteTemplates = std::map<std::string, std::vector<BelRecord>>;

struct ReferenceIntegrity {
  std::uint64_t site_types = 0;
  std::uint64_t tile_types = 0;
  std::uint64_t tiles = 0;
  std::uint64_t wires = 0;
  std::uint64_t nodes = 0;
  std::uint64_t pips = 0;
  std::uint64_t node_wire_memberships = 0;
  std::uint64_t wires_without_node_membership = 0;
};

std::string json_escape(const std::string& value) {
  std::string result;
  result.reserve(value.size() + 8);
  for (const unsigned char character : value) {
    switch (character) {
      case '"':
        result += "\\\"";
        break;
      case '\\':
        result += "\\\\";
        break;
      case '\b':
        result += "\\b";
        break;
      case '\f':
        result += "\\f";
        break;
      case '\n':
        result += "\\n";
        break;
      case '\r':
        result += "\\r";
        break;
      case '\t':
        result += "\\t";
        break;
      default:
        if (character < 0x20) {
          static constexpr char kHex[] = "0123456789abcdef";
          result += "\\u00";
          result += kHex[(character >> 4) & 0xf];
          result += kHex[character & 0xf];
        } else {
          result += static_cast<char>(character);
        }
    }
  }
  return result;
}

void write_json_string(std::ostream& output, const std::string& value) {
  output << '"' << json_escape(value) << '"';
}

std::vector<unsigned char> read_input(const std::string& path) {
  std::ifstream probe(path, std::ios::binary);
  if (!probe) {
    throw std::runtime_error("cannot open input file: " + path);
  }
  unsigned char magic[2] = {0, 0};
  probe.read(reinterpret_cast<char*>(magic), sizeof(magic));
  const bool gzip = probe.gcount() == 2 && magic[0] == 0x1f && magic[1] == 0x8b;
  probe.close();

  std::vector<unsigned char> bytes;
  if (gzip) {
    gzFile input = gzopen(path.c_str(), "rb");
    if (input == nullptr) {
      throw std::runtime_error("cannot open gzip input file: " + path);
    }
    std::vector<unsigned char> chunk(1U << 20);
    while (true) {
      const int count =
          gzread(input, chunk.data(), static_cast<unsigned int>(chunk.size()));
      if (count < 0) {
        int code = Z_OK;
        const char* message = gzerror(input, &code);
        gzclose(input);
        throw std::runtime_error(
            "gzip read failed: " +
            std::string(message == nullptr ? "unknown error" : message));
      }
      if (count == 0) {
        break;
      }
      bytes.insert(bytes.end(), chunk.begin(), chunk.begin() + count);
    }
    if (gzclose(input) != Z_OK) {
      throw std::runtime_error("gzip close failed");
    }
  } else {
    std::ifstream input(path, std::ios::binary | std::ios::ate);
    const auto size = input.tellg();
    if (size <= 0) {
      throw std::runtime_error("input file is empty");
    }
    bytes.resize(static_cast<std::size_t>(size));
    input.seekg(0);
    input.read(
        reinterpret_cast<char*>(bytes.data()),
        static_cast<std::streamsize>(bytes.size()));
    if (!input) {
      throw std::runtime_error("input file read failed");
    }
  }
  if (bytes.empty() || bytes.size() % sizeof(capnp::word) != 0) {
    throw std::runtime_error(
        "DeviceResources payload is not an aligned Cap'n Proto message");
  }
  return bytes;
}

template <typename StringList>
std::string string_at(const StringList& strings, std::uint32_t index) {
  if (index >= strings.size()) {
    throw std::runtime_error("DeviceResources string index is out of range");
  }
  const auto value = strings[index];
  return std::string(value.cStr(), value.size());
}

bool supported_cell(const std::string& cell) {
  static const std::set<std::string> kSupported = {
      "CARRY8",   "DSP48E2", "FDCE",     "FDPE",    "FDRE",
      "FDSE",     "LUT1",    "LUT2",     "LUT3",    "LUT4",
      "LUT5",     "LUT6",    "LUT6_2",   "RAM64X1S", "RAMB18E2",
      "RAMB36E2", "URAM288", "MUXF7",    "MUXF8", "MUXF9",
  };
  return kSupported.count(cell) != 0;
}

std::string resource_class(
    const std::string& bel_name,
    const std::set<std::string>& cells) {
  if (bel_name.size() == 5 && bel_name[1] == '6' &&
      bel_name.substr(2) == "LUT") {
    return "lut";
  }
  if (cells.count("FDRE") != 0) {
    return "ff";
  }
  if (cells.count("MUXF7") != 0) {
    return "MUXF7";
  }
  if (cells.count("MUXF8") != 0) {
    return "MUXF8";
  }
  if (cells.count("MUXF9") != 0) {
    return "MUXF9";
  }
  if (cells.count("RAM64X1S") != 0) {
    return "RAM64X1S";
  }
  for (const char* cell :
       {"CARRY8", "DSP48E2", "RAMB18E2", "RAMB36E2", "URAM288"}) {
    if (cells.count(cell) != 0) {
      return cell;
    }
  }
  return bel_name;
}

bool excluded_shared_lut_bel(const std::string& bel_name) {
  return bel_name.size() == 5 && bel_name[1] == '5' &&
         bel_name.substr(2) == "LUT";
}

template <typename DeviceReader, typename StringList>
ReferenceIntegrity validate_reference_integrity(
    const DeviceReader& device,
    const StringList& strings) {
  ReferenceIntegrity result;
  const auto site_types = device.getSiteTypeList();
  const auto tile_types = device.getTileTypeList();
  const auto tiles = device.getTileList();
  const auto wires = device.getWires();
  const auto nodes = device.getNodes();
  const auto wire_types = device.getWireTypes();
  const auto pip_timings = device.getPipTimings();
  const auto node_timings = device.getNodeTimings();

  result.site_types = site_types.size();
  result.tile_types = tile_types.size();
  result.tiles = tiles.size();
  result.wires = wires.size();
  result.nodes = nodes.size();

  for (const auto site_type : site_types) {
    string_at(strings, site_type.getName());
    const auto bel_pins = site_type.getBelPins();
    for (const auto bel_pin : bel_pins) {
      string_at(strings, bel_pin.getName());
      string_at(strings, bel_pin.getBel());
    }
    for (const auto pin : site_type.getPins()) {
      string_at(strings, pin.getName());
      if (pin.getBelpin() >= bel_pins.size()) {
        throw std::runtime_error(
            "DeviceResources site pin BEL-pin index is out of range");
      }
    }
    for (const auto bel : site_type.getBels()) {
      string_at(strings, bel.getName());
      string_at(strings, bel.getType());
      for (const auto pin : bel.getPins()) {
        if (pin >= bel_pins.size()) {
          throw std::runtime_error(
              "DeviceResources BEL pin index is out of range");
        }
      }
    }
    for (const auto site_pip : site_type.getSitePIPs()) {
      if (site_pip.getInpin() >= bel_pins.size() ||
          site_pip.getOutpin() >= bel_pins.size()) {
        throw std::runtime_error(
            "DeviceResources site PIP BEL-pin index is out of range");
      }
    }
    for (const auto site_wire : site_type.getSiteWires()) {
      string_at(strings, site_wire.getName());
      for (const auto pin : site_wire.getPins()) {
        if (pin >= bel_pins.size()) {
          throw std::runtime_error(
              "DeviceResources site wire BEL-pin index is out of range");
        }
      }
    }
    for (const auto alternative : site_type.getAltSiteTypes()) {
      if (alternative >= site_types.size()) {
        throw std::runtime_error(
            "DeviceResources alternative site type is out of range");
      }
    }
  }

  std::map<std::string, std::set<std::string>> tile_type_wires;
  for (const auto tile_type : tile_types) {
    const std::string tile_type_name =
        string_at(strings, tile_type.getName());
    auto& wire_names = tile_type_wires[tile_type_name];
    for (const auto wire : tile_type.getWires()) {
      wire_names.insert(string_at(strings, wire));
    }
    for (const auto site : tile_type.getSiteTypes()) {
      if (site.getPrimaryType() >= site_types.size()) {
        throw std::runtime_error(
            "DeviceResources tile site type is out of range");
      }
      const auto primary = site_types[site.getPrimaryType()];
      const auto primary_pins = primary.getPins();
      if (site.getPrimaryPinsToTileWires().size() !=
          primary_pins.size()) {
        throw std::runtime_error(
            "DeviceResources primary site-pin mapping has wrong length");
      }
      for (const auto wire : site.getPrimaryPinsToTileWires()) {
        string_at(strings, wire);
      }
      if (site.getAltPinsToPrimaryPins().size() !=
          primary.getAltSiteTypes().size()) {
        throw std::runtime_error(
            "DeviceResources alternate site-pin mapping has wrong length");
      }
      for (const auto parent_pins : site.getAltPinsToPrimaryPins()) {
        for (const auto pin : parent_pins.getPins()) {
          if (pin >= primary_pins.size()) {
            throw std::runtime_error(
                "DeviceResources alternate site-pin index is out of range");
          }
        }
      }
    }
    for (const auto pip : tile_type.getPips()) {
      if (pip.getWire0() >= tile_type.getWires().size() ||
          pip.getWire1() >= tile_type.getWires().size()) {
        throw std::runtime_error(
            "DeviceResources tile PIP wire index is out of range");
      }
      // RapidWright's DeviceResources writer does not populate the optional
      // pip timing table.  In that representation the schema-default timing
      // index is not a dangling reference: timing is simply unqualified.
      if (pip_timings.size() != 0 && pip.getTiming() >= pip_timings.size()) {
        throw std::runtime_error(
            "DeviceResources tile PIP timing index is out of range");
      }
      ++result.pips;
    }
    for (const auto constant : tile_type.getConstants()) {
      for (const auto wire : constant.getWires()) {
        if (wire >= tile_type.getWires().size()) {
          throw std::runtime_error(
              "DeviceResources constant wire index is out of range");
        }
      }
    }
  }

  std::map<std::string, std::string> tile_to_type;
  for (const auto tile : tiles) {
    if (tile.getType() >= tile_types.size()) {
      throw std::runtime_error("DeviceResources tile type is out of range");
    }
    const std::string tile_name = string_at(strings, tile.getName());
    const std::string tile_type_name =
        string_at(strings, tile_types[tile.getType()].getName());
    if (!tile_to_type.emplace(tile_name, tile_type_name).second) {
      throw std::runtime_error("DeviceResources tile name is duplicated");
    }
    for (const auto site : tile.getSites()) {
      string_at(strings, site.getName());
      if (site.getType() >= tile_types[tile.getType()].getSiteTypes().size()) {
        throw std::runtime_error(
            "DeviceResources site-in-tile type is out of range");
      }
    }
    const auto prefixes = tile.getSubTilesPrefices();
    for (const auto prefix : prefixes) {
      string_at(strings, prefix);
    }
    for (const auto pip : tile_types[tile.getType()].getPips()) {
      if ((prefixes.size() == 0 && pip.getSubTile() != 0) ||
          (prefixes.size() != 0 && pip.getSubTile() >= prefixes.size())) {
        throw std::runtime_error(
            "DeviceResources tile PIP sub-tile index is out of range");
      }
    }
  }

  for (const auto wire : wires) {
    const std::string tile_name = string_at(strings, wire.getTile());
    const std::string wire_name = string_at(strings, wire.getWire());
    if (wire.getType() >= wire_types.size()) {
      throw std::runtime_error(
          "DeviceResources wire type is out of range");
    }
    const auto tile = tile_to_type.find(tile_name);
    if (tile == tile_to_type.end() ||
        tile_type_wires[tile->second].count(wire_name) == 0) {
      throw std::runtime_error(
          "DeviceResources global wire does not resolve in its tile type");
    }
  }
  std::vector<std::uint8_t> wire_membership(wires.size(), 0);
  for (const auto node : nodes) {
    // As with PIP timing, an empty optional node timing table means that
    // physical timing was not encoded by the producer.
    if (node_timings.size() != 0 &&
        node.getNodeTiming() >= node_timings.size()) {
      throw std::runtime_error(
          "DeviceResources node timing index is out of range");
    }
    for (const auto wire : node.getWires()) {
      if (wire >= wires.size()) {
        throw std::runtime_error(
            "DeviceResources node wire index is out of range");
      }
      if (++wire_membership[wire] != 1) {
        throw std::runtime_error(
            "DeviceResources wire belongs to multiple nodes");
      }
      ++result.node_wire_memberships;
    }
  }
  result.wires_without_node_membership = static_cast<std::uint64_t>(
      std::count(wire_membership.begin(), wire_membership.end(), 0));
  return result;
}

template <typename DeviceReader, typename StringList>
CompatibilityMap build_compatibility(
    const DeviceReader& device,
    const StringList& strings) {
  CompatibilityMap result;
  for (const auto mapping : device.getCellBelMap()) {
    const std::string cell = string_at(strings, mapping.getCell());
    if (!supported_cell(cell)) {
      continue;
    }
    for (const auto common : mapping.getCommonPins()) {
      for (const auto entry : common.getSiteTypes()) {
        const std::string site_type =
            string_at(strings, entry.getSiteType());
        for (const auto bel_index : entry.getBels()) {
          result[{site_type, string_at(strings, bel_index)}].insert(cell);
        }
      }
    }
    for (const auto parameter : mapping.getParameterPins()) {
      for (const auto entry : parameter.getParametersSiteTypes()) {
        result[{string_at(strings, entry.getSiteType()),
                string_at(strings, entry.getBel())}]
            .insert(cell);
      }
    }
  }
  return result;
}

template <typename SiteTypeList, typename StringList>
SiteTemplates build_site_templates(
    const SiteTypeList& site_types,
    const StringList& strings,
    const CompatibilityMap& compatibility) {
  SiteTemplates result;
  for (const auto site_type : site_types) {
    const std::string site_type_name =
        string_at(strings, site_type.getName());
    std::vector<
        std::pair<std::string, DeviceResources::Device::BEL::Reader>>
        ordered_bels;
    for (const auto bel : site_type.getBels()) {
      ordered_bels.emplace_back(string_at(strings, bel.getName()), bel);
    }
    std::sort(
        ordered_bels.begin(), ordered_bels.end(),
        [](const auto& left, const auto& right) {
          return left.first < right.first;
        });
    std::set<std::string> bel_names;
    for (const auto& item : ordered_bels) {
      bel_names.insert(item.first);
    }
    std::map<std::string, std::uint32_t> next_z;
    std::vector<BelRecord> records;
    std::string dsp_representative_type;
    for (const auto& item : ordered_bels) {
      const std::string bel_type = string_at(strings, item.second.getType());
      if (site_type_name == "DSP48E2" && item.first == "DSP_ALU") {
        dsp_representative_type = bel_type;
      }
      if (excluded_shared_lut_bel(item.first)) {
        continue;
      }
      const auto found =
          compatibility.find({site_type_name, item.first});
      if (found == compatibility.end() || found->second.empty()) {
        continue;
      }
      std::set<std::string> cells = found->second;
      if (item.first.size() == 5 && item.first[1] == '6' &&
          item.first.substr(2) == "LUT" && cells.count("LUT6") != 0) {
        std::string shared_lut_bel = item.first;
        shared_lut_bel[1] = '5';
        if (bel_names.count(shared_lut_bel) != 0) {
          // FPGA Interchange exposes the O5 and O6 resources as paired
          // physical LUT5/LUT6 BELs, while Route A represents the required
          // dual-output carry adapter as one logical LUT6_2 placement unit.
          // Admit that composite view only when both component BELs exist;
          // the RWRoute boundary expands it back to the exact physical pair.
          cells.insert("LUT6_2");
        }
      }
      if (site_type_name == "SLICEM" &&
          item.first.size() == 5 && item.first[1] == '6' &&
          item.first.substr(2) == "LUT") {
        // RAM64X1S is a macro view of one SLICEM LUT storage element and is
        // absent from RapidWright's monolithic cellBelMap.
        cells.insert("RAM64X1S");
      }
      const std::string klass = resource_class(item.first, cells);
      records.push_back(
          {item.first, bel_type, next_z[klass]++, std::move(cells)});
    }
    // RapidWright represents DSP48E2 as a macro over component BELs, so the
    // generated cellBelMap has no monolithic DSP48E2 entry. Use the schema's
    // canonical DSP_ALU component as the site-level global-placement resource;
    // physical lowering must expand the macro before detailed placement.
    if (site_type_name == "DSP48E2" &&
        !dsp_representative_type.empty()) {
      records.push_back(
          {"DSP_ALU", dsp_representative_type, 0, {"DSP48E2"}});
    }
    if (!records.empty()) {
      result.emplace(site_type_name, std::move(records));
    }
  }
  return result;
}

template <typename DeviceReader>
void write_extract(const DeviceReader& device, std::ostream& output) {
  const auto strings = device.getStrList();
  const ReferenceIntegrity reference_integrity =
      validate_reference_integrity(device, strings);
  const auto site_types = device.getSiteTypeList();
  const auto tile_types = device.getTileTypeList();
  const CompatibilityMap compatibility =
      build_compatibility(device, strings);
  const SiteTemplates site_templates =
      build_site_templates(site_types, strings, compatibility);
  std::map<std::string, std::vector<std::string>> alternative_templates;
  for (const auto site_type : site_types) {
    const std::string name = string_at(strings, site_type.getName());
    if (site_templates.count(name) == 0) {
      continue;
    }
    for (const auto alternative_index : site_type.getAltSiteTypes()) {
      if (alternative_index >= site_types.size()) {
        throw std::runtime_error(
            "DeviceResources alternative site type is out of range");
      }
      const std::string alternative =
          string_at(strings, site_types[alternative_index].getName());
      if (alternative != name &&
          site_templates.count(alternative) != 0) {
        alternative_templates[name].push_back(alternative);
      }
    }
    // UltraScale+ exposes each 36-Kb BRAM site through two RAMB18E2 modes
    // (lower/upper) or one RAMB36E2 mode. RapidWright's DeviceResources uses
    // RAMB181 as the instantiated primary site and leaves the sibling modes
    // as otherwise-unreferenced templates, so make that packing relation
    // explicit for global resource accounting.
    if (name == "RAMB181" &&
        site_templates.count("RAMB180") != 0 &&
        site_templates.count("RAMB36") != 0) {
      alternative_templates[name].push_back("RAMB180");
      alternative_templates[name].push_back("RAMB36");
    }
    auto& alternatives = alternative_templates[name];
    std::sort(alternatives.begin(), alternatives.end());
    alternatives.erase(
        std::unique(alternatives.begin(), alternatives.end()),
        alternatives.end());
  }

  output << "{\"schema\":"
         << "\"emuflow.fpga-interchange-architecture-extract/v1\","
         << "\"device\":";
  const auto device_name = device.getName();
  write_json_string(
      output, std::string(device_name.cStr(), device_name.size()));
  output << ",\"site_templates\":{";
  bool first_template = true;
  for (const auto& item : site_templates) {
    if (!first_template) {
      output << ',';
    }
    first_template = false;
    write_json_string(output, item.first);
    output << ":{\"alternative_templates\":[";
    bool first_alternative = true;
    for (const auto& alternative : alternative_templates[item.first]) {
      if (!first_alternative) {
        output << ',';
      }
      first_alternative = false;
      write_json_string(output, alternative);
    }
    output << "],\"bels\":[";
    bool first_bel = true;
    for (const auto& bel : item.second) {
      if (!first_bel) {
        output << ',';
      }
      first_bel = false;
      output << "{\"name\":";
      write_json_string(output, bel.name);
      output << ",\"type\":";
      write_json_string(output, bel.type);
      output << ",\"z\":" << bel.z << ",\"compatible_cells\":[";
      bool first_cell = true;
      for (const auto& cell : bel.cells) {
        if (!first_cell) {
          output << ',';
        }
        first_cell = false;
        write_json_string(output, cell);
      }
      output << "]}";
    }
    output << "]}";
  }
  output << "},\"tiles\":[";

  bool first_tile = true;
  std::uint64_t emitted_sites = 0;
  std::uint64_t emitted_bels = 0;
  for (const auto tile : device.getTileList()) {
    if (tile.getType() >= tile_types.size()) {
      throw std::runtime_error("DeviceResources tile type is out of range");
    }
    const auto tile_type = tile_types[tile.getType()];
    std::vector<std::string> rendered_sites;
    std::uint32_t site_index = 0;
    for (const auto site : tile.getSites()) {
      if (site.getType() >= tile_type.getSiteTypes().size()) {
        throw std::runtime_error(
            "DeviceResources site-in-tile type is out of range");
      }
      const auto site_in_tile = tile_type.getSiteTypes()[site.getType()];
      if (site_in_tile.getPrimaryType() >= site_types.size()) {
        throw std::runtime_error("DeviceResources site type is out of range");
      }
      const auto site_type = site_types[site_in_tile.getPrimaryType()];
      const std::string site_type_name =
          string_at(strings, site_type.getName());
      const auto template_found = site_templates.find(site_type_name);
      if (template_found == site_templates.end()) {
        ++site_index;
        continue;
      }

      std::ostringstream site_output;
      site_output << "{\"name\":";
      write_json_string(site_output, string_at(strings, site.getName()));
      site_output << ",\"type\":";
      write_json_string(site_output, site_type_name);
      site_output << ",\"index_in_tile\":" << site_index << '}';
      rendered_sites.push_back(site_output.str());
      ++emitted_sites;
      emitted_bels += template_found->second.size();
      ++site_index;
    }
    if (rendered_sites.empty()) {
      continue;
    }
    if (!first_tile) {
      output << ',';
    }
    first_tile = false;
    output << "{\"name\":";
    write_json_string(output, string_at(strings, tile.getName()));
    output << ",\"type\":";
    write_json_string(output, string_at(strings, tile_type.getName()));
    output << ",\"row\":" << tile.getRow() << ",\"col\":" << tile.getCol()
           << ",\"sites\":[";
    for (std::size_t index = 0; index < rendered_sites.size(); ++index) {
      if (index != 0) {
        output << ',';
      }
      output << rendered_sites[index];
    }
    output << "]}";
  }

  output << "],\"packages\":[";
  bool first_package = true;
  for (const auto package : device.getPackages()) {
    if (!first_package) {
      output << ',';
    }
    first_package = false;
    output << "{\"name\":";
    write_json_string(output, string_at(strings, package.getName()));
    output << ",\"package_pin_count\":" << package.getPackagePins().size()
           << ",\"grades\":[";
    bool first_grade = true;
    for (const auto grade : package.getGrades()) {
      if (!first_grade) {
        output << ',';
      }
      first_grade = false;
      output << "{\"name\":";
      write_json_string(output, string_at(strings, grade.getName()));
      output << ",\"speed_grade\":";
      write_json_string(output, string_at(strings, grade.getSpeedGrade()));
      output << ",\"temperature_grade\":";
      write_json_string(
          output, string_at(strings, grade.getTemperatureGrade()));
      output << '}';
    }
    output << "]}";
  }
  const bool route_resources_present =
      reference_integrity.wires != 0 && reference_integrity.nodes != 0;
  const bool route_timings_present =
      device.getPipTimings().size() != 0 ||
      device.getNodeTimings().size() != 0;
  output << "],\"reference_integrity\":{"
         << "\"status\":\"pass\","
         << "\"route_resources_present\":"
         << (route_resources_present ? "true" : "false") << ','
         << "\"route_timings_present\":"
         << (route_timings_present ? "true" : "false") << ','
         << "\"site_types\":" << reference_integrity.site_types << ','
         << "\"tile_types\":" << reference_integrity.tile_types << ','
         << "\"tiles\":" << reference_integrity.tiles << ','
         << "\"wires\":" << reference_integrity.wires << ','
         << "\"nodes\":" << reference_integrity.nodes << ','
         << "\"pips\":" << reference_integrity.pips << ','
         << "\"node_wire_memberships\":"
         << reference_integrity.node_wire_memberships << ','
         << "\"wires_without_node_membership\":"
         << reference_integrity.wires_without_node_membership << "},"
         << "\"resource_counts\":{"
         << "\"all_tiles\":" << device.getTileList().size() << ','
         << "\"tile_types\":" << device.getTileTypeList().size() << ','
         << "\"site_types\":" << device.getSiteTypeList().size() << ','
         << "\"placement_sites\":" << emitted_sites << ','
         << "\"placement_bels\":" << emitted_bels << ','
         << "\"wires\":" << device.getWires().size() << ','
         << "\"nodes\":" << device.getNodes().size() << ','
         << "\"pip_timings\":" << device.getPipTimings().size() << ','
         << "\"node_timings\":" << device.getNodeTimings().size()
         << "}}\n";
}

void usage(std::ostream& output) {
  output
      << "usage: emuflow_fpgaif_arch_importer DEVICE_RESOURCES OUTPUT_JSON\n"
      << "Reads gzip/raw unpacked FPGA Interchange DeviceResources and emits "
         "the supported UltraScale+ placement inventory.\n";
}

}  // namespace

int main(int argc, char** argv) {
  if (argc == 2 && std::string(argv[1]) == "--help") {
    usage(std::cout);
    return 0;
  }
  if (argc != 3) {
    usage(std::cerr);
    return 2;
  }
  try {
    const std::vector<unsigned char> bytes = read_input(argv[1]);
    const std::size_t word_count = bytes.size() / sizeof(capnp::word);
    auto words = kj::heapArray<capnp::word>(word_count);
    std::memcpy(words.begin(), bytes.data(), bytes.size());
    capnp::ReaderOptions options;
    options.traversalLimitInWords =
        std::numeric_limits<std::uint64_t>::max() / 8;
    options.nestingLimit = 128;
    capnp::FlatArrayMessageReader reader(words.asPtr(), options);
    const auto device = reader.getRoot<DeviceResources::Device>();
    std::ofstream output(argv[2], std::ios::binary);
    if (!output) {
      throw std::runtime_error(
          "cannot open output file: " + std::string(argv[2]));
    }
    write_extract(device, output);
    if (!output) {
      throw std::runtime_error("failed to write output JSON");
    }
    return 0;
  } catch (const kj::Exception& error) {
    std::cerr << "emuflow_fpgaif_arch_importer: Cap'n Proto error: "
              << error.getDescription().cStr() << '\n';
  } catch (const std::exception& error) {
    std::cerr << "emuflow_fpgaif_arch_importer: " << error.what() << '\n';
  }
  return 1;
}
