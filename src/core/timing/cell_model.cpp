#include "timing/cell_model.hpp"
#include "common/bounded_json.hpp"
#include "storage/store.hpp"
#include "timing/routed_model.hpp"
#include "timing/opensta_export.hpp"
#include <algorithm>
#include <cctype>
namespace emuflow::core {
namespace {
using D = BoundedJson;
using O = std::map<std::string, std::string>;
using I = std::int64_t;
void Check(bool ok, const std::string &why) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidArtifact, "timing cell model: " + why);
}
std::string Text(const std::string &s) {
  Check(s.find('\0') == std::string::npos, "NUL in identity");
  return JsonQuoteUtf8(s);
}
std::string Array(const std::vector<std::string> &values) {
  std::vector<JsonRecord> rows;
  for (const auto &value : values)
    rows.emplace_back(value);
  return JsonRecord(rows).Encode();
}
std::vector<std::string> Strings(const std::string &value) {
  std::vector<std::string> out;
  for (const auto &token : D::Array(value, 8192))
    out.push_back(DecodeJsonString(token));
  return out;
}
std::string Lower(std::string s) {
  for (auto &c : s)
    c = std::tolower(static_cast<unsigned char>(c));
  return s;
}
std::set<std::string> UsedTypes(SqliteDatabase &db, bool architecture) {
  std::set<std::string> used;
  auto q = db.Prepare("SELECT DISTINCT "
                      "c.type,json_type(p.value,'$.IS_C_INVERTED'),json_"
                      "extract(p.value,'$.IS_C_INVERTED') FROM cell_records c "
                      "LEFT JOIN payloads p ON p.seq=c.parameters_seq");
  while (q.Step()) {
    auto name = q.TypedText(0);
    Check(!name.empty(), "empty primitive identity");
    const auto kind = q.IsNull(1) ? "null" : q.TypedText(1);
    if (architecture &&
        std::set<std::string>{"FDCE", "FDPE", "FDRE", "FDSE"}.count(name) &&
        TimingParameterIsOne(kind, kind == "integer" ? q.TypedInteger(2) : 0,
                             kind == "text" ? q.TypedText(2) : ""))
      name += "__NEG";
    used.insert(name);
    Check(used.size() <= 4096, "primitive catalog exceeds bound");
  }
  return used;
}
std::vector<std::string> ScalarPins(SqliteDatabase &db, I seq, int role) {
  auto q = db.Prepare(
      "SELECT p.name,e.bit FROM endpoint_records e LEFT JOIN pin_names p ON "
      "p.seq=e.port_seq WHERE e.instance_seq=? AND e.role=? UNION SELECT "
      "json_extract(k.value,'$.port'),json_extract(k.value,'$.bit') FROM "
      "cell_records c JOIN payloads p ON "
      "p.seq=c.constants_seq,json_each(p.value) k WHERE c.seq=? AND ?=1 ORDER "
      "BY 1,2");
  q.BindInteger(1, seq);
  q.BindInteger(2, role);
  q.BindInteger(3, seq);
  q.BindInteger(4, role);
  std::set<std::pair<std::string, I>> pins;
  std::map<std::string, I> widths;
  while (q.Step()) {
    auto name = q.TypedText(0);
    auto bit = q.TypedInteger(1);
    Check(!name.empty() && bit >= 0 && pins.size() < 8192,
          "primitive pin identity/catalog is invalid");
    pins.emplace(name, bit);
    widths[name] = std::max(widths[name], bit);
  }
  std::vector<std::string> out;
  for (const auto &pin : pins)
    out.push_back(widths.at(pin.first) == 0
                      ? pin.first
                      : pin.first + "__" + std::to_string(pin.second));
  return out;
}
std::map<std::string, double>
Delays(const std::map<std::string, ArtifactRef> &sources, SqliteDatabase &db) {
  D arch(D::Read(sources.at("architecture-timing-db"))),
      contract(D::Read(sources.at("architecture-contract")));
  Check(arch.Text("schema") == "emuflow.xilinx-preplacement-timing-db/v1" &&
            arch.Text("family") == "UltraScale+" &&
            arch.Text("mapping_profile") == "xilinx-ultrascaleplus-open-v1",
        "Xilinx timing target/schema is invalid");
  Check(contract.Text("schema") == "emuflow.rapidwright-timing-contract/v1",
        "architecture source contract is invalid");
  D source(arch.Token("source")), expected(contract.Token("timing")),
      limitation(arch.Token("limitations"));
  Check(source.Text("provider") == "rapidwright-delay-model-v0.5" &&
            source.Text("revision") == expected.Text("source_revision") &&
            source.Text("qualification") == "analytical_uncharacterized",
        "Xilinx timing source identity is invalid");
  RequireEquivalentJson(db, source.Token("files_sha256"),
                        expected.Token("source_data_sha256"),
                        "Xilinx timing source file seals disagree");
  Check(limitation.Text("authoritative_setup") ==
            "Phase 7 routed timing plus global OpenSTA",
        "Xilinx timing limitations are invalid");
  D raw(arch.Token("delays_ps"));
  raw.Keys({"lut1", "lut2", "lut3", "lut4", "lut5", "lut6", "lut6_2", "muxf7",
            "muxf8", "muxf9", "carry8", "ff_setup", "ff_clock_to_q",
            "bram_setup", "bram_clock_to_q", "uram_setup", "uram_clock_to_q",
            "dsp48e2", "sink_interconnect"});
  std::map<std::string, double> delays;
  for (const auto &item : raw.Fields()) {
    const auto n = raw.Number(item.first);
    Check(n > 0, "Xilinx delay must be positive");
    delays[item.first] = n / 1000.;
  }
  return delays;
}
std::string Model(const std::map<std::string, ArtifactRef> &sources,
                  SqliteDatabase &db, const std::set<std::string> &used) {
  D base(D::Read(sources.at("timing-model")));
  Check(base.Text("schema") == "emuflow.fpga-timing-model/v1" ||
            base.Text("schema") == "emuflow.fpga-timing-model/v2",
        "unsupported timing model schema");
  Check(!base.Text("name").empty() && !base.Text("family").empty(),
        "empty timing model identity");
  D source(base.Token("source"));
  Check(std::set<std::string>{"analytical_uncharacterized",
                              "academic_open_model", "calibrated",
                              "characterized"}
            .count(source.Text("qualification")),
        "unsupported model qualification");
  auto fields = base.Fields();
  auto cells = D(base.Token("cells")).Fields();
  Check(!cells.empty() && cells.size() <= 4096,
        "primitive model exceeds bound");
  // Validate the base model before deriving any architecture specialization.
  (void)RenderOpenstaLiberty(base.text());
  if (sources.count("architecture-timing-db")) {
    const auto d = Delays(sources, db);
    const auto change = [&](const std::string &name, const O &values) {
      Check(cells.count(name), "base primitive is missing: " + name);
      auto f = D(cells.at(name)).Fields();
      for (const auto &v : values)
        f[v.first] = v.second;
      cells[name] = D::Encode(f);
    };
    const auto number = [](double x) { return JsonRecord(x).Encode(); };
    for (unsigned i = 1; i <= 6; ++i)
      change("LUT" + std::to_string(i),
             {{"delay_ns", number(d.at("lut" + std::to_string(i)) +
                                  d.at("sink_interconnect"))}});
    for (const std::string n : {"MUXF7", "MUXF8"})
      change(n, {{"delay_ns",
                  number(d.at(Lower(n)) + d.at("sink_interconnect"))}});
    for (const std::string n : {"FDCE", "FDPE", "FDRE", "FDSE"}) {
      change(n, {{"setup_ns",
                  number(d.at("ff_setup") + d.at("sink_interconnect"))},
                 {"clock_to_q_ns", number(d.at("ff_clock_to_q"))}});
      if (used.count(n + "__NEG")) {
        auto f = D(cells.at(n)).Fields();
        f["kind"] = Text("falling_edge_ff");
        cells[n + "__NEG"] = D::Encode(f);
      }
    }
    auto representatives = db.Prepare("SELECT type,MIN(seq) FROM cell_records "
                                      "GROUP BY type ORDER BY MIN(seq)");
    while (representatives.Step()) {
      const auto name = representatives.TypedText(0);
      if (cells.count(name))
        continue;
      const auto seq = representatives.TypedInteger(1);
      const auto in = ScalarPins(db, seq, 1), out = ScalarPins(db, seq, 0);
      Check(!in.empty() && !out.empty(),
            "Xilinx timing cell lacks input/output pins: " + name);
      if (std::set<std::string>{"LUT6_2", "MUXF9", "CARRY8", "DSP48E2"}.count(
              name))
        cells[name] =
            D::Encode({{"kind", Text("combinational")},
                       {"inputs", Array(in)},
                       {"outputs", Array(out)},
                       {"delay_ns", number(d.at(Lower(name)) +
                                           d.at("sink_interconnect"))}});
      else if (std::set<std::string>{"RAMB18E2", "RAMB36E2", "URAM288"}.count(
                   name)) {
        const auto first =
            std::find_if(in.begin(), in.end(), [](const auto &pin) {
              return Lower(pin).find("clk") != std::string::npos;
            });
        Check(first != in.end(), "hard block lacks clock: " + name);
        const auto clock = *first;
        std::vector<std::string> control, data;
        for (const auto &pin : in)
          if (pin != clock) {
            const auto s = Lower(pin);
            (s.find("clk") != std::string::npos ||
                     s.find("rst") != std::string::npos || s == "sleep"
                 ? control
                 : data)
                .push_back(pin);
          }
        Check(!data.empty(), "hard block lacks data: " + name);
        const std::string prefix = name == "URAM288" ? "uram" : "bram";
        cells[name] = D::Encode(
            {{"kind", Text("rising_edge_bank")},
             {"clock", Text(clock)},
             {"inputs", Array(data)},
             {"controls", Array(control)},
             {"outputs", Array(out)},
             {"setup_ns",
              number(d.at(prefix + "_setup") + d.at("sink_interconnect"))},
             {"clock_to_q_ns", number(d.at(prefix + "_clock_to_q"))}});
      } else
        Check(false,
              "RapidWright pre-placement TimingDB does not cover " + name);
      Check(cells.size() <= 4096, "primitive model exceeds bound");
    }
    D arch(D::Read(sources.at("architecture-timing-db"))),
        origin(arch.Token("source"));
    fields["name"] = Text("rapidwright-ultrascaleplus-preplacement-v1");
    fields["family"] = Text("xcup");
    fields["source"] = D::Encode(
        {{"provider", Text("rapidwright-delay-model-preplacement-v1")},
         {"qualification", Text("analytical_uncharacterized")},
         {"architecture_timing_db",
          Text(sources.at("architecture-timing-db").path())},
         {"rapidwright_revision", origin.Token("revision")},
         {"sink_interconnect_delay_ns", number(d.at("sink_interconnect"))},
         {"hard_block_timing", Text("nonzero-conservative-scalar")},
         {"hold_analysis", Text("unavailable")},
         {"authoritative_setup",
          Text("Phase 7 routed timing plus global OpenSTA")}});
  }
  std::size_t arcs = 0;
  for (const auto &entry : cells) {
    Check(!entry.first.empty() && entry.first.size() <= 1024,
          "primitive identity exceeds bound");
    D cell(entry.second);
    std::vector<std::string> names;
    for (const auto *f : {"inputs", "outputs", "controls"})
      if (cell.Has(f)) {
        auto a = Strings(cell.Token(f));
        names.insert(names.end(), a.begin(), a.end());
      }
    for (const auto *f : {"clock", "data", "output"})
      if (cell.Has(f))
        names.push_back(cell.Text(f));
    for (const auto &pin : names)
      Check(!pin.empty() && pin.size() <= 1024, "pin identity exceeds bound");
    if (cell.Text("kind") == "combinational")
      arcs += Strings(cell.Token("inputs")).size() *
              (cell.Has("outputs") ? Strings(cell.Token("outputs")).size() : 1);
    else
      arcs += names.size() + 1;
    Check(arcs <= 32768, "primitive model arcs exceed bound");
  }
  for (const auto &name : used)
    Check(cells.count(name),
          "OpenSTA timing model does not cover mapped primitives: " + name);
  fields["cells"] = D::Encode(cells);
  return D::Encode(fields);
}

} // namespace
TimingCellModel BuildTimingCellModel(SqliteDatabase &db,
                                     const ArtifactRef &base,
                                     const ArtifactRef *architecture,
                                     const ArtifactRef *contract) {
  Check(bool(architecture) == bool(contract),
        "architecture source coverage disagrees");
  std::map<std::string, ArtifactRef> sources{{"timing-model", base}};
  if (architecture) {
    sources.emplace("architecture-timing-db", *architecture);
    sources.emplace("architecture-contract", *contract);
  }
  auto used = UsedTypes(db, architecture != nullptr);
  auto json = Model(sources, db, used);
  for (const auto &source : sources)
    source.second.RequireUnchanged();
  return {std::move(json), std::move(used), architecture != nullptr};
}
} // namespace emuflow::core
