// Copyright 2026 EmuFlow contributors
// SPDX-License-Identifier: Apache-2.0

#include "pugixml.hpp"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {

using Counts = std::map<std::string, int>;

std::string hex_encode(const std::string& value) {
  static constexpr char kHex[] = "0123456789abcdef";
  std::string result;
  result.reserve(value.size() * 2);
  for (const unsigned char character : value) {
    result.push_back(kHex[character >> 4]);
    result.push_back(kHex[character & 0x0f]);
  }
  return result;
}

std::string attribute(
    const pugi::xml_node& node,
    const char* name,
    const std::string& fallback = "") {
  const auto value = node.attribute(name);
  return value ? value.value() : fallback;
}

std::string node_scope(pugi::xml_node node) {
  std::vector<std::string> segments;
  while (node && std::string(node.name()) != "architecture") {
    std::string segment = node.name();
    const std::string name = attribute(node, "name");
    if (!name.empty()) {
      segment += ":" + name;
    }
    segments.push_back(segment);
    node = node.parent();
  }
  std::reverse(segments.begin(), segments.end());
  std::ostringstream stream;
  for (std::size_t index = 0; index < segments.size(); ++index) {
    if (index != 0) {
      stream << '/';
    }
    stream << segments[index];
  }
  return stream.str();
}

int positive_integer(
    const pugi::xml_node& node,
    const char* name,
    int fallback = 1) {
  const auto raw = node.attribute(name);
  if (!raw) {
    return fallback;
  }
  const int value = raw.as_int(-1);
  if (value <= 0) {
    throw std::runtime_error(
        std::string(node.name()) + "." + name + " must be positive");
  }
  return value;
}

double nonnegative_number(
    const pugi::xml_attribute& raw,
    const std::string& context,
    double fallback = 0.0) {
  if (!raw) {
    return fallback;
  }
  char* end = nullptr;
  const double value = std::strtod(raw.value(), &end);
  if (end == raw.value() || *end != '\0' || !std::isfinite(value) ||
      value < 0.0) {
    throw std::runtime_error(context + " must be a non-negative number");
  }
  return value;
}

std::string canonical_primitive(const pugi::xml_node& node) {
  const std::string model = attribute(node, "blif_model");
  const std::string name = attribute(node, "name");
  if (model == ".names") {
    int width = 0;
    for (const auto port : node.children("input")) {
      width += positive_integer(port, "num_pins");
    }
    if (width <= 0) {
      throw std::runtime_error("LUT primitive has no input pins");
    }
    return "LUT" + std::to_string(width);
  }
  if (model == ".latch") {
    return "DFF";
  }
  if (model == ".input" || model == ".output") {
    return "IOPAD";
  }
  if (model == ".subckt adder") {
    return "ADDER";
  }
  if (model == ".subckt multiply") {
    std::string upper = name;
    std::transform(
        upper.begin(), upper.end(), upper.begin(),
        [](unsigned char character) {
          return static_cast<char>(std::toupper(character));
        });
    return upper;
  }
  if (
      model == ".subckt single_port_ram" ||
      model == ".subckt dual_port_ram") {
    std::string upper = name;
    std::transform(
        upper.begin(), upper.end(), upper.begin(),
        [](unsigned char character) {
          return static_cast<char>(std::toupper(character));
        });
    return upper;
  }
  std::string upper = name;
  std::transform(
      upper.begin(), upper.end(), upper.begin(),
      [](unsigned char character) {
        return static_cast<char>(std::toupper(character));
      });
  return upper;
}

void add_scaled(Counts& target, const Counts& source, int scale) {
  for (const auto& [name, count] : source) {
    target[name] += count * scale;
  }
}

void take_componentwise_max(Counts& target, const Counts& source) {
  for (const auto& [name, count] : source) {
    target[name] = std::max(target[name], count);
  }
}

Counts maximum_leaf_counts(const pugi::xml_node& node) {
  const std::string model = attribute(node, "blif_model");
  if (!model.empty()) {
    return {{canonical_primitive(node), 1}};
  }

  Counts direct_children;
  bool has_direct_children = false;
  for (const auto child : node.children("pb_type")) {
    has_direct_children = true;
    add_scaled(
        direct_children,
        maximum_leaf_counts(child),
        positive_integer(child, "num_pb"));
  }

  Counts mode_maximum;
  bool has_modes = false;
  for (const auto mode : node.children("mode")) {
    has_modes = true;
    Counts mode_counts;
    for (const auto child : mode.children("pb_type")) {
      add_scaled(
          mode_counts,
          maximum_leaf_counts(child),
          positive_integer(child, "num_pb"));
    }
    take_componentwise_max(mode_maximum, mode_counts);
  }
  if (has_direct_children && has_modes) {
    throw std::runtime_error(
        "pb_type mixes direct children and modes: " +
        attribute(node, "name"));
  }
  return has_modes ? mode_maximum : direct_children;
}

