#include "timing/routed_model.hpp"
#include "common/bounded_json.hpp"
#include <algorithm>
#include <cctype>
#include <iomanip>
#include <locale>
#include <sstream>
namespace emuflow::core {
namespace {
using Doc = BoundedJson;
using Fields = std::map<std::string, std::string>;
void Check(bool value, const std::string &why) {
  if (!value)
    throw StageError(ErrorCode::kInvalidArtifact,
                     "routed OpenSTA model: " + why);
}
std::vector<std::string> Strings(const std::string &value) {
  std::vector<std::string> result;
  std::set<std::string> unique;
  for (const auto &token : Doc::Array(value, 8192)) {
    auto pin = DecodeJsonString(token);
    Check(!pin.empty() && unique.insert(pin).second, "empty or duplicate pin");
    Check(std::all_of(pin.begin(), pin.end(),
                      [](unsigned char c) {
                        return std::isalnum(c) || c == '_' || c == '$';
                      }),
          "invalid Liberty pin name");
    result.push_back(pin);
  }
  return result;
}
std::string Array(const std::vector<std::string> &values) {
  std::vector<JsonRecord> a;
  for (const auto &v : values)
    a.emplace_back(v);
  return JsonRecord(a).Encode();
}
std::string Number(double x) {
  Check(std::isfinite(x) && x >= 0, "invalid delay");
  return JsonRecord(x).Encode();
}
std::vector<std::string> ScalarPins(const std::string &value) {
  std::set<std::pair<std::string, std::int64_t>> pins;
  std::map<std::string, std::int64_t> widths;
  for (const auto &token : Doc::Array(value, 8192)) {
    const auto pair = Doc::Array(token, 2);
    Check(pair.size() == 2, "invalid pin pair");
    const auto name = DecodeJsonString(pair[0]);
    const auto bit = Doc::IntegerToken(pair[1]);
    Check(!name.empty() && bit >= 0 && bit < 1048576, "invalid pin identity");
    pins.emplace(name, bit);
    widths[name] = std::max(widths[name], bit + 1);
  }
  std::vector<std::string> result;
  for (const auto &pin : pins)
    result.push_back(widths.at(pin.first) == 1
                         ? pin.first
                         : pin.first + "__" + std::to_string(pin.second));
  (void)Strings(Array(result));
  return result;
}
void Scalar(std::ostream &out, const std::string &name, double value,
            const std::string &indent) {
  Check(std::isfinite(value) && value >= 0, "invalid Liberty scalar");
  out << indent << name << " (scalar) {\n"
      << indent << "  values (\"" << std::setprecision(12) << value << "\");\n"
      << indent << "}\n";
}
void Input(std::ostream &out, const std::string &pin, bool clock = false) {
  out << "    pin (" << pin << ") {\n      direction : input;\n";
  if (clock)
    out << "      clock : true;\n";
  out << "      capacitance : 0.001;\n    }\n";
}
void Setup(std::ostream &out, const std::string &pin, const std::string &clock,
           double delay, bool falling) {
  out << "    pin (" << pin
      << ") {\n      direction : input;\n      capacitance : 0.001;\n      "
         "timing () {\n        related_pin : \""
      << clock << "\";\n        timing_type : "
      << (falling ? "setup_falling" : "setup_rising") << ";\n";
  Scalar(out, "rise_constraint", delay, "        ");
  Scalar(out, "fall_constraint", delay, "        ");
  out << "      }\n    }\n";
}
void Arc(std::ostream &out, const std::string &input, double delay,
         const std::string &edge = "") {
  out << "      timing () {\n        related_pin : \"" << input << "\";\n";
  if (!edge.empty())
    out << "        timing_type : " << edge << ";\n";
  out << "        timing_sense : non_unate;\n";
  for (const auto *name : {"cell_rise", "cell_fall"})
    Scalar(out, name, delay, "        ");
  for (const auto *name : {"rise_transition", "fall_transition"})
    Scalar(out, name, 0.01, "        ");
  out << "      }\n";
}
std::string Liberty(Doc &model) {
  auto name = model.Text("name");
  std::replace(name.begin(), name.end(), '-', '_');
  std::ostringstream out;
  out.imbue(std::locale::classic());
  out << "library (" << name
      << ") {\n  delay_model : \"table_lookup\";\n  time_unit : \"1ns\";\n  "
         "voltage_unit : \"1V\";\n  current_unit : \"1mA\";\n  "
         "leakage_power_unit : \"1nW\";\n  pulling_resistance_unit : "
         "\"1kohm\";\n  capacitive_load_unit (1,pF);\n  "
         "input_threshold_pct_rise : 50;\n  input_threshold_pct_fall : 50;\n  "
         "output_threshold_pct_rise : 50;\n  output_threshold_pct_fall : 50;\n "
         " slew_lower_threshold_pct_rise : 20;\n  "
         "slew_lower_threshold_pct_fall : 20;\n  slew_upper_threshold_pct_rise "
         ": 80;\n  slew_upper_threshold_pct_fall : 80;\n\n";
  Doc cells(model.Token("cells"));
  for (const auto &entry : cells.Fields()) {
    (void)Strings(Array({entry.first}));
    Doc cell(entry.second);
    const auto kind = cell.Text("kind");
    out << "  cell (" << entry.first << ") {\n    area : 1.0;\n";
    const auto controls = cell.Has("controls") ? Strings(cell.Token("controls"))
                                               : std::vector<std::string>{};
    const auto outputs = cell.Has("outputs")
                             ? Strings(cell.Token("outputs"))
                             : Strings(Array({cell.Text("output")}));
    if (kind == "combinational") {
      const auto inputs = Strings(cell.Token("inputs"));
      Check(!inputs.empty(), "combinational cell lacks inputs");
      std::set<std::string> unique;
      for (const auto &group : {inputs, controls, outputs})
        for (const auto &pin : group)
          Check(unique.insert(pin).second, "primitive pin roles overlap");
      for (const auto &pin : inputs)
        Input(out, pin);
      for (const auto &pin : controls)
        Input(out, pin);
      for (const auto &pin : outputs) {
        out << "    pin (" << pin << ") {\n      direction : output;\n";
        for (const auto &input : inputs)
          Arc(out, input, cell.Number("delay_ns"));
        out << "    }\n";
      }
    } else if (kind == "rising_edge_ff" || kind == "falling_edge_ff") {
      const bool falling = kind == "falling_edge_ff";
      const auto clock = cell.Text("clock"), data = cell.Text("data");
      std::vector<std::string> roles{clock, data, cell.Text("output")};
      roles.insert(roles.end(), controls.begin(), controls.end());
      (void)Strings(Array(roles));
      out << "    ff (IQ, IQN) {\n      clocked_on : \"" << (falling ? "!" : "")
          << clock << "\";\n      next_state : \"" << data << "\";\n    }\n";
      Input(out, clock, true);
      Setup(out, data, clock, cell.Number("setup_ns"), falling);
      for (const auto &pin : controls)
        Input(out, pin);
      out << "    pin (" << cell.Text("output")
          << ") {\n      direction : output;\n      function : \"IQ\";\n";
      Arc(out, clock, cell.Number("clock_to_q_ns"),
          falling ? "falling_edge" : "rising_edge");
      out << "    }\n";
    } else if (kind == "rising_edge_bank") {
      const auto clock = cell.Text("clock");
      (void)Strings(Array({clock}));
      const auto inputs = Strings(cell.Token("inputs"));
      Check(!inputs.empty(), "sequential bank lacks data inputs");
      std::vector<std::string> roles{clock};
      roles.insert(roles.end(), inputs.begin(), inputs.end());
      roles.insert(roles.end(), controls.begin(), controls.end());
      roles.insert(roles.end(), outputs.begin(), outputs.end());
      (void)Strings(Array(roles));
      Input(out, clock, true);
      for (const auto &pin : inputs)
        Setup(out, pin, clock, cell.Number("setup_ns"), false);
      for (const auto &pin : controls)
        Input(out, pin);
      for (unsigned index = 0; index < outputs.size(); ++index) {
        const auto state = "IQ" + std::to_string(index),
                   inverse = "IQN" + std::to_string(index);
        out << "    ff (" << state << ", " << inverse
            << ") {\n      clocked_on : \"" << clock
            << "\";\n      next_state : \"" << inputs.front()
            << "\";\n    }\n    pin (" << outputs[index]
            << ") {\n      direction : output;\n      function : \"" << state
            << "\";\n";
        Arc(out, clock, cell.Number("clock_to_q_ns"), "rising_edge");
        out << "    }\n";
      }
    } else {
      Check(kind == "constant", "unsupported primitive kind");
      for (const auto &pin : outputs)
        out << "    pin (" << pin << ") {\n      direction : output;\n    }\n";
    }
    out << "  }\n\n";
  }
  out << "}\n";
  return out.str();
}
} // namespace
RoutedOpenstaModel BuildRoutedOpenstaModel(const ArtifactRef &base_model,
                                           const std::string &coefficients,
                                           const std::string &pin_sets) {
  Doc base(Doc::Read(base_model)), coef(coefficients), pins(pin_sets);
  Check(base.Text("schema") == "emuflow.fpga-timing-model/v1" ||
            base.Text("schema") == "emuflow.fpga-timing-model/v2",
        "unsupported base model");
  auto fields = base.Fields();
  Doc source(base.Token("source"));
  Check(std::set<std::string>{"analytical_uncharacterized",
                              "academic_open_model", "calibrated",
                              "characterized"}
            .count(source.Text("qualification")),
        "unsupported base qualification");
  Doc original(base.Token("cells"));
  auto cells = original.Fields();
  double lut = 0;
  for (const auto &c : coef.Fields())
    if (c.first.rfind("lut_", 0) == 0)
      lut = std::max(lut, coef.Number(c.first) / 1000.0);
  for (unsigned width = 1; width <= 6; ++width) {
    const auto name = "LUT" + std::to_string(width);
    Doc cell(cells.at(name));
    auto f = cell.Fields();
    f["delay_ns"] = Number(lut);
    cells[name] = Doc::Encode(f);
  }
  for (const auto *name : {"FDCE", "FDPE", "FDRE", "FDSE"}) {
    auto f = Doc(cells.at(name)).Fields();
    f["clock_to_q_ns"] = Number(coef.Number("ff_clock_to_q") / 1000.0);
    cells[name] = Doc::Encode(f);
    f["kind"] = JsonQuote("falling_edge_ff");
    cells[std::string(name) + "__NEG"] = Doc::Encode(f);
  }
  cells["LUT6_2"] =
      Doc::Encode({{"kind", JsonQuote("combinational")},
                   {"inputs", Array({"I0", "I1", "I2", "I3", "I4", "I5"})},
                   {"outputs", Array({"O5", "O6"})},
                   {"delay_ns", Number(lut)}});
  std::vector<std::string> inputs{"CI", "CI_TOP"}, outputs;
  for (const auto *pin : {"DI", "S"})
    for (unsigned i = 0; i < 8; ++i)
      inputs.push_back(std::string(pin) + "__" + std::to_string(i));
  for (const auto *pin : {"CO", "O"})
    for (unsigned i = 0; i < 8; ++i)
      outputs.push_back(std::string(pin) + "__" + std::to_string(i));
  cells["CARRY8"] =
      Doc::Encode({{"kind", JsonQuote("combinational")},
                   {"inputs", Array(inputs)},
                   {"outputs", Array(outputs)},
                   {"delay_ns", Number(coef.Number("carry_co") / 1000.0)}});
  if (!cells.count("MUXF9"))
    cells["MUXF9"] =
        Doc::Encode({{"kind", JsonQuote("combinational")},
                     {"inputs", Array({"I0", "I1", "S"})},
                     {"output", JsonQuote("O")},
                     {"delay_ns", Doc(cells.at("MUXF8")).Token("delay_ns")}});
  std::vector<std::string> hard;
  for (const auto &entry : pins.Fields()) {
    if (cells.count(entry.first))
      continue;
    Doc ports(entry.second);
    auto inputs = ScalarPins(ports.Token("inputs")),
         outputs = ScalarPins(ports.Token("outputs"));
    if (std::set<std::string>{"RAMB18E2", "RAMB36E2", "URAM288"}.count(
            entry.first)) {
      std::vector<std::string> clocks, controls, data;
      auto upper = [](std::string s) {
        for (auto &c : s)
          c = std::toupper(static_cast<unsigned char>(c));
        return s;
      };
      for (const auto &p : inputs)
        if (upper(p).find("CLK") != std::string::npos)
          clocks.push_back(p);
      Check(!clocks.empty() && !outputs.empty(),
            "hard block lacks clock/outputs");
      const auto clock = clocks.front();
      for (const auto &p : inputs)
        if (p != clock) {
          const auto u = upper(p);
          if (u.find("CLK") != std::string::npos ||
              u.find("RST") != std::string::npos || u == "SLEEP")
            controls.push_back(p);
          else
            data.push_back(p);
        }
      Check(!data.empty(), "hard block lacks timing inputs");
      cells[entry.first] = Doc::Encode({{"kind", JsonQuote("rising_edge_bank")},
                                        {"clock", JsonQuote(clock)},
                                        {"inputs", Array(data)},
                                        {"controls", Array(controls)},
                                        {"outputs", Array(outputs)},
                                        {"setup_ns", "0.0"},
                                        {"clock_to_q_ns", "0.0"}});
    } else {
      Check(entry.first == "DSP48E2" && !inputs.empty() && !outputs.empty(),
            "uncovered primitive or empty DSP pins");
      cells[entry.first] = Doc::Encode({{"kind", JsonQuote("combinational")},
                                        {"inputs", Array(inputs)},
                                        {"outputs", Array(outputs)},
                                        {"delay_ns", "5.0"}});
    }
    hard.push_back(entry.first);
  }
  fields["cells"] = Doc::Encode(cells);
  fields["name"] = JsonQuote("rapidwright-ultrascaleplus-routed-v1");
  fields["family"] = JsonQuote("xcup");
  fields["source"] = Doc::Encode(
      {{"provider", JsonQuote("rapidwright-lightweight+routed-opensta-v1")},
       {"qualification", JsonQuote("analytical_uncharacterized")},
       {"route_delays", JsonQuote("rapidwright-lightweight-exact-per-sink")},
       {"logic_delays",
        JsonQuote("rapidwright-lightweight-conservative-scalar")},
       {"ff_setup", JsonQuote("analytical-uncharacterized")},
       {"hard_block_timing",
        JsonQuote("zero-delay-sequential-surrogate-unqualified")},
       {"hold_analysis", JsonQuote("unavailable")}});
  auto json = Doc::Encode(fields);
  Doc final(json);
  const auto liberty = Liberty(final);
  base_model.RequireUnchanged();
  return {json, liberty, hard};
}
} // namespace emuflow::core
