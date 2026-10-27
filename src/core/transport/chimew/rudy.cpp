// SPDX-License-Identifier: Apache-2.0
// Source-qualified RUDY integration kernel for the Chimew lookahead gate.

#include <algorithm>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

#include "flow/native_stages.hpp"

namespace {

struct Point {
  double x = 0.0;
  double y = 0.0;
};

struct Net {
  int index = -1;
  std::vector<Point> pins;
};

struct Input {
  double origin_x = 0.0;
  double origin_y = 0.0;
  double bin_width = 0.0;
  double bin_height = 0.0;
  int columns = 0;
  int rows = 0;
  double wire_pitch_per_layer = 0.0;
  double max_utilization = 0.0;
  std::vector<double> capacities;
  std::vector<Net> nets;
};

bool finite_positive(double value) {
  return std::isfinite(value) && value > 0.0;
}

Input read_input(std::istream& stream) {
  std::string header;
  if (!(stream >> header) || header != "EMUFLOW_CHIMEW_RUDY_INPUT_V1") {
    throw std::runtime_error("invalid Chimew RUDY input header");
  }
  Input input;
  std::string record;
  if (!(stream >> record) || record != "GRID" ||
      !(stream >> input.origin_x >> input.origin_y >> input.bin_width >>
        input.bin_height >> input.columns >> input.rows)) {
    throw std::runtime_error("malformed Chimew RUDY grid");
  }
  if (!std::isfinite(input.origin_x) || !std::isfinite(input.origin_y) ||
      !finite_positive(input.bin_width) ||
      !finite_positive(input.bin_height) || input.columns <= 0 ||
      input.rows <= 0) {
    throw std::runtime_error("invalid Chimew RUDY grid");
  }
  if (!(stream >> record) || record != "PARAM" ||
      !(stream >> input.wire_pitch_per_layer >> input.max_utilization) ||
      !finite_positive(input.wire_pitch_per_layer) ||
      !finite_positive(input.max_utilization)) {
    throw std::runtime_error("invalid Chimew RUDY parameters");
  }
  const int bin_count = input.columns * input.rows;
  input.capacities.assign(bin_count, 0.0);
  for (int expected = 0; expected < bin_count; ++expected) {
    int index = -1;
    double capacity = 0.0;
    if (!(stream >> record) || record != "CAP" ||
        !(stream >> index >> capacity) || index != expected ||
        !finite_positive(capacity)) {
      throw std::runtime_error("invalid Chimew RUDY bin capacity");
    }
    input.capacities[index] = capacity;
  }
  while (stream >> record) {
    if (record != "NET") {
      throw std::runtime_error("invalid Chimew RUDY record");
    }
    Net net;
    int pin_count = 0;
    if (!(stream >> net.index >> pin_count) ||
        net.index != static_cast<int>(input.nets.size()) || pin_count < 2) {
      throw std::runtime_error("invalid Chimew RUDY net identity");
    }
    net.pins.resize(pin_count);
    for (Point& pin : net.pins) {
      if (!(stream >> pin.x >> pin.y) || !std::isfinite(pin.x) ||
          !std::isfinite(pin.y)) {
        throw std::runtime_error("invalid Chimew RUDY pin");
      }
    }
    input.nets.push_back(std::move(net));
  }
  if (input.nets.empty()) {
    throw std::runtime_error("Chimew RUDY requires at least one net");
  }
  return input;
}

double overlap(double lower_a, double upper_a, double lower_b,
               double upper_b) {
  return std::max(0.0, std::min(upper_a, upper_b) -
                           std::max(lower_a, lower_b));
}

void run(std::istream& input_stream, std::ostream& output) {
  const Input input = read_input(input_stream);
  const double grid_upper_x =
      input.origin_x + input.bin_width * input.columns;
  const double grid_upper_y =
      input.origin_y + input.bin_height * input.rows;
  std::vector<double> loads(input.capacities.size(), 0.0);
  double total_wire_area = 0.0;
  int pin_count = 0;

  for (const Net& net : input.nets) {
    double lower_x = std::numeric_limits<double>::infinity();
    double lower_y = std::numeric_limits<double>::infinity();
    double upper_x = -std::numeric_limits<double>::infinity();
    double upper_y = -std::numeric_limits<double>::infinity();
    for (const Point& pin : net.pins) {
      lower_x = std::min(lower_x, pin.x);
      lower_y = std::min(lower_y, pin.y);
      upper_x = std::max(upper_x, pin.x);
      upper_y = std::max(upper_y, pin.y);
    }
    if (!(lower_x >= input.origin_x && lower_y >= input.origin_y &&
          upper_x <= grid_upper_x && upper_y <= grid_upper_y)) {
      throw std::runtime_error("Chimew RUDY net lies outside the grid");
    }
    const double width = upper_x - lower_x;
    const double height = upper_y - lower_y;
    if (!finite_positive(width) || !finite_positive(height)) {
      throw std::runtime_error(
          "Chimew RUDY v1 rejects zero-area bounding boxes");
    }
    const double hpwl = width + height;
    const double wire_area = hpwl * input.wire_pitch_per_layer;
    const double density = wire_area / (width * height);
    total_wire_area += wire_area;
    pin_count += static_cast<int>(net.pins.size());

    const int first_column = std::max(
        0, static_cast<int>(std::floor((lower_x - input.origin_x) /
                                       input.bin_width)));
    const int last_column = std::min(
        input.columns - 1,
        static_cast<int>(std::ceil((upper_x - input.origin_x) /
                                   input.bin_width)) -
            1);
    const int first_row = std::max(
        0, static_cast<int>(std::floor((lower_y - input.origin_y) /
                                       input.bin_height)));
    const int last_row = std::min(
        input.rows - 1,
        static_cast<int>(std::ceil((upper_y - input.origin_y) /
                                   input.bin_height)) -
            1);
    for (int row = first_row; row <= last_row; ++row) {
      const double bin_lower_y = input.origin_y + row * input.bin_height;
      const double y_overlap =
          overlap(lower_y, upper_y, bin_lower_y, bin_lower_y + input.bin_height);
      for (int column = first_column; column <= last_column; ++column) {
        const double bin_lower_x = input.origin_x + column * input.bin_width;
        const double x_overlap = overlap(lower_x, upper_x, bin_lower_x,
                                         bin_lower_x + input.bin_width);
        loads[row * input.columns + column] += density * x_overlap * y_overlap;
      }
    }
  }

  double total_bin_load = 0.0;
  double peak_load = 0.0;
  double peak_utilization = 0.0;
  int overloaded_bins = 0;
  std::vector<double> utilizations(loads.size(), 0.0);
  for (int index = 0; index < static_cast<int>(loads.size()); ++index) {
    total_bin_load += loads[index];
    peak_load = std::max(peak_load, loads[index]);
    utilizations[index] = loads[index] / input.capacities[index];
    peak_utilization = std::max(peak_utilization, utilizations[index]);
    overloaded_bins += utilizations[index] > input.max_utilization + 1e-12;
  }
  const double conservation_tolerance =
      1e-9 * std::max(1.0, std::abs(total_wire_area));
  if (std::abs(total_bin_load - total_wire_area) > conservation_tolerance) {
    throw std::runtime_error("Chimew RUDY load is not conserved");
  }

  output << std::setprecision(17);
  output << "EMUFLOW_CHIMEW_RUDY_OUTPUT_V1\n";
  output << "METRIC " << input.nets.size() << " " << pin_count << " "
         << total_wire_area << " " << total_bin_load << " " << peak_load
         << " " << peak_utilization << " " << overloaded_bins << "\n";
  for (int index = 0; index < static_cast<int>(loads.size()); ++index) {
    output << "BIN " << index << " " << loads[index] << " "
           << utilizations[index] << "\n";
  }
}

}  // namespace

namespace emuflow::native {

void EvaluateChimewRudy(const std::string& input_path,
                        std::ostream& output) {
  std::ifstream input_file;
  std::istream* input = &std::cin;
  if (input_path != "-") {
    input_file.open(input_path);
    if (!input_file) {
      throw std::runtime_error("cannot open Chimew RUDY input");
    }
    input = &input_file;
  }
  run(*input, output);
  if (!output) {
    throw std::runtime_error("cannot write Chimew RUDY output");
  }
}

}  // namespace emuflow::native