void emit_layout(const pugi::xml_node& architecture) {
  const auto layout = architecture.child("layout");
  if (!layout) {
    throw std::runtime_error("VTR architecture has no layout");
  }
  for (const auto candidate : layout.children()) {
    const std::string kind = candidate.name();
    if (kind != "auto_layout" && kind != "fixed_layout") {
      continue;
    }
    std::cout << "LAYOUT\t" << hex_encode(kind) << '\t'
              << hex_encode(attribute(candidate, "name", "default")) << '\t'
              << attribute(candidate, "width", "0") << '\t'
              << attribute(candidate, "height", "0") << '\t'
              << attribute(candidate, "aspect_ratio", "0") << '\n';
    for (const auto rule : candidate.children()) {
      const std::string rule_kind = rule.name();
      if (
          rule_kind != "fill" && rule_kind != "perimeter" &&
          rule_kind != "corners" && rule_kind != "col" &&
          rule_kind != "row" && rule_kind != "single") {
        continue;
      }
      std::cout << "RULE\t" << hex_encode(attribute(candidate, "name", "default"))
                << '\t' << hex_encode(rule_kind) << '\t'
                << hex_encode(attribute(rule, "type")) << '\t'
                << attribute(rule, "priority", "0") << '\t'
                << attribute(rule, rule_kind == "single" ? "x" : "startx", "-1") << '\t'
                << attribute(rule, rule_kind == "single" ? "y" : "starty", "-1") << '\t'
                << attribute(rule, "repeatx", "0") << '\t'
                << attribute(rule, "repeaty", "0") << '\n';
    }
  }
}

void emit_tiles(const pugi::xml_node& architecture) {
  const auto tiles = architecture.child("tiles");
  if (!tiles) {
    throw std::runtime_error("VTR architecture has no tiles");
  }
  for (const auto tile : tiles.children("tile")) {
    const std::string tile_name = attribute(tile, "name");
    if (tile_name.empty()) {
      throw std::runtime_error("VTR tile has no name");
    }
    std::cout << "TILE\t" << hex_encode(tile_name) << '\t'
              << positive_integer(tile, "width") << '\t'
              << positive_integer(tile, "height") << '\n';
    for (const auto sub_tile : tile.children("sub_tile")) {
      const int capacity = positive_integer(sub_tile, "capacity");
      const auto sites =
          sub_tile.child("equivalent_sites").children("site");
      bool emitted = false;
      for (const auto site : sites) {
        const std::string pb_type = attribute(site, "pb_type");
        if (pb_type.empty()) {
          throw std::runtime_error("VTR equivalent site has no pb_type");
        }
        std::cout << "SUBTILE\t" << hex_encode(tile_name) << '\t'
                  << hex_encode(attribute(sub_tile, "name")) << '\t'
                  << capacity << '\t' << hex_encode(pb_type) << '\n';
        emitted = true;
      }
      if (!emitted) {
        throw std::runtime_error(
            "VTR sub_tile has no equivalent site: " +
            attribute(sub_tile, "name"));
      }
    }
  }
}

void emit_resources(const pugi::xml_node& architecture) {
  const auto complex_blocks = architecture.child("complexblocklist");
  if (!complex_blocks) {
    throw std::runtime_error("VTR architecture has no complexblocklist");
  }
  for (const auto pb_type : complex_blocks.children("pb_type")) {
    const std::string name = attribute(pb_type, "name");
    for (const auto& [resource, count] : maximum_leaf_counts(pb_type)) {
      std::cout << "RESOURCE\t" << hex_encode(name) << '\t'
                << hex_encode(resource) << '\t' << count << '\n';
    }
  }
}

void emit_primitive(const pugi::xml_node& node, const std::string& path) {
  const std::string name = attribute(node, "name");
  const std::string current = path.empty() ? name : path + "/" + name;
  const std::string model = attribute(node, "blif_model");
  if (!model.empty()) {
    std::cout << "PRIMITIVE\t" << hex_encode(current) << '\t'
              << hex_encode(canonical_primitive(node)) << '\t'
              << hex_encode(model) << '\t'
              << hex_encode(attribute(node, "class")) << '\n';
    for (const char* direction : {"input", "output", "clock"}) {
      for (const auto port : node.children(direction)) {
        std::cout << "PORT\t" << hex_encode(current) << '\t'
                  << hex_encode(direction) << '\t'
                  << hex_encode(attribute(port, "name")) << '\t'
                  << positive_integer(port, "num_pins") << '\n';
      }
    }
  }

  for (const char* tag :
       {"delay_constant", "delay_matrix", "T_setup", "T_clock_to_Q"}) {
    for (const auto timing : node.children(tag)) {
      const double min_value = nonnegative_number(
          timing.attribute("min"),
          current + "." + tag + ".min",
          nonnegative_number(
              timing.attribute("value"),
              current + "." + tag + ".value"));
      const double max_value = nonnegative_number(
          timing.attribute("max"),
          current + "." + tag + ".max",
          nonnegative_number(
              timing.attribute("value"),
              current + "." + tag + ".value"));
      std::cout << "ARC\t" << hex_encode(current) << '\t'
                << hex_encode(tag) << '\t'
                << hex_encode(attribute(timing, "type")) << '\t'
                << hex_encode(attribute(timing, "in_port")) << '\t'
                << hex_encode(attribute(timing, "out_port")) << '\t'
                << hex_encode(attribute(timing, "port")) << '\t'
                << hex_encode(attribute(timing, "clock")) << '\t'
                << min_value << '\t' << max_value << '\t'
                << hex_encode(timing.text().as_string()) << '\n';
    }
  }

  for (const auto child : node.children("pb_type")) {
    emit_primitive(child, current);
  }
  for (const auto mode : node.children("mode")) {
    const std::string mode_path =
        current + "/mode:" + attribute(mode, "name");
    for (const auto child : mode.children("pb_type")) {
      emit_primitive(child, mode_path);
    }
  }
}

