#include "physical/mapped_index.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/hash.hpp"
#include "ir/primitive_resources.hpp"
#include "ir/resource_view.hpp"
#include <algorithm>
#include <array>
#include <filesystem>
#include <limits>
#include <set>

namespace emuflow::core {
namespace {
using I = std::int64_t;
using Fields = std::map<std::string, std::string>;
void Check(bool ok, const std::string &why) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidArtifact,
                     "Xilinx mapped index: " + why);
}
void Stop(const volatile std::sig_atomic_t *flag) {
  if (flag && *flag)
    throw StageError(ErrorCode::kExecutionFailed,
                     "mapped input generation cancelled");
}
std::string Hash(const ArtifactRef &source,
                 const volatile std::sig_atomic_t *flag) {
  std::ifstream in(source.path(), std::ios::binary);
  Check(bool(in), "cannot hash source");
  Sha256 digest;
  std::array<char, 65536> block;
  while (in) {
    Stop(flag);
    in.read(block.data(), block.size());
    digest.update(reinterpret_cast<const unsigned char *>(block.data()),
                  in.gcount());
  }
  Check(in.eof(), "source hash failed");
  source.RequireUnchanged();
  std::string out;
  for (auto b : digest.finish()) {
    out += "0123456789abcdef"[b >> 4];
    out += "0123456789abcdef"[b & 15];
  }
  return out;
}
std::string Meta(SqliteDatabase &db, const std::string &key) {
  auto r = db.Prepare("SELECT value FROM source_emuir.metadata WHERE key=?");
  r.BindText(1, key);
  Check(r.Step(), "missing compact metadata " + key);
  auto out = r.TypedText(0);
  Check(out.size() <= 8 * 1024 * 1024 && !r.Step(), "invalid compact metadata");
  return out;
}
I Count(SqliteDatabase &db, const std::string &sql) {
  auto r = db.Prepare(sql);
  Check(r.Step(), "aggregate missing");
  return r.TypedInteger(0);
}
void Reject(SqliteDatabase &db, const std::string &sql,
            const std::string &why) {
  auto r = db.Prepare(sql);
  if (r.Step()) {
    std::string detail;
    if (!r.IsNull(0))
      detail = r.Text(0);
    throw StageError(ErrorCode::kInvalidArtifact,
                     "Xilinx mapped cell " + detail + " " + why);
  }
}
#include "physical/mapped_projection.inc"
std::string Raw(const std::string &value) {
  BoundedJson wrapped("{\"v\":" + value + "}");
  return wrapped.Token("v");
}
std::string Dict(const Fields &values) { return BoundedJson::Encode(values); }
I AddBit(I seq) {
  Check(seq >= 0 && seq <= std::numeric_limits<I>::max() - 3,
        "net ordinal is invalid");
  return seq + 2;
}
struct Pin {
  std::string value;
  int direction;
};
using Pins = std::map<std::string, std::map<I, Pin>>;
void PinValue(Pins &pins, const std::string &port, I bit,
              const std::string &value, int direction, const std::string &id) {
  Check(!port.empty() && bit >= 0 && bit < 1048576, "invalid primitive pin");
  auto &bits = pins[port];
  auto p = bits.emplace(bit, Pin{value, direction});
  Check(p.second || p.first->second.value == value,
        "cell " + id + " has conflicting constant and net");
  if (!p.second && p.first->second.direction != direction)
    p.first->second.direction = 2;
}
std::string Binary(std::string value, I width) {
  Check(!value.empty() && value.find_first_not_of("01") == std::string::npos,
        "invalid LUT truth table");
  auto begin = value.find_first_not_of('0');
  value = begin == std::string::npos ? "0" : value.substr(begin);
  if (value.size() < std::size_t(I(1) << width))
    value.insert(0, (I(1) << width) - value.size(), '0');
  return value;
}
I BinaryWidth(const std::string &text) {
  Check(!text.empty() && text.size() <= 64 &&
            text.find_first_not_of("01") == std::string::npos,
        "generic LUT lacks WIDTH/LUT parameters");
  I v = 0;
  for (char c : text) {
    Check(v <= 6, "unsupported LUT width");
    v = 2 * v + c - '0';
  }
  Check(v >= 1 && v <= 6, "unsupported LUT width");
  return v;
}
void MixedProjection(SqliteDatabase &db, const std::set<std::string> &native,
                     const volatile std::sig_atomic_t *flag) {
  auto cells = db.Prepare(
      "SELECT "
      "c.seq,c.id,c.type,coalesce(p.value,'{}'),coalesce(a.value,'{}'),k.value "
      "FROM source_emuir.cell_records c LEFT JOIN source_emuir.payloads p ON "
      "p.seq=c.parameters_seq LEFT JOIN source_emuir.payloads a ON "
      "a.seq=c.attributes_seq JOIN source_emuir.payloads k ON "
      "k.seq=c.constants_seq ORDER BY c.seq");
  auto endpoints =
      db.Prepare("SELECT e.instance_seq,e.net_seq,p.name,e.bit,e.role FROM "
                 "source_emuir.endpoint_records e JOIN source_emuir.pin_names "
                 "p ON p.seq=e.port_seq WHERE e.instance_seq IS NOT NULL ORDER "
                 "BY e.instance_seq,e.role,e.ordinal");
  bool has = endpoints.Step();
  auto insert = db.Prepare("INSERT INTO cells VALUES(?,?,?,?)"),
       pin = db.Prepare("INSERT INTO cell_pins VALUES(?,?,?,?,?,?,?,?,?)");
  while (cells.Step()) {
    Stop(flag);
    const auto seq = cells.TypedInteger(0);
    const auto id = cells.TypedText(1);
    auto type = cells.TypedText(2), params = cells.TypedText(3);
    const auto attrs = cells.TypedText(4);
    auto constants = BoundedJson::Array(cells.TypedText(5), 1048576);
    I width = 0;
    std::string in, out;
    bool dff = false;
    if (!native.count(type)) {
      if (type == "$lut" || type == "$_LUT_") {
        BoundedJson p(params);
        width = BinaryWidth(p.Token("WIDTH").front() == '"' ? p.Text("WIDTH")
                                                            : p.Token("WIDTH"));
        auto truth = p.Has("LUT") ? p.Token("LUT") : p.Token("INIT");
        if (truth.front() == '"')
          truth = DecodeJsonString(truth);
        params = Dict({{"INIT", JsonQuote(Binary(truth, width))}});
        type = "LUT" + std::to_string(width);
        in = "A";
        out = "Y";
      } else if (type == "$_DFF_P_" || type == "$_DFF_N_") {
        dff = true;
        params = Dict(
            {{"INIT", JsonQuote("0")},
             {"IS_C_INVERTED", JsonQuote(type == "$_DFF_N_" ? "1" : "0")}});
        type = "FDRE";
        constants.push_back("{\"port\":\"CE\",\"bit\":0,\"value\":\"1\"}");
        constants.push_back("{\"port\":\"R\",\"bit\":0,\"value\":\"0\"}");
      } else
        Check(false, "does not yet lower mapped primitive type " + type);
    }
    Check(native.count(type), "unsupported Xilinx primitive " + type);
    auto remap = [&](std::string port, I bit) {
      if (width) {
        if (port == in && bit < width)
          return std::pair<std::string, I>{"I" + std::to_string(bit), 0};
        if (port == out && bit == 0)
          return std::pair<std::string, I>{"O", 0};
      }
      return std::pair<std::string, I>{port, bit};
    };
    Pins pins;
    while (has && endpoints.TypedInteger(0) == seq) {
      auto p = remap(endpoints.TypedText(2), endpoints.TypedInteger(3));
      PinValue(pins, p.first, p.second,
               std::to_string(AddBit(endpoints.TypedInteger(1))),
               1 - int(endpoints.TypedInteger(4)), id);
      has = endpoints.Step();
    }
    Check(!has || endpoints.TypedInteger(0) > seq,
          "endpoint stream is orphaned");
    for (const auto &raw : constants) {
      BoundedJson c(raw);
      auto p = remap(c.Text("port"), c.Integer("bit"));
      auto token = JsonQuote(c.Text("value"));
      auto existing = pins[p.first].find(p.second);
      if (existing == pins[p.first].end())
        PinValue(pins, p.first, p.second, token, 0, id);
      else
        Check(existing->second.value == token,
              "cell " + id + " has conflicting constant and net");
    }
    auto record = Dict({{"attributes", Raw(attrs)},
                        {"hide_name", "0"},
                        {"parameters", Raw(params)},
                        {"type", JsonQuote(type)}});
    insert.BindText(1, id);
    insert.BindInteger(2, seq);
    insert.BindText(3, type);
    insert.BindText(4, record);
    insert.Execute();
    for (const auto &port : pins) {
      const auto size = I(port.second.size());
      Check(!port.second.empty() && port.second.begin()->first == 0 &&
                port.second.rbegin()->first + 1 == size,
            "cell " + id + " is not contiguous");
      int direction = port.second.begin()->second.direction;
      for (const auto &p : port.second)
        if (p.second.direction != direction)
          direction = 2;
      for (const auto &p : port.second) {
        pin.BindInteger(1, seq);
        pin.BindText(2, id);
        pin.BindText(3, port.first);
        pin.BindInteger(4, p.first);
        pin.BindInteger(5, size);
        pin.BindInteger(6, direction);
        bool constant = p.second.value.front() == '"';
        pin.BindInteger(7, constant ? 1 : 0);
        if (constant) {
          pin.BindNull(8);
          pin.BindText(9, DecodeJsonString(p.second.value));
        } else {
          pin.BindInteger(8, BoundedJson::IntegerToken(p.second.value));
          pin.BindNull(9);
        }
        pin.Execute();
      }
    }
  }
  Check(!has, "endpoint stream has unmatched cells");
}
const char *ddl = R"SQL(
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE cells(id TEXT PRIMARY KEY,seq INTEGER NOT NULL UNIQUE,type TEXT NOT NULL,record_json TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE cell_pins(cell_seq INTEGER NOT NULL,cell_id TEXT NOT NULL,port TEXT NOT NULL,port_bit INTEGER NOT NULL,port_width INTEGER NOT NULL,direction INTEGER NOT NULL CHECK(direction BETWEEN 0 AND 2),value_kind INTEGER NOT NULL CHECK(value_kind IN (0,1)),net_bit INTEGER,constant TEXT,PRIMARY KEY(cell_id,port,port_bit),CHECK((value_kind=0 AND net_bit IS NOT NULL AND constant IS NULL) OR(value_kind=1 AND net_bit IS NULL AND constant IS NOT NULL))) WITHOUT ROWID;
CREATE TABLE netnames(id TEXT PRIMARY KEY,seq INTEGER NOT NULL UNIQUE,record_json TEXT NOT NULL) WITHOUT ROWID;
)SQL";
} // namespace
StageResult BuildMappedIndex(const std::vector<ArtifactRef> &inputs,
                             const std::string &output,
                             const std::string &summary,
                             const volatile std::sig_atomic_t *flag) {
  return RunStage(
      "physical.build-mapped-index",
      [&] {
        Check(inputs.size() == 3, "source coverage disagrees");
        return inputs;
      },
      [&](const auto &refs) {
        CheckedRequestReader request(refs[2].path(), 4096);
        Check(request.Read<std::uint64_t>() == 0x31584d50464d5545ULL &&
                  request.Read<std::uint32_t>() == 1,
              "request version disagrees");
        for (unsigned i = 0; i < 2; ++i)
          if (!(ReadFileGeneration(request) == refs[i].generation()))
            throw StageError(ErrorCode::kMixedGeneration,
                             "mapped input generation changed");
        request.RequireEnd();
        Check(refs[0].schema() == "emuir-compact-v2" &&
                  refs[1].schema() == "physical-input-document-v1",
              "source roles disagree");
        Check(std::filesystem::weakly_canonical(output) !=
                  std::filesystem::weakly_canonical(summary),
              "summary aliases mapped output");
        BoundedJson library(BoundedJson::Read(refs[1]));
        Check(library.Text("schema") ==
                      "emuflow.xilinx-primitive-library-db/v1" &&
                  library.Text("mapping_profile") ==
                      "xilinx-ultrascaleplus-open-v1",
              "primitive library identity disagrees");
        Check(library.Text("family") == "UltraScale+" &&
                  library.Text("unknown_cell_policy") == "fail",
              "primitive library policy disagrees");
        BoundedJson contracts(library.Token("cells"));
        std::set<std::string> native;
        for (const auto &c : contracts.Fields()) {
          Check(!c.first.empty(), "empty primitive identity");
          BoundedJson contract(c.second);
          Check(!contract.Text("category").empty(),
                "invalid primitive category");
          contract.Boolean("hard_block");
          contract.Boolean("placement_required");
          std::array<I, 12> amounts{};
          BoundedJson resources(contract.Token("resources"));
          for (const auto &field : resources.Fields()) {
            auto found = std::find_if(
                kResourceFields.begin(), kResourceFields.end(),
                [&](const char *key) { return field.first == key; });
            Check(found != kResourceFields.end(), "unknown primitive resource");
            auto amount = BoundedJson::IntegerToken(field.second);
            Check(amount >= 0, "invalid primitive resource count");
            amounts[found - kResourceFields.begin()] = amount;
          }
          Check(amounts == ClassifyPrimitive(c.first),
                "primitive resource contract disagrees with EmuIR");
          native.insert(c.first);
        }
        Check(!native.empty() && native.size() <= 4096,
              "primitive library population invalid");
        I cells = 0, nets = 0, ports = 0, dead_in = 0, dead_out = 0;
        std::string top, inventory, hard, resources;
        bool native_path = true;
        SqliteArtifactWriter writer(output, "xilinx-mapped-index-v2", refs);
        {
          auto &db = writer.database();
          db.Execute("PRAGMA temp_store=FILE;PRAGMA cache_size=-131072");
          auto attach = db.Prepare("ATTACH DATABASE ? AS source_emuir");
          attach.BindText(1, SqliteImmutableUri(refs[0].path()));
          attach.Execute();
          SqliteTransaction transaction(db);
          db.Execute(ddl);
          CheckSource(db);
          BoundedJson design(Meta(db, "design_json"));
          top = design.Text("top");
          Check(!top.empty(), "top is empty");
          db.Execute("CREATE TEMP TABLE top_pins(port TEXT,bit INTEGER,net_seq "
                     "INTEGER,PRIMARY KEY(port,bit)) WITHOUT ROWID");
          Reject(db,
                 "SELECT p.name FROM source_emuir.endpoint_records e JOIN "
                 "source_emuir.pin_names p ON p.seq=e.port_seq WHERE "
                 "e.instance_seq IS NULL GROUP BY p.name,e.bit HAVING "
                 "min(e.net_seq)!=max(e.net_seq) LIMIT 1",
                 "top pin has multiple nets");
          db.Execute("INSERT INTO top_pins SELECT p.name,e.bit,min(e.net_seq) "
                     "FROM source_emuir.endpoint_records e JOIN "
                     "source_emuir.pin_names p ON p.seq=e.port_seq WHERE "
                     "e.instance_seq IS NULL GROUP BY p.name,e.bit");
          I next = Count(
              db,
              "SELECT coalesce(max(seq)+3,2) FROM source_emuir.net_records");
          Check(next >= 2, "net ordinal overflows");
          Fields top_ports;
          auto rows = db.Prepare(
              "SELECT record_json FROM source_emuir.port_records ORDER BY seq");
          auto bit =
              db.Prepare("SELECT net_seq FROM top_pins WHERE port=? AND bit=?");
          while (rows.Step()) {
            Stop(flag);
            BoundedJson p(rows.TypedText(0));
            auto name = p.Text("id"), direction = p.Text("direction");
            Check(direction == "input" || direction == "output" ||
                      direction == "inout",
                  "invalid top port direction");
            auto width = p.Integer("width");
            Check(width > 0 && width <= 1048576, "top port width is invalid");
            std::map<I, std::string> constants;
            if (p.Has("constant_connections"))
              for (const auto &raw : BoundedJson::Array(
                       p.Token("constant_connections"), 1048576)) {
                BoundedJson c(raw);
                auto pos = c.Integer("bit");
                Check(
                    constants.emplace(pos, JsonQuote(c.Text("value"))).second &&
                        pos >= 0 && pos < width,
                    "top constant repeats or is out of range");
              }
            std::string bits = "[";
            for (I i = 0; i < width; ++i) {
              if (i)
                bits += ',';
              bit.BindText(1, name);
              bit.BindInteger(2, i);
              if (bit.Step())
                bits += std::to_string(AddBit(bit.TypedInteger(0)));
              else if (constants.count(i))
                bits += constants.at(i);
              else if (direction == "output") {
                bits += JsonQuote("x");
                ++dead_out;
              } else if (direction == "input") {
                Check(next < std::numeric_limits<I>::max(),
                      "dead input identity overflows");
                bits += std::to_string(next++);
                ++dead_in;
              } else
                Check(false, "unconnected top pin");
              bit.Reset();
            }
            bits += ']';
            Check(
                top_ports
                    .emplace(name, Dict({{"bits", bits},
                                         {"direction", JsonQuote(direction)}}))
                    .second,
                "duplicate top port");
            ++ports;
          }
          auto meta = db.Prepare("INSERT INTO metadata VALUES(?,?)");
          for (const auto &item :
               Fields{{"schema", "emuflow.xilinx-mapped-index/v2"},
                      {"top_json", JsonQuote(top)},
                      {"creator_json",
                       JsonQuote("EmuFlow Xilinx mapped-netlist exporter")},
                      {"ports_json", Dict(top_ports)}}) {
            meta.BindText(1, item.first);
            meta.BindText(2, item.second);
            meta.Execute();
          }
          auto types =
              db.Prepare("SELECT DISTINCT type FROM source_emuir.cell_records");
          while (types.Step())
            native_path &= native.count(types.TypedText(0)) != 0;
          if (native_path)
            NativeProjection(db);
          else
            MixedProjection(db, native, flag);
          CheckPins(db);
          AuditProjection(db);
          db.Execute(
              "INSERT INTO netnames SELECT "
              "id,seq,'{\"attributes\":{},\"bits\":['||(seq+2)||'],\"hide_"
              "name\":0}' FROM source_emuir.net_records ORDER BY seq");
          cells = Count(db, "SELECT count(*) FROM cells");
          nets = Count(db, "SELECT count(*) FROM netnames");
          Check(
              cells ==
                      Count(db,
                            "SELECT count(*) FROM source_emuir.cell_records") &&
                  nets ==
                      Count(db,
                            "SELECT count(*) FROM source_emuir.net_records"),
              "mapped index coverage is incomplete");
          Fields counts, hard_counts;
          std::map<std::string, I> totals;
          auto counts_rows = db.Prepare(
              "SELECT type,count(*) FROM cells GROUP BY type ORDER BY type");
          while (counts_rows.Step()) {
            const auto type = counts_rows.TypedText(0);
            Check(native.count(type), "unsupported Xilinx primitive " + type);
            const auto n = counts_rows.TypedInteger(1);
            counts[type] = std::to_string(n);
            BoundedJson c(contracts.Token(type));
            if (c.Boolean("hard_block"))
              hard_counts[type] = std::to_string(n);
            BoundedJson r(c.Token("resources"));
            for (const auto &f : r.Fields()) {
              const auto amount = BoundedJson::IntegerToken(f.second);
              Check(
                  amount >= 0 &&
                      (!amount || n <= std::numeric_limits<I>::max() / amount),
                  "resource count overflow");
              auto &total = totals[f.first];
              Check(total <= std::numeric_limits<I>::max() - amount * n,
                    "resource total overflow");
              total += amount * n;
            }
          }
          Fields resource_fields;
          for (const auto &f : totals)
            if (f.second)
              resource_fields[f.first] = std::to_string(f.second);
          inventory = Dict(counts);
          hard = Dict(hard_counts);
          resources = Dict(resource_fields);
          db.Execute(
              "CREATE INDEX cell_pins_by_seq ON "
              "cell_pins(cell_seq,port,port_bit);CREATE INDEX cell_pins_by_net "
              "ON cell_pins(net_bit,direction,cell_id,port,port_bit) WHERE "
              "value_kind=0");
          transaction.Commit();
          Stop(flag);
        }
        const auto published = writer.Commit();
        const auto digest = Hash(published, flag);
        auto audit = Dict(
            {{"status", JsonQuote("pass")},
             {"schema", JsonQuote("emuflow.xilinx-primitive-audit/v1")},
             {"mapping_profile", JsonQuote("xilinx-ultrascaleplus-open-v1")},
             {"top", JsonQuote(top)},
             {"instances", std::to_string(cells)},
             {"cell_types", inventory},
             {"hard_block_cells", hard},
             {"resource_totals", resources},
             {"unknown_cell_types", "{}"},
             {"external_macros", "{}"},
             {"mapped_sha256", JsonQuote(digest)},
             {"mapped_json_sha256", "null"},
             {"mapped_representation", JsonQuote("sqlite-index")},
             {"primitive_library_sha256", JsonQuote(Hash(refs[1], flag))}});
        const auto report = Dict(
            {{"schema", JsonQuote("emuflow.xilinx-mapped-netlist-report/v1")},
             {"status", JsonQuote("pass")},
             {"provider", JsonQuote("emuflow-emuir-to-xilinx-sqlite-index-v2")},
             {"mapping_profile", JsonQuote("xilinx-ultrascaleplus-open-v1")},
             {"implementation_id", JsonQuote("native-xilinx-mapped-index-v1")},
             {"top", JsonQuote(top)},
             {"cells", std::to_string(cells)},
             {"nets", std::to_string(nets)},
             {"ports", std::to_string(ports)},
             {"unconnected_input_bits", std::to_string(dead_in)},
             {"unconnected_output_bits", std::to_string(dead_out)},
             {"output_sha256", JsonQuote(digest)},
             {"index", JsonQuote(published.path())},
             {"index_sha256", JsonQuote(digest)},
             {"index_builder",
              JsonQuote(native_path ? "native-primitive-projection-v1"
                                    : "native-primitive-lowering-v1")},
             {"audit", audit},
             {"output", JsonQuote(published.path())}});
        auto sources = refs;
        sources.push_back(published);
        PublishTextArtifact(summary, sources,
                            [&](std::ostream &out) { out << report << '\n'; });
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(output, "xilinx-mapped-index-v2"),
            ArtifactRef::Bind(summary, "xilinx-mapped-index-report-v1")};
      });
}
} // namespace emuflow::core