void emit_primitives_and_internal_arcs(const pugi::xml_node& architecture) {
  const auto complex_blocks = architecture.child("complexblocklist");
  for (const auto pb_type : complex_blocks.children("pb_type")) {
    emit_primitive(pb_type, "");
  }
  for (const auto timing : architecture.select_nodes(
           ".//interconnect/*/delay_constant | "
           ".//interconnect/*/delay_matrix")) {
    const auto node = timing.node();
    std::cout << "BLOCK_ARC\t" << hex_encode(node_scope(node.parent())) << '\t'
              << hex_encode(node.name()) << '\t'
              << hex_encode(attribute(node, "type")) << '\t'
              << hex_encode(attribute(node, "in_port")) << '\t'
              << hex_encode(attribute(node, "out_port")) << '\t'
              << attribute(node, "min", attribute(node, "value", "0")) << '\t'
              << attribute(node, "max", attribute(node, "value", "0")) << '\t'
              << hex_encode(node.text().as_string()) << '\n';
  }
}

void emit_routing(const pugi::xml_node& architecture) {
  for (const auto sw : architecture.child("switchlist").children("switch")) {
    std::cout << "SWITCH\t" << hex_encode(attribute(sw, "name")) << '\t'
              << hex_encode(attribute(sw, "type")) << '\t'
              << attribute(sw, "R", "0") << '\t'
              << attribute(sw, "Cin", "0") << '\t'
              << attribute(sw, "Cout", "0") << '\t'
              << attribute(sw, "Tdel", "0") << '\n';
  }
  int index = 0;
  for (const auto segment :
       architecture.child("segmentlist").children("segment")) {
    std::string mux;
    if (segment.child("mux")) {
      mux = attribute(segment.child("mux"), "name");
    }
    std::cout << "SEGMENT\t" << index++ << '\t'
              << attribute(segment, "length", "1") << '\t'
              << hex_encode(attribute(segment, "type")) << '\t'
              << attribute(segment, "freq", "0") << '\t'
              << attribute(segment, "Rmetal", "0") << '\t'
              << attribute(segment, "Cmetal", "0") << '\t'
              << hex_encode(mux) << '\n';
  }
  for (const auto direct :
       architecture.child("directlist").children("direct")) {
    std::cout << "DIRECT\t" << hex_encode(attribute(direct, "name")) << '\t'
              << hex_encode(attribute(direct, "from_pin")) << '\t'
              << hex_encode(attribute(direct, "to_pin")) << '\t'
              << attribute(direct, "x_offset", "0") << '\t'
              << attribute(direct, "y_offset", "0") << '\t'
              << attribute(direct, "z_offset", "0") << '\t'
              << hex_encode(attribute(direct, "switch_name")) << '\n';
  }
}

}  // namespace

int main(int argc, char** argv) {
  try {
    if (argc != 2) {
      throw std::runtime_error(
          "usage: emuflow_vtr_arch_importer ARCHITECTURE_XML");
    }
    pugi::xml_document document;
    const pugi::xml_parse_result parsed = document.load_file(argv[1]);
    if (!parsed) {
      throw std::runtime_error(
          "cannot parse VTR architecture XML: " +
          std::string(parsed.description()));
    }
    const auto architecture = document.child("architecture");
    if (!architecture) {
      throw std::runtime_error("XML root is not <architecture>");
    }
    std::cout << "EMUFLOW_VTR_ARCHITECTURE_EXTRACT_V1\n";
    emit_layout(architecture);
    emit_tiles(architecture);
    emit_resources(architecture);
    emit_primitives_and_internal_arcs(architecture);
    emit_routing(architecture);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "emuflow_vtr_arch_importer: " << error.what() << '\n';
    return 1;
  }
}
