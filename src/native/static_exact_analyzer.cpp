#include <algorithm>
#include <array>
#include <atomic>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <memory>
#include <queue>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#if defined(_WIN32)
#include <process.h>
#else
#include <unistd.h>
#endif

#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/native_stages.hpp"
#include "emuflow_core/sqlite.hpp"

namespace {

constexpr std::uint64_t kInputMagic = 0x31584553464d5545ULL;
constexpr std::uint64_t kOutputMagic = 0x315845534f4d5545ULL;
constexpr std::uint32_t kVersion = 3;

struct Capture {
  std::uint32_t cut = 0;
  std::uint32_t part = 0;
  std::uint32_t net = 0;
  std::uint32_t sink = 0;
};

static_assert(sizeof(Capture) == 4 * sizeof(std::uint32_t),
              "capture spool record must be packed");
static_assert(std::is_trivially_copyable<Capture>::value,
              "capture spool record must be trivially copyable");

bool CaptureLess(const Capture& left, const Capture& right) {
  if (left.net != right.net) return left.net < right.net;
  if (left.sink != right.sink) return left.sink < right.sink;
  if (left.cut != right.cut) return left.cut < right.cut;
  return left.part < right.part;
}

bool CaptureEqual(const Capture& left, const Capture& right) {
  return left.cut == right.cut && left.part == right.part &&
         left.net == right.net && left.sink == right.sink;
}

std::uint64_t ProcessId() {
#if defined(_WIN32)
  return static_cast<std::uint64_t>(_getpid());
#else
  return static_cast<std::uint64_t>(getpid());
#endif
}

// Capture discovery walks one cut cone at a time, whereas protocol v3 is
// ordered by authoritative (net, sink, cut, part) coordinates.  Keeping that
// complete population in a vector made the analyzer retain 16 bytes per raw
// capture and sort all records at once.  This private external sorter bounds
// the resident population, deduplicates each run early, and performs a
// bounded-fan-in merge before the atomic result is published.
class CaptureSorter {
 public:
  explicit CaptureSorter(const std::string& output_path) {
    const auto sequence = sequence_.fetch_add(1);
    prefix_ = output_path + ".capture-sort." + std::to_string(ProcessId()) +
              "." + std::to_string(sequence);
    buffer_.reserve(kChunkRecords);
  }

  ~CaptureSorter() {
    for (const auto& path : owned_paths_) {
      std::error_code error;
      std::filesystem::remove(path, error);
    }
  }

  CaptureSorter(const CaptureSorter&) = delete;
  CaptureSorter& operator=(const CaptureSorter&) = delete;

  void Add(const Capture& capture) {
    if (finished_) throw std::runtime_error("capture sorter is finalized");
    buffer_.push_back(capture);
    if (buffer_.size() == kChunkRecords) FlushRun();
  }

  std::uint64_t Finish() {
    if (finished_) return count_;
    if (runs_.empty()) {
      SortUniqueBuffer();
      count_ = buffer_.size();
      finished_ = true;
      return count_;
    }
    FlushRun();
    while (runs_.size() > 1) {
      std::vector<std::string> next;
      next.reserve((runs_.size() + kMergeFanIn - 1) / kMergeFanIn);
      for (std::size_t begin = 0; begin < runs_.size();
           begin += kMergeFanIn) {
        const auto end = std::min(runs_.size(), begin + kMergeFanIn);
        if (end - begin == 1) {
          next.push_back(runs_[begin]);
        } else {
          next.push_back(MergeRuns(begin, end));
        }
      }
      runs_ = std::move(next);
    }
    if (!runs_.empty()) {
      final_path_ = runs_.front();
      std::error_code error;
      const auto bytes = std::filesystem::file_size(final_path_, error);
      if (error || bytes % sizeof(Capture) != 0) {
        throw std::runtime_error("capture sort output is invalid");
      }
      count_ = bytes / sizeof(Capture);
    }
    finished_ = true;
    return count_;
  }

  void WriteTo(emuflow::core::AtomicBinaryWriter& output) {
    Finish();
    if (final_path_.empty()) {
      output.WriteBytes(buffer_.data(), buffer_.size() * sizeof(Capture));
      return;
    }
    std::ifstream stream(final_path_, std::ios::binary);
    if (!stream) throw std::runtime_error("cannot read capture sort output");
    std::vector<Capture> records(kCopyRecords);
    std::uint64_t observed = 0;
    while (stream) {
      stream.read(reinterpret_cast<char*>(records.data()),
                  static_cast<std::streamsize>(records.size() *
                                               sizeof(Capture)));
      const auto bytes = stream.gcount();
      if (bytes < 0 || bytes % static_cast<std::streamsize>(sizeof(Capture)) !=
                           0) {
        throw std::runtime_error("capture sort output is truncated");
      }
      if (bytes == 0) break;
      output.WriteBytes(records.data(), static_cast<std::size_t>(bytes));
      observed += static_cast<std::uint64_t>(bytes) / sizeof(Capture);
    }
    if ((!stream.eof() && stream.fail()) || observed != count_) {
      throw std::runtime_error("cannot read capture sort output");
    }
  }

 private:
  struct MergeItem {
    Capture capture;
    std::size_t stream = 0;
  };

  struct MergeGreater {
    bool operator()(const MergeItem& left, const MergeItem& right) const {
      return CaptureLess(right.capture, left.capture);
    }
  };

  static constexpr std::size_t kChunkRecords = 1U << 20;
  static constexpr std::size_t kMergeFanIn = 64;
  static constexpr std::size_t kCopyRecords = 1U << 15;
  static std::atomic<std::uint64_t> sequence_;

  std::string NewPath() {
    const auto path = prefix_ + "." + std::to_string(next_path_++);
    owned_paths_.push_back(path);
    return path;
  }

  static bool ReadCapture(std::ifstream& stream, Capture& capture) {
    stream.read(reinterpret_cast<char*>(&capture), sizeof(Capture));
    if (stream.gcount() == 0 && stream.eof()) return false;
    if (!stream || stream.gcount() != sizeof(Capture)) {
      throw std::runtime_error("capture sort run is truncated");
    }
    return true;
  }

  static void CloseOutput(std::ofstream& stream) {
    stream.flush();
    if (!stream) throw std::runtime_error("cannot write capture sort run");
    stream.close();
    if (!stream) throw std::runtime_error("cannot close capture sort run");
  }

  void SortUniqueBuffer() {
    std::sort(buffer_.begin(), buffer_.end(), CaptureLess);
    buffer_.erase(std::unique(buffer_.begin(), buffer_.end(), CaptureEqual),
                  buffer_.end());
  }

  void FlushRun() {
    if (buffer_.empty()) return;
    SortUniqueBuffer();
    const auto path = NewPath();
    std::ofstream stream(path, std::ios::binary | std::ios::trunc);
    if (!stream) throw std::runtime_error("cannot create capture sort run");
    stream.write(reinterpret_cast<const char*>(buffer_.data()),
                 static_cast<std::streamsize>(buffer_.size() *
                                              sizeof(Capture)));
    CloseOutput(stream);
    runs_.push_back(path);
    buffer_.clear();
  }

  std::string MergeRuns(std::size_t begin, std::size_t end) {
    std::vector<std::unique_ptr<std::ifstream>> inputs;
    inputs.reserve(end - begin);
    std::priority_queue<MergeItem, std::vector<MergeItem>, MergeGreater> queue;
    for (auto index = begin; index < end; ++index) {
      auto stream = std::make_unique<std::ifstream>(runs_[index],
                                                    std::ios::binary);
      if (!*stream) throw std::runtime_error("cannot read capture sort run");
      Capture capture;
      if (ReadCapture(*stream, capture)) {
        queue.push({capture, inputs.size()});
      }
      inputs.push_back(std::move(stream));
    }
    const auto output_path = NewPath();
    std::ofstream output(output_path, std::ios::binary | std::ios::trunc);
    if (!output) throw std::runtime_error("cannot create capture sort run");
    Capture previous;
    bool have_previous = false;
    while (!queue.empty()) {
      const auto item = queue.top();
      queue.pop();
      if (!have_previous || !CaptureEqual(previous, item.capture)) {
        output.write(reinterpret_cast<const char*>(&item.capture),
                     sizeof(Capture));
        if (!output) throw std::runtime_error("cannot write capture sort run");
        previous = item.capture;
        have_previous = true;
      }
      Capture next;
      if (ReadCapture(*inputs[item.stream], next)) {
        queue.push({next, item.stream});
      }
    }
    CloseOutput(output);
    for (auto index = begin; index < end; ++index) {
      std::error_code error;
      std::filesystem::remove(runs_[index], error);
    }
    return output_path;
  }

  std::string prefix_;
  std::vector<Capture> buffer_;
  std::vector<std::string> runs_;
  std::vector<std::string> owned_paths_;
  std::string final_path_;
  std::size_t next_path_ = 0;
  std::uint64_t count_ = 0;
  bool finished_ = false;
};

std::atomic<std::uint64_t> CaptureSorter::sequence_{0};

struct EndpointRef {
  std::uint32_t net = 0;
  std::uint32_t sink = 0;
};

struct CsrIndex {
  std::vector<std::uint32_t> offsets;
  std::vector<std::uint32_t> values;
};

class GenerationMarks {
 public:
  explicit GenerationMarks(std::size_t size) : marks_(size, 0) {}

  void Next() {
    ++generation_;
    if (generation_ == 0) {
      std::fill(marks_.begin(), marks_.end(), 0);
      generation_ = 1;
    }
  }

  bool Insert(std::uint32_t index) {
    if (marks_[index] == generation_) return false;
    marks_[index] = generation_;
    return true;
  }

 private:
  std::vector<std::uint32_t> marks_;
  std::uint32_t generation_ = 0;
};

void RequireCount(std::uint64_t count, std::size_t minimum_bytes,
                  std::size_t remaining, const char* context) {
  if (minimum_bytes == 0 || count > remaining / minimum_bytes) {
    throw std::runtime_error(std::string(context) + " count is invalid");
  }
}

CsrIndex BuildInstanceNetIndex(
    std::uint32_t instance_count, std::uint32_t net_count,
    const std::vector<std::uint32_t>& net_offsets,
    const std::vector<std::int32_t>& endpoints, const char* context) {
  std::vector<std::uint32_t> counts(instance_count, 0);
  for (const auto endpoint : endpoints) {
    if (endpoint < 0) continue;
    auto& count = counts[static_cast<std::uint32_t>(endpoint)];
    if (count == std::numeric_limits<std::uint32_t>::max()) {
      throw std::runtime_error(std::string(context) + " fanout is invalid");
    }
    ++count;
  }
  CsrIndex result;
  result.offsets.resize(static_cast<std::size_t>(instance_count) + 1, 0);
  std::uint64_t total = 0;
  for (std::uint32_t index = 0; index < instance_count; ++index) {
    total += counts[index];
    if (total > std::numeric_limits<std::uint32_t>::max()) {
      throw std::runtime_error(std::string(context) + " count is invalid");
    }
    result.offsets[index + 1] = static_cast<std::uint32_t>(total);
  }
  result.values.resize(static_cast<std::size_t>(total));
  auto positions = result.offsets;
  for (std::uint32_t net = 0; net < net_count; ++net) {
    for (auto offset = net_offsets[net]; offset < net_offsets[net + 1];
         ++offset) {
      const auto endpoint = endpoints[offset];
      if (endpoint >= 0) {
        result.values[positions[static_cast<std::uint32_t>(endpoint)]++] = net;
      }
    }
  }
  return result;
}

bool CutContainsSink(std::uint32_t cut, std::uint32_t part,
                     const std::vector<std::uint32_t>& offsets,
                     const std::vector<std::uint32_t>& sink_parts) {
  return std::binary_search(sink_parts.begin() + offsets[cut],
                            sink_parts.begin() + offsets[cut + 1], part);
}

void AnalyzeImpl(const std::string& input_path, const std::string& output_path,
                 const std::string& indexed_path) {
  emuflow::core::BinaryReader reader(input_path);
  const bool indexed = !indexed_path.empty();
  const emuflow::core::ArtifactSchema schema{
      kInputMagic, indexed ? 4U : kVersion, "Static Exact analyzer input"};
  emuflow::core::ReadAndValidateHeader(reader, schema);
  const auto instance_count = reader.Read<std::uint32_t>();
  const auto net_count = reader.Read<std::uint32_t>();
  const auto cut_count = reader.Read<std::uint32_t>();
  const auto part_count = reader.Read<std::uint32_t>();
  const auto endpoint_count = reader.Read<std::uint32_t>();
  if (part_count == 0) throw std::runtime_error("native input has no parts");
  RequireCount(instance_count,
               (indexed ? 0 : sizeof(std::uint8_t)) + sizeof(std::uint32_t),
               reader.remaining(), "native instance");
  if (!indexed) {
    RequireCount(net_count, 2 * sizeof(std::uint32_t), reader.remaining(),
                 "native net");
  }
  RequireCount(cut_count, 3 * sizeof(std::uint32_t), reader.remaining(),
               "native cut");
  if (!indexed) {
    RequireCount(endpoint_count, sizeof(std::int32_t), reader.remaining(),
                 "native endpoint");
  }

  std::vector<std::uint8_t> instance_states(instance_count, 0);
  std::vector<std::uint32_t> instance_parts(instance_count, 0);
  for (std::uint32_t index = 0; index < instance_count; ++index) {
    const auto state = indexed ? 0 : reader.Read<std::uint8_t>();
    const auto part = reader.Read<std::uint32_t>();
    if (state > 1 || part >= part_count) {
      throw std::runtime_error("native instance record is invalid");
    }
    instance_states[index] = state;
    instance_parts[index] = part;
  }

  if (!indexed) {
    RequireCount(net_count, 2 * sizeof(std::uint32_t), reader.remaining(),
                 "native net");
  }
  std::vector<std::uint32_t> net_driver_offsets;
  std::vector<std::int32_t> net_drivers;
  std::vector<std::uint32_t> net_sink_offsets;
  std::vector<std::int32_t> net_sinks;
  net_driver_offsets.reserve(static_cast<std::size_t>(net_count) + 1);
  net_sink_offsets.reserve(static_cast<std::size_t>(net_count) + 1);
  net_sinks.reserve(endpoint_count);
  net_driver_offsets.push_back(0);
  net_sink_offsets.push_back(0);
  std::uint64_t observed_endpoints = 0;
  if (indexed) {
    emuflow::core::SqliteDatabase database(
        indexed_path, emuflow::core::SqliteDatabase::Mode::kReadOnly);
    auto application = database.Prepare("PRAGMA application_id");
    auto version = database.Prepare("PRAGMA user_version");
    if (!application.Step() || application.Integer(0) != 0x454D5549 ||
        !version.Step() || version.Integer(0) != 2) {
      throw std::runtime_error("native compact EmuIR envelope is invalid");
    }
    auto storage = database.Prepare(
        "SELECT value FROM metadata WHERE key='storage_schema'");
    auto counts = database.Prepare(
        "SELECT json_extract(value,'$.cell_records'),"
        "json_extract(value,'$.net_records'),"
        "json_extract(value,'$.endpoint_records') "
        "FROM metadata WHERE key='record_counts_json'");
    if (!storage.Step() || storage.Text(0) != "emuflow.emuir-sqlite/v2" ||
        !counts.Step() || counts.Integer(0) != instance_count ||
        counts.Integer(1) != net_count || counts.Integer(2) < endpoint_count) {
      throw std::runtime_error("native compact EmuIR identity/counts disagree");
    }
    const auto expected_total_endpoints = counts.Integer(2);
    auto cells = database.Prepare(
        "SELECT c.seq,json_type(p.value),"
        "coalesce(json_extract(p.value,'$.ff'),0)!=0 OR "
        "coalesce(json_extract(p.value,'$.bram'),0)!=0 OR "
        "coalesce(json_extract(p.value,'$.bram18k'),0)!=0 OR "
        "coalesce(json_extract(p.value,'$.uram288'),0)!=0 "
        "FROM cell_records c JOIN payloads p ON p.seq=c.resources_seq "
        "ORDER BY c.seq");
    std::uint64_t observed_cells = 0;
    while (cells.Step()) {
      if (observed_cells >= instance_count ||
          cells.Integer(0) != static_cast<std::int64_t>(observed_cells) ||
          cells.Text(1) != "object") {
        throw std::runtime_error("native compact EmuIR cell sequence is invalid");
      }
      instance_states[observed_cells++] = cells.Integer(2) != 0;
    }
    if (observed_cells != instance_count) {
      throw std::runtime_error("native compact EmuIR cell count disagrees");
    }
    auto nets = database.Prepare("SELECT seq FROM net_records ORDER BY seq");
    auto endpoints = database.Prepare(
        "SELECT net_seq,role,ordinal,coalesce(instance_seq,-1),"
        "typeof(net_seq)='integer' AND typeof(role)='integer' AND "
        "typeof(ordinal)='integer' AND (instance_seq IS NULL OR "
        "(typeof(instance_seq)='integer' AND instance_seq>=0)) "
        "FROM endpoint_records ORDER BY net_seq,role,ordinal");
    bool have_endpoint = endpoints.Step();
    std::uint64_t observed_nets = 0;
    std::int64_t total_endpoints = 0;
    while (nets.Step()) {
      if (observed_nets >= net_count ||
          nets.Integer(0) != static_cast<std::int64_t>(observed_nets)) {
        throw std::runtime_error("native compact EmuIR net sequence is invalid");
      }
      std::int64_t previous_role = -1, previous_ordinal = -1;
      while (have_endpoint && endpoints.Integer(0) ==
                                  static_cast<std::int64_t>(observed_nets)) {
        const auto role = endpoints.Integer(1);
        const auto ordinal = endpoints.Integer(2);
        const auto owner = endpoints.Integer(3);
        if (!endpoints.Integer(4) || (role != 0 && role != 1) || ordinal < 0 ||
            role < previous_role ||
            (role == previous_role && ordinal <= previous_ordinal) ||
            owner < -1 || owner >= static_cast<std::int64_t>(instance_count)) {
          throw std::runtime_error("native compact EmuIR endpoint is invalid");
        }
        auto& values = role == 0 ? net_drivers : net_sinks;
        if (values.size() >= std::numeric_limits<std::uint32_t>::max()) {
          throw std::runtime_error("native compact EmuIR endpoint count is invalid");
        }
        values.push_back(static_cast<std::int32_t>(owner));
        observed_endpoints += role == 1;
        ++total_endpoints;
        previous_role = role;
        previous_ordinal = ordinal;
        have_endpoint = endpoints.Step();
      }
      net_driver_offsets.push_back(static_cast<std::uint32_t>(net_drivers.size()));
      net_sink_offsets.push_back(static_cast<std::uint32_t>(net_sinks.size()));
      ++observed_nets;
    }
    if (have_endpoint || observed_nets != net_count ||
        total_endpoints != expected_total_endpoints) {
      throw std::runtime_error("native compact EmuIR endpoint/net coverage disagrees");
    }
  } else {
    for (std::uint32_t index = 0; index < net_count; ++index) {
      const auto drivers = reader.Read<std::uint32_t>();
      RequireCount(drivers, sizeof(std::int32_t), reader.remaining(),
                   "native net driver");
      if (drivers > std::numeric_limits<std::uint32_t>::max() -
                        net_drivers.size()) {
        throw std::runtime_error("native net driver count is invalid");
      }
      for (std::uint32_t driver_index = 0; driver_index < drivers;
           ++driver_index) {
        const auto driver = reader.Read<std::int32_t>();
        if (driver < -1 ||
            (driver >= 0 && static_cast<std::uint32_t>(driver) >= instance_count)) {
          throw std::runtime_error("native net driver is invalid");
        }
        net_drivers.push_back(driver);
      }
      net_driver_offsets.push_back(
          static_cast<std::uint32_t>(net_drivers.size()));
      const auto sinks = reader.Read<std::uint32_t>();
      observed_endpoints += sinks;
      if (observed_endpoints > endpoint_count ||
          sinks > reader.remaining() / sizeof(std::int32_t)) {
        throw std::runtime_error("native endpoint count is invalid");
      }
      for (std::uint32_t sink_index = 0; sink_index < sinks; ++sink_index) {
        const auto sink = reader.Read<std::int32_t>();
        if (sink < -1 ||
            (sink >= 0 && static_cast<std::uint32_t>(sink) >= instance_count)) {
          throw std::runtime_error("native net sink is invalid");
        }
        net_sinks.push_back(sink);
      }
      net_sink_offsets.push_back(static_cast<std::uint32_t>(net_sinks.size()));
    }
  }
  if (observed_endpoints != endpoint_count) {
    throw std::runtime_error("native endpoint count is invalid");
  }

  RequireCount(cut_count, 3 * sizeof(std::uint32_t), reader.remaining(),
               "native cut");
  std::vector<std::uint32_t> cut_nets(cut_count, 0);
  std::vector<std::uint32_t> cut_source_parts(cut_count, 0);
  std::vector<std::uint32_t> cut_sink_offsets;
  std::vector<std::uint32_t> cut_sink_parts;
  cut_sink_offsets.reserve(static_cast<std::size_t>(cut_count) + 1);
  cut_sink_offsets.push_back(0);
  std::vector<std::int32_t> cut_by_net(net_count, -1);
  for (std::uint32_t index = 0; index < cut_count; ++index) {
    const auto net = reader.Read<std::uint32_t>();
    const auto source_part = reader.Read<std::uint32_t>();
    const auto sinks = reader.Read<std::uint32_t>();
    RequireCount(sinks, sizeof(std::uint32_t), reader.remaining(),
                 "native cut sink");
    if (sinks > std::numeric_limits<std::uint32_t>::max() -
                    cut_sink_parts.size()) {
      throw std::runtime_error("native cut sink count is invalid");
    }
    if (net >= net_count || source_part >= part_count ||
        cut_by_net[net] != -1) {
      throw std::runtime_error("native cut record is invalid");
    }
    cut_nets[index] = net;
    cut_source_parts[index] = source_part;
    cut_by_net[net] = static_cast<std::int32_t>(index);
    std::uint32_t previous_sink = 0;
    bool have_sink = false;
    for (std::uint32_t sink_index = 0; sink_index < sinks; ++sink_index) {
      const auto sink = reader.Read<std::uint32_t>();
      if (sink >= part_count || (have_sink && sink <= previous_sink)) {
        throw std::runtime_error("native cut sink is invalid");
      }
      cut_sink_parts.push_back(sink);
      previous_sink = sink;
      have_sink = true;
    }
    cut_sink_offsets.push_back(
        static_cast<std::uint32_t>(cut_sink_parts.size()));
  }
  reader.RequireEnd();

  const auto incoming = BuildInstanceNetIndex(
      instance_count, net_count, net_sink_offsets, net_sinks,
      "native incoming net");
  const auto outgoing = BuildInstanceNetIndex(
      instance_count, net_count, net_driver_offsets, net_drivers,
      "native outgoing net");

  std::vector<std::uint32_t> dependency_offsets;
  std::vector<std::uint32_t> dependencies;
  dependency_offsets.reserve(static_cast<std::size_t>(cut_count) + 1);
  dependency_offsets.push_back(0);
  std::vector<std::uint8_t> local_launches(cut_count, 0);
  GenerationMarks dependency_marks(instance_count);
  GenerationMarks found_marks(cut_count);
  std::vector<std::uint32_t> work;
  for (std::uint32_t cut_index = 0; cut_index < cut_count; ++cut_index) {
    std::vector<std::uint32_t> drivers;
    const auto cut_net = cut_nets[cut_index];
    for (auto offset = net_driver_offsets[cut_net];
         offset < net_driver_offsets[cut_net + 1]; ++offset) {
      const auto driver = net_drivers[offset];
      if (driver >= 0) drivers.push_back(static_cast<std::uint32_t>(driver));
    }
    std::sort(drivers.begin(), drivers.end());
    drivers.erase(std::unique(drivers.begin(), drivers.end()), drivers.end());
    if (drivers.size() != 1) {
      throw std::runtime_error("exact cut lacks one logic driver");
    }
    dependency_marks.Next();
    found_marks.Next();
    work = drivers;
    std::vector<std::uint32_t> found;
    while (!work.empty()) {
      const auto instance_index = work.back();
      work.pop_back();
      if (!dependency_marks.Insert(instance_index)) continue;
      if (instance_parts[instance_index] != cut_source_parts[cut_index]) {
        throw std::runtime_error("exact cut source cone crosses a boundary");
      }
      if (instance_states[instance_index]) {
        local_launches[cut_index] = 1;
        continue;
      }
      for (auto incoming_offset = incoming.offsets[instance_index];
           incoming_offset < incoming.offsets[instance_index + 1];
           ++incoming_offset) {
        const auto incoming_net = incoming.values[incoming_offset];
        const auto predecessor = cut_by_net[incoming_net];
        if (predecessor >= 0) {
          const auto predecessor_index =
              static_cast<std::uint32_t>(predecessor);
          if (CutContainsSink(predecessor_index, cut_source_parts[cut_index],
                              cut_sink_offsets, cut_sink_parts)) {
            if (found_marks.Insert(predecessor_index)) {
              found.push_back(predecessor_index);
            }
            continue;
          }
        }
        for (auto driver_offset = net_driver_offsets[incoming_net];
             driver_offset < net_driver_offsets[incoming_net + 1];
             ++driver_offset) {
          const auto upstream = net_drivers[driver_offset];
          if (upstream < 0) {
            local_launches[cut_index] = 1;
          } else if (instance_parts[static_cast<std::uint32_t>(upstream)] ==
                     cut_source_parts[cut_index]) {
            work.push_back(static_cast<std::uint32_t>(upstream));
          }
        }
      }
    }
    std::sort(found.begin(), found.end());
    if (found.size() > std::numeric_limits<std::uint32_t>::max() -
                           dependencies.size()) {
      throw std::runtime_error("native dependency count is invalid");
    }
    dependencies.insert(dependencies.end(), found.begin(), found.end());
    dependency_offsets.push_back(
        static_cast<std::uint32_t>(dependencies.size()));
  }

  CaptureSorter captures(output_path);
  GenerationMarks capture_marks(instance_count);
  std::vector<EndpointRef> endpoint_work;
  for (std::uint32_t cut_index = 0; cut_index < cut_count; ++cut_index) {
    const auto cut_net = cut_nets[cut_index];
    const auto cut_sink_begin = net_sink_offsets[cut_net];
    const auto cut_sink_count =
        net_sink_offsets[cut_net + 1] - cut_sink_begin;
    for (auto part_offset = cut_sink_offsets[cut_index];
         part_offset < cut_sink_offsets[cut_index + 1]; ++part_offset) {
      const auto sink_part = cut_sink_parts[part_offset];
      endpoint_work.clear();
      for (std::uint32_t sink_index = 0; sink_index < cut_sink_count;
           ++sink_index) {
        const auto endpoint = net_sinks[cut_sink_begin + sink_index];
        if (endpoint < 0) {
          captures.Add({cut_index, sink_part, cut_net, sink_index});
        } else if (instance_parts[static_cast<std::uint32_t>(endpoint)] ==
                   sink_part) {
          endpoint_work.push_back({cut_net, sink_index});
        }
      }
      capture_marks.Next();
      while (!endpoint_work.empty()) {
        const auto endpoint_ref = endpoint_work.back();
        endpoint_work.pop_back();
        const auto endpoint = net_sinks[
            net_sink_offsets[endpoint_ref.net] + endpoint_ref.sink];
        const auto instance_index = static_cast<std::uint32_t>(endpoint);
        if (instance_states[instance_index]) {
          captures.Add(
              {cut_index, sink_part, endpoint_ref.net, endpoint_ref.sink});
          continue;
        }
        if (!capture_marks.Insert(instance_index)) continue;
        for (auto outgoing_offset = outgoing.offsets[instance_index];
             outgoing_offset < outgoing.offsets[instance_index + 1];
             ++outgoing_offset) {
          const auto outgoing_net = outgoing.values[outgoing_offset];
          const auto downstream = cut_by_net[outgoing_net];
          const auto sink_begin = net_sink_offsets[outgoing_net];
          const auto sink_count =
              net_sink_offsets[outgoing_net + 1] - sink_begin;
          for (std::uint32_t sink_index = 0; sink_index < sink_count;
               ++sink_index) {
            const auto sink = net_sinks[sink_begin + sink_index];
            if (downstream >= 0 &&
                cut_source_parts[static_cast<std::uint32_t>(downstream)] ==
                    sink_part && sink >= 0 &&
                instance_parts[static_cast<std::uint32_t>(sink)] != sink_part) {
              continue;
            }
            if (sink < 0) {
              captures.Add(
                  {cut_index, sink_part, outgoing_net, sink_index});
            } else if (instance_parts[static_cast<std::uint32_t>(sink)] ==
                       sink_part) {
              endpoint_work.push_back({outgoing_net, sink_index});
            }
          }
        }
      }
    }
  }
  // Protocol v3 returns stable EmuIR coordinates rather than asserting final
  // string identity. The Python parent resolves only these C capture records
  // against authoritative EmuIR and independently checks endpoint semantics.
  const auto capture_count = captures.Finish();

  emuflow::core::AtomicBinaryWriter output(output_path);
  output.Write(kOutputMagic);
  output.Write(kVersion);
  output.Write(cut_count);
  output.Write(capture_count);
  for (std::uint32_t cut_index = 0; cut_index < cut_count; ++cut_index) {
    output.Write(local_launches[cut_index]);
    const auto begin = dependency_offsets[cut_index];
    const auto end = dependency_offsets[cut_index + 1];
    const auto count = end - begin;
    output.Write(count);
    for (auto offset = begin; offset < end; ++offset) {
      output.Write(dependencies[offset]);
    }
  }
  captures.WriteTo(output);
  output.Commit();
}

}  // namespace

void emuflow::native::AnalyzeStaticExact(const std::string& input_path,
                                         const std::string& output_path,
                                         const std::string& indexed_path) {
  AnalyzeImpl(input_path, output_path, indexed_path);
}

// Final cut-node records are produced from analyzer topology and final identities.
// The parent independently checks the topology; no scheduling policy is applied.
void emuflow::native::FinalizeStaticExact(
    const std::string& topology_path, const std::string& metadata_path,
    const std::string& output_path) {
  using emuflow::core::BinaryReader;
  constexpr std::uint64_t metadata_magic = 0x314D4553464D5545ULL;
  constexpr std::uint64_t nodes_magic = 0x314E4553464D5545ULL;
  BinaryReader topology(topology_path), metadata(metadata_path);
  emuflow::core::ReadAndValidateHeader(topology, {kOutputMagic, 3, "exact topology"});
  emuflow::core::ReadAndValidateHeader(metadata, {metadata_magic, 1, "exact identities"});
  const auto count = topology.Read<std::uint32_t>();
  const auto raw_captures = topology.Read<std::uint64_t>();
  if (metadata.Read<std::uint32_t>() != count || count > metadata.remaining() / 23) {
    throw std::runtime_error("native semantic cut coverage disagrees");
  }
  struct Cut {
    std::string net, kind, source;
    std::vector<std::string> sinks;
    std::uint32_t captures = 0;
  };
  std::vector<Cut> cuts(count);
  std::vector<std::uint8_t> flags(count);
  std::vector<std::uint64_t> offsets{0};
  std::vector<std::uint32_t> dependencies;
  std::uint64_t launches = 0, captures = 0;
  for (std::uint32_t i = 0; i < count; ++i) {
    auto& cut = cuts[i];
    cut.net = metadata.ReadString32();
    cut.kind = metadata.ReadString32();
    cut.source = metadata.ReadString32();
    if (i && cut.net <= cuts[i - 1].net) {
      throw std::runtime_error("native semantic cut identity order is invalid");
    }
    const auto sinks = metadata.Read<std::uint32_t>();
    if (sinks > metadata.remaining() / 5) {
      throw std::runtime_error("native semantic sinks are truncated");
    }
    for (std::uint32_t j = 0; j < sinks; ++j) cut.sinks.push_back(metadata.ReadString32());
    cut.captures = metadata.Read<std::uint32_t>();
    captures += cut.captures;
    const auto launch = topology.Read<std::uint8_t>();
    const auto edges = topology.Read<std::uint32_t>();
    if (launch > 1 || edges > count || edges > topology.remaining() / 4) {
      throw std::runtime_error("native semantic topology is invalid");
    }
    flags[i] = launch ? 1 : (edges == 0 ? 2 : 0);
    launches += flags[i] != 0;
    std::uint32_t previous = 0;
    for (std::uint32_t j = 0; j < edges; ++j) {
      const auto source = topology.Read<std::uint32_t>();
      if (source >= count || (j && source <= previous)) {
        throw std::runtime_error("native semantic predecessor is invalid");
      }
      dependencies.push_back(source);
      previous = source;
    }
    offsets.push_back(dependencies.size());
  }
  metadata.RequireEnd();
  if (raw_captures > topology.remaining() / 16 || topology.remaining() != raw_captures * 16) {
    throw std::runtime_error("native semantic capture coordinates are truncated");
  }
  // Coordinates have already been resolved and independently normalized. Check
  // their envelope and range before discarding them; final counts may deduplicate.
  for (std::uint64_t i = 0; i < raw_captures; ++i) {
    if (topology.Read<std::uint32_t>() >= count) {
      throw std::runtime_error("native semantic capture cut is invalid");
    }
    topology.Read<std::uint32_t>();
    topology.Read<std::uint32_t>();
    topology.Read<std::uint32_t>();
  }
  topology.RequireEnd();
  if (captures > raw_captures || launches + dependencies.size() + captures >= UINT32_MAX) {
    throw std::runtime_error("native semantic segment population is invalid");
  }
  std::vector<std::uint64_t> successor_offsets(count + 1, 0);
  for (const auto source : dependencies) ++successor_offsets[source + 1];
  for (std::uint32_t i = 0; i < count; ++i) successor_offsets[i + 1] += successor_offsets[i];
  auto cursor = successor_offsets;
  std::vector<std::uint32_t> successors(dependencies.size()), levels(count, 1), depths(count, 0);
  std::vector<std::uint64_t> indegree(count);
  std::queue<std::uint32_t> ready;
  for (std::uint32_t i = 0; i < count; ++i) {
    indegree[i] = offsets[i + 1] - offsets[i];
    if (!indegree[i]) ready.push(i);
    for (auto j = offsets[i]; j < offsets[i + 1]; ++j) successors[cursor[dependencies[j]]++] = i;
  }
  std::uint64_t visited = 0;
  while (!ready.empty()) {
    const auto i = ready.front(); ready.pop(); ++visited;
    depths[i] += cuts[i].kind == "combinational";
    for (auto j = successor_offsets[i]; j < successor_offsets[i + 1]; ++j) {
      const auto sink = successors[j];
      levels[sink] = std::max(levels[sink], levels[i] + 1);
      depths[sink] = std::max(depths[sink], depths[i]);
      if (--indegree[sink] == 0) ready.push(sink);
    }
  }
  if (visited != count) throw std::runtime_error("native semantic topology contains a cycle");
  auto segment = [](std::uint64_t number) {
    const auto digits = std::to_string(number);
    return std::string("segment") + std::string(digits.size() < 6 ? 6 - digits.size() : 0, '0') + digits;
  };
  auto string_size = [](const std::string& value) { return 4ULL + value.size(); };
  std::vector<std::uint64_t> positions{0};
  std::uint64_t launch_number = 0, edge_number = launches, capture_number = launches + dependencies.size();
  auto visit = [&](auto&& record) {
    for (std::uint32_t i = 0; i < count; ++i) {
      const auto& cut = cuts[i];
      record(i, cut, launch_number, edge_number, capture_number);
      launch_number += flags[i] != 0;
      edge_number += offsets[i + 1] - offsets[i];
      capture_number += cut.captures;
    }
  };
  visit([&](auto i, const auto& cut, auto launch, auto edge, auto capture) {
    std::uint64_t bytes = string_size(cut.net) + string_size(cut.kind) + string_size(cut.source) + 25;
    for (const auto& sink : cut.sinks) bytes += string_size(sink);
    if (flags[i]) bytes += string_size(segment(launch));
    for (auto j = offsets[i]; j < offsets[i + 1]; ++j) {
      bytes += 4 + string_size(cuts[dependencies[j]].net) + string_size(segment(edge++));
    }
    for (std::uint32_t j = 0; j < cut.captures; ++j) bytes += string_size(segment(capture++));
    positions.push_back(positions.back() + bytes);
  });
  emuflow::core::AtomicBinaryWriter output(output_path);
  output.Write(nodes_magic); output.Write(std::uint32_t{2}); output.Write(count);
  output.Write(positions.back());
  for (const auto position : positions) output.Write(position);
  auto write_string = [&](const std::string& value) {
    output.Write(static_cast<std::uint32_t>(value.size())); output.WriteBytes(value.data(), value.size());
  };
  launch_number = 0; edge_number = launches; capture_number = launches + dependencies.size();
  visit([&](auto i, const auto& cut, auto launch, auto edge, auto capture) {
    write_string(cut.net); write_string(cut.kind); write_string(cut.source);
    const auto edges = static_cast<std::uint32_t>(offsets[i + 1] - offsets[i]);
    output.Write(levels[i]); output.Write(depths[i]);
    output.Write(static_cast<std::uint32_t>(cut.sinks.size())); output.Write(edges);
    output.Write(edges + std::uint32_t(flags[i] != 0)); output.Write(cut.captures);
    for (const auto& sink : cut.sinks) write_string(sink);
    for (auto j = offsets[i]; j < offsets[i + 1]; ++j) write_string(cuts[dependencies[j]].net);
    if (flags[i]) write_string(segment(launch));
    for (std::uint32_t j = 0; j < edges; ++j) write_string(segment(edge++));
    std::vector<std::uint32_t> capture_ids(cut.captures);
    for (std::uint32_t j = 0; j < cut.captures; ++j) capture_ids[j] = capture + j;
    std::sort(capture_ids.begin(), capture_ids.end(), [&](auto left, auto right) { return segment(left) < segment(right); });
    for (const auto number : capture_ids) write_string(segment(number));
    for (auto j = offsets[i]; j < offsets[i + 1]; ++j) output.Write(dependencies[j]);
    output.Write(flags[i]);
  });
  output.Commit();
}

void emuflow::native::ResolveStaticExactCaptures(
    const std::string& source_path, const std::string& request_path,
    const std::string& topology_path, const std::string& parts_path,
    const std::string& output_path) {
  using emuflow::core::BinaryReader;
  using emuflow::core::SqliteDatabase;
  BinaryReader request(request_path), topology(topology_path), identities(parts_path);
  emuflow::core::ReadAndValidateHeader(request, {kInputMagic, 4, "indexed request"});
  emuflow::core::ReadAndValidateHeader(topology, {kOutputMagic, 3, "exact topology"});
  emuflow::core::ReadAndValidateHeader(identities, {0x31504553464D5545ULL, 1, "exact parts"});
  const auto cells = request.Read<std::uint32_t>();
  const auto nets = request.Read<std::uint32_t>();
  const auto cuts = request.Read<std::uint32_t>();
  const auto parts = request.Read<std::uint32_t>();
  request.Read<std::uint32_t>();
  if (!parts || identities.Read<std::uint32_t>() != parts ||
      parts > identities.remaining() / 5 || cells > request.remaining() / 4 ||
      topology.Read<std::uint32_t>() != cuts) {
    throw std::runtime_error("native capture input coverage disagrees");
  }
  const auto raw_count = topology.Read<std::uint64_t>();
  std::vector<std::string> part_ids;
  for (std::uint32_t i = 0; i < parts; ++i) {
    auto identity = identities.ReadString32();
    if (i && identity <= part_ids.back()) throw std::runtime_error("native capture part order is invalid");
    part_ids.push_back(std::move(identity));
  }
  identities.RequireEnd();
  std::vector<std::uint32_t> assignments(cells);
  for (auto& assignment : assignments) {
    assignment = request.Read<std::uint32_t>();
    if (assignment >= parts) throw std::runtime_error("native capture assignment is invalid");
  }
  SqliteDatabase source(source_path, SqliteDatabase::Mode::kReadOnly);
  auto application = source.Prepare("PRAGMA application_id");
  auto version = source.Prepare("PRAGMA user_version");
  auto counts = source.Prepare(
      "SELECT json_extract(value,'$.cell_records'),json_extract(value,'$.net_records') "
      "FROM metadata WHERE key='record_counts_json'");
  if (!application.Step() || application.Integer(0) != 0x454D5549 ||
      !version.Step() || version.Integer(0) != 2 || !counts.Step() ||
      counts.Integer(0) != cells || counts.Integer(1) != nets) {
    throw std::runtime_error("native capture source envelope/counts disagree");
  }
  if (cuts > request.remaining() / 12) throw std::runtime_error("native capture cuts are truncated");
  std::vector<std::string> cut_ids;
  std::vector<std::uint64_t> sink_offsets{0};
  std::vector<std::uint32_t> sink_parts;
  auto net_id = source.Prepare("SELECT id FROM net_records WHERE seq=?");
  for (std::uint32_t i = 0; i < cuts; ++i) {
    const auto net = request.Read<std::uint32_t>();
    const auto source_part = request.Read<std::uint32_t>();
    const auto sinks = request.Read<std::uint32_t>();
    if (net >= nets || source_part >= parts || sinks > request.remaining() / 4) {
      throw std::runtime_error("native capture cut is invalid");
    }
    net_id.BindInteger(1, net);
    if (!net_id.Step()) throw std::runtime_error("native capture cut identity is missing");
    auto identity = net_id.Text(0); net_id.Reset();
    if (identity.empty() || (i && identity <= cut_ids.back())) {
      throw std::runtime_error("native capture cut order is invalid");
    }
    cut_ids.push_back(std::move(identity));
    for (std::uint32_t j = 0; j < sinks; ++j) {
      const auto sink = request.Read<std::uint32_t>();
      if (sink >= parts || (j && sink <= sink_parts.back())) {
        throw std::runtime_error("native capture cut sink is invalid");
      }
      sink_parts.push_back(sink);
    }
    sink_offsets.push_back(sink_parts.size());
    const auto launch = topology.Read<std::uint8_t>();
    const auto predecessors = topology.Read<std::uint32_t>();
    if (launch > 1 || predecessors > cuts || predecessors > topology.remaining() / 4) {
      throw std::runtime_error("native capture topology is invalid");
    }
    std::uint32_t previous = 0;
    for (std::uint32_t j = 0; j < predecessors; ++j) {
      const auto predecessor = topology.Read<std::uint32_t>();
      if (predecessor >= cuts || (j && predecessor <= previous)) {
        throw std::runtime_error("native capture predecessor is invalid");
      }
      previous = predecessor;
    }
  }
  request.RequireEnd();
  if (raw_count > topology.remaining() / 16 || raw_count * 16 != topology.remaining()) {
    throw std::runtime_error("native capture coordinates are truncated");
  }
  // Lifecycle-scoped uniqueness/index spool. Ordered records themselves are
  // published as binary, and the independent validator removes this index.
  SqliteDatabase index(output_path + ".sqlite", SqliteDatabase::Mode::kCreateExclusive);
  index.Execute("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE;"
      "CREATE TABLE capture_relation(cut_net TEXT NOT NULL,fpga TEXT NOT NULL,"
      "kind TEXT NOT NULL,endpoint TEXT NOT NULL,port_key TEXT NOT NULL,"
      "bit_length INTEGER NOT NULL,bit_key BLOB NOT NULL,seq INTEGER,"
      "PRIMARY KEY(cut_net,fpga,kind,endpoint,port_key,bit_length,bit_key)) WITHOUT ROWID");
  emuflow::core::SqliteTransaction transaction(index);
  auto insert = index.Prepare("INSERT OR IGNORE INTO capture_relation "
      "VALUES(?,?,?,?,?,?,?,NULL)");
  auto endpoints = source.Prepare(
      "SELECT e.net_seq,e.ordinal,coalesce(e.instance_seq,-1),c.id,p.name,e.bit,"
      "typeof(e.bit),typeof(e.instance_seq),"
      "coalesce(json_extract(r.value,'$.ff'),0)!=0 OR "
      "coalesce(json_extract(r.value,'$.bram'),0)!=0 OR "
      "coalesce(json_extract(r.value,'$.bram18k'),0)!=0 OR "
      "coalesce(json_extract(r.value,'$.uram288'),0)!=0,"
      "typeof(e.net_seq),typeof(e.ordinal) "
      "FROM endpoint_records e LEFT JOIN cell_records c ON c.seq=e.instance_seq "
      "JOIN pin_names p ON p.seq=e.port_seq LEFT JOIN payloads r ON r.seq=c.resources_seq "
      "WHERE e.role=1 ORDER BY e.net_seq,e.ordinal");
  std::int64_t previous_net = -1, previous_stored_ordinal = -1;
  std::uint64_t dense_ordinal = 0;
  const auto advance_endpoint = [&]() {
    if (!endpoints.Step()) return false;
    const auto net = endpoints.Integer(0);
    const auto ordinal = endpoints.Integer(1);
    if (endpoints.Text(9) != "integer" || endpoints.Text(10) != "integer" ||
        net < 0 || net >= nets || ordinal < 0 || net < previous_net ||
        (net == previous_net && ordinal <= previous_stored_ordinal)) {
      throw std::runtime_error("compact EmuIR endpoint sequence is invalid");
    }
    dense_ordinal = net == previous_net ? dense_ordinal + 1 : 0;
    if (dense_ordinal > std::numeric_limits<std::uint32_t>::max()) {
      throw std::runtime_error("compact EmuIR endpoint sequence is invalid");
    }
    previous_net = net;
    previous_stored_ordinal = ordinal;
    return true;
  };
  bool have_endpoint = advance_endpoint();
  std::array<std::uint32_t,4> previous{};
  bool have_previous = false;
  for (std::uint64_t i = 0; i < raw_count; ++i) {
    const auto cut = topology.Read<std::uint32_t>();
    const auto part = topology.Read<std::uint32_t>();
    const auto net = topology.Read<std::uint32_t>();
    const auto sink = topology.Read<std::uint32_t>();
    const std::array<std::uint32_t,4> key{net,sink,cut,part};
    if (cut >= cuts || part >= parts || net >= nets ||
        (have_previous && key <= previous) ||
        !std::binary_search(sink_parts.begin() + sink_offsets[cut],
                            sink_parts.begin() + sink_offsets[cut + 1], part)) {
      throw std::runtime_error("native Static Exact capture is invalid");
    }
    previous = key; have_previous = true;
    while (have_endpoint && (endpoints.Integer(0) < net ||
        (endpoints.Integer(0) == net && dense_ordinal < sink))) {
      have_endpoint = advance_endpoint();
    }
    if (!have_endpoint || endpoints.Integer(0) != net || dense_ordinal != sink ||
        endpoints.Text(6) != "integer" || endpoints.Integer(5) < 0 || endpoints.Text(4).empty()) {
      throw std::runtime_error("native Static Exact capture endpoint is invalid");
    }
    const auto owner = endpoints.Integer(2);
    const auto bit = static_cast<std::uint64_t>(endpoints.Integer(5));
    const auto port = endpoints.Text(4);
    std::string kind, endpoint, port_key, bit_key;
    if (endpoints.Text(7) == "null") {
      kind = "top-output"; endpoint = "top:" + port + "[" + std::to_string(bit) + "]";
    } else {
      if (endpoints.Text(7) != "integer" || owner < 0 || owner >= cells ||
          assignments[owner] != part || endpoints.Text(3).empty() || !endpoints.Integer(8)) {
        throw std::runtime_error("native Static Exact capture ownership is invalid");
      }
      kind = "architectural-state"; endpoint = endpoints.Text(3); port_key = port;
      auto remaining = bit;
      do { bit_key.insert(bit_key.begin(), static_cast<char>(remaining & 255)); remaining >>= 8; } while (remaining);
    }
    insert.BindText(1, cut_ids[cut]); insert.BindText(2, part_ids[part]);
    insert.BindText(3, kind); insert.BindText(4, endpoint); insert.BindText(5, port_key);
    insert.BindInteger(6, kind == "top-output" ? -1 : static_cast<std::int64_t>(bit_key.size()));
    insert.BindBlob(7, bit_key.data(), bit_key.size()); insert.Execute();
  }
  topology.RequireEnd();
  const std::string columns = "cut_net,fpga,kind,endpoint,port_key,bit_length,bit_key";
  auto rows = index.Prepare("SELECT " + columns + " FROM capture_relation ORDER BY " + columns);
  std::vector<std::uint64_t> positions{0};
  while (rows.Step()) {
    std::uint64_t size = 21;
    for (int column : {0,1,3,4}) size += rows.Text(column).size();
    size += rows.Blob(6).size();
    positions.push_back(positions.back() + size);
  }
  rows.Reset();
  if (positions.size() - 1 >= UINT32_MAX) throw std::runtime_error("native capture population is too large");
  emuflow::core::AtomicBinaryWriter output(output_path);
  output.Write(std::uint64_t{0x31434553464D5545ULL}); output.Write(std::uint32_t{1});
  output.Write(static_cast<std::uint32_t>(positions.size() - 1)); output.Write(positions.back());
  for (const auto position : positions) output.Write(position);
  auto update = index.Prepare("UPDATE capture_relation SET seq=? WHERE cut_net=? AND fpga=? "
      "AND kind=? AND endpoint=? AND port_key=? AND bit_length=? AND bit_key=?");
  auto first = index.Prepare("SELECT " + columns + " FROM capture_relation ORDER BY " + columns + " LIMIT 1");
  auto next = index.Prepare("SELECT " + columns + " FROM capture_relation WHERE (" + columns +
      ") > (?,?,?,?,?,?,?) ORDER BY " + columns + " LIMIT 1");
  const auto count = positions.size() - 1;
  for (std::uint32_t seq = 0; seq < count; ++seq) {
    auto& row = seq == 0 ? first : next;
    if (!row.Step()) throw std::runtime_error("native capture ordered coverage disagrees");
    const auto kind = row.Text(2);
    const auto bit_length = row.Integer(5);
    const std::array<std::string,5> keys{row.Text(0),row.Text(1),kind,row.Text(3),row.Text(4)};
    const auto bit = row.Blob(6);
    row.Reset();  // No active reader cursor may observe its own table updates.
    const std::array<std::string,5> fields{keys[0],keys[1],keys[3],keys[4],bit};
    output.Write(std::uint8_t(kind == "top-output" ? 0 : 1));
    for (const auto& field : fields) output.Write(static_cast<std::uint32_t>(field.size()));
    for (const auto& field : fields) output.WriteBytes(field.data(), field.size());
    update.BindInteger(1, seq);
    for (int column = 0; column < 5; ++column) {
      update.BindText(column + 2, keys[column]);
      next.BindText(column + 1, keys[column]);
    }
    update.BindInteger(7, bit_length); update.BindBlob(8, bit.data(), bit.size()); update.Execute();
    next.BindInteger(6, bit_length); next.BindBlob(7, bit.data(), bit.size());
  }
  transaction.Commit();
  output.Commit();
}

namespace {

std::string ExactIdentity(const char* prefix, std::uint64_t number) {
  const auto digits = std::to_string(number);
  return std::string(prefix) + std::string(digits.size() < 6 ? 6 - digits.size() : 0, '0') + digits;
}

// Match Python's ensure_ascii=True encoding, including surrogate pairs and
// canonical short control escapes, while rejecting malformed UTF-8 input.
std::string ExactQuote(const std::string& text) {
  std::string result = "\"";
  const char* hex = "0123456789abcdef";
  auto escape = [&](std::uint32_t code) {
    result += "\\u";
    for (int shift = 12; shift >= 0; shift -= 4) result += hex[(code >> shift) & 15];
  };
  for (std::size_t i = 0; i < text.size();) {
    const auto first = static_cast<unsigned char>(text[i++]);
    std::uint32_t code = first;
    int extra = 0;
    if (first >= 0xc2 && first <= 0xdf) { code = first & 31; extra = 1; }
    else if (first >= 0xe0 && first <= 0xef) { code = first & 15; extra = 2; }
    else if (first >= 0xf0 && first <= 0xf4) { code = first & 7; extra = 3; }
    else if (first >= 0x80) throw std::runtime_error("native canonical identity has invalid UTF-8");
    if (i + extra > text.size()) throw std::runtime_error("native canonical identity is truncated");
    for (int j = 0; j < extra; ++j) {
      const auto next = static_cast<unsigned char>(text[i++]);
      if ((next & 0xc0) != 0x80) throw std::runtime_error("native canonical identity has invalid UTF-8");
      code = (code << 6) | (next & 63);
    }
    if ((extra == 1 && code < 0x80) || (extra == 2 && code < 0x800) ||
        (extra == 3 && code < 0x10000) || code > 0x10ffff ||
        (code >= 0xd800 && code <= 0xdfff)) {
      throw std::runtime_error("native canonical identity has invalid UTF-8");
    }
    switch (code) {
      case '"': result += "\\\""; break;
      case '\\': result += "\\\\"; break;
      case '\b': result += "\\b"; break;
      case '\f': result += "\\f"; break;
      case '\n': result += "\\n"; break;
      case '\r': result += "\\r"; break;
      case '\t': result += "\\t"; break;
      default:
        if (code >= 0x20 && code <= 0x7e) result += static_cast<char>(code);
        else if (code <= 0xffff) escape(code);
        else { code -= 0x10000; escape(0xd800 + (code >> 10)); escape(0xdc00 + (code & 1023)); }
    }
  }
  return result + "\"";
}

std::string ExactStringArray(const std::vector<std::string>& items) {
  if (items.empty()) return "[]";
  std::string value = "[";
  for (std::size_t i = 0; i < items.size(); ++i) value += std::string(i ? ",\n        " : "\n        ") + ExactQuote(items[i]);
  return value + "\n      ]";
}

std::string ExactBitDecimal(const std::string& bytes) {
  std::vector<std::uint32_t> words{0};
  for (const auto byte : bytes) {
    std::uint64_t carry = static_cast<unsigned char>(byte);
    for (auto& word : words) { carry += std::uint64_t(word) * 256; word = carry % 1000000000; carry /= 1000000000; }
    if (carry) words.push_back(static_cast<std::uint32_t>(carry));
  }
  std::string result = std::to_string(words.back());
  for (std::size_t i = words.size() - 1; i > 0; --i) {
    const auto digits = std::to_string(words[i - 1]); result += std::string(9 - digits.size(), '0') + digits;
  }
  return result;
}

class ExactHashTable {
 public:
  ExactHashTable(const std::string& path, std::uint64_t magic, std::uint32_t version)
      : reader(path), offsets(path) {
    emuflow::core::ReadAndValidateHeader(reader, {magic, version, "native semantic records"});
    count = reader.Read<std::uint32_t>(); size_ = reader.Read<std::uint64_t>();
    start_ = 24ULL + 8ULL * (std::uint64_t(count) + 1);
    if (start_ > reader.size() || size_ != reader.size() - start_) throw std::runtime_error("native canonical record envelope is invalid");
    offsets.Seek(24ULL + 8ULL * count);
    if (offsets.Read<std::uint64_t>() != size_) throw std::runtime_error("native canonical record extent is invalid");
    Reset();
  }
  void Reset() {
    reader.Seek(start_); offsets.Seek(24); observed_ = 0; end_ = 0;
    if (offsets.Read<std::uint64_t>() != 0) throw std::runtime_error("native canonical record origin is invalid");
  }
  void Begin() {
    if (observed_++ >= count) throw std::runtime_error("native canonical record count is invalid");
    const auto next = offsets.Read<std::uint64_t>();
    if (next <= end_ || next > size_) throw std::runtime_error("native canonical record offsets are invalid");
    end_ = next;
  }
  std::uint64_t Remaining() const {
    const auto position = reader.size() - reader.remaining();
    if (position > start_ + end_) throw std::runtime_error("native canonical record exceeds its boundary");
    return start_ + end_ - position;
  }
  void End() {
    if (reader.size() - reader.remaining() != start_ + end_) throw std::runtime_error("native canonical record lengths disagree");
  }
  emuflow::core::BinaryReader reader;
  std::uint32_t count = 0;
 private:
  emuflow::core::BinaryReader offsets;
  std::uint64_t size_ = 0, start_ = 0, end_ = 0, observed_ = 0;
};

struct ExactHashNode {
  std::string net, kind, source;
  std::uint32_t level, depth;
  std::vector<std::string> sinks, predecessors, sources, captures;
  std::uint8_t flag;
  std::vector<std::uint32_t> predecessor_indices;
};

ExactHashNode ReadExactHashNode(ExactHashTable& table) {
  table.Begin(); auto& r = table.reader;
  ExactHashNode node;
  node.net = r.ReadString32(); node.kind = r.ReadString32(); node.source = r.ReadString32();
  node.level = r.Read<std::uint32_t>(); node.depth = r.Read<std::uint32_t>();
  std::array<std::uint32_t,4> counts{};
  std::uint64_t total = 0;
  for (auto& count : counts) { count = r.Read<std::uint32_t>(); total += count; }
  if (total > table.Remaining() / 5) throw std::runtime_error("native canonical node fields are truncated");
  std::array<std::vector<std::string>*,4> lists{&node.sinks,&node.predecessors,&node.sources,&node.captures};
  for (int i = 0; i < 4; ++i) for (std::uint32_t j = 0; j < counts[i]; ++j) lists[i]->push_back(r.ReadString32());
  for (std::uint32_t i = 0; i < counts[1]; ++i) {
    const auto ordinal = r.Read<std::uint32_t>();
    if (ordinal >= table.count || (i && ordinal <= node.predecessor_indices.back())) {
      throw std::runtime_error("native canonical predecessor coordinate is invalid");
    }
    node.predecessor_indices.push_back(ordinal);
  }
  node.flag = r.Read<std::uint8_t>();
  if (node.flag > 2 || node.sources.size() != node.predecessors.size() + (node.flag != 0) ||
      (node.flag == 2 && !node.predecessors.empty()) || (!node.flag && node.predecessors.empty())) {
    throw std::runtime_error("native canonical launch semantics disagree");
  }
  table.End(); return node;
}

struct ExactHashCapture { std::string net, fpga, endpoint, port, bit; std::uint8_t kind; };

ExactHashCapture ReadExactHashCapture(ExactHashTable& table) {
  table.Begin(); auto& r = table.reader;
  ExactHashCapture capture; capture.kind = r.Read<std::uint8_t>();
  std::array<std::uint32_t,5> lengths{}; std::uint64_t total = 0;
  for (auto& length : lengths) { length = r.Read<std::uint32_t>(); total += length; }
  if (total != table.Remaining()) throw std::runtime_error("native canonical capture fields are truncated");
  std::array<std::string*,5> fields{&capture.net,&capture.fpga,&capture.endpoint,&capture.port,&capture.bit};
  for (int i = 0; i < 5; ++i) { fields[i]->resize(lengths[i]); r.ReadBytes(fields[i]->data(), lengths[i]); }
  if (capture.kind > 1 || capture.net.empty() || capture.fpga.empty() || capture.endpoint.empty() ||
      (!capture.kind && (!capture.port.empty() || !capture.bit.empty())) ||
      (capture.kind && (capture.port.empty() || capture.bit.empty() || capture.bit.size() > 1024 ||
                       (capture.bit.size() > 1 && capture.bit[0] == 0)))) {
    throw std::runtime_error("native canonical capture is invalid");
  }
  table.End(); return capture;
}

class ExactCanonicalHash {
 public:
  void Write(const std::string& text) {
    buffer_ += text; if (buffer_.size() >= 8192) Flush();
  }
  void Object(std::initializer_list<std::pair<std::string,std::string>> fields) {
    Write("{"); std::string previous; bool first = true;
    for (const auto& field : fields) {
      if (!first && field.first <= previous) throw std::runtime_error("native canonical field order is invalid");
      Write(std::string(first ? "\n      " : ",\n      ") + ExactQuote(field.first) + ": " + field.second);
      previous = field.first; first = false;
    }
    if (!first) Write("\n    "); Write("}");
  }
  void Element(std::uint64_t& count) { Write(count++ ? ",\n    " : "\n    "); }
  void FinishArray(std::uint64_t count) { if (count) Write("\n  "); Write("]"); }
  std::string Finish() {
    Flush(); const auto digest = hash_.finish(); std::string result;
    for (auto byte : digest) { result += "0123456789abcdef"[byte >> 4]; result += "0123456789abcdef"[byte & 15]; }
    return result;
  }
 private:
  void Flush() { hash_.update(reinterpret_cast<const unsigned char*>(buffer_.data()), buffer_.size()); buffer_.clear(); }
  emuflow::core::Sha256 hash_; std::string buffer_;
};

}  // namespace

void emuflow::native::HashStaticExactSemantics(
    const std::string& nodes_path, const std::string& captures_path,
    const std::string& request_path, const std::string& output_path) {
  ExactHashTable nodes(nodes_path, 0x314E4553464D5545ULL, 2);
  ExactHashTable captures(captures_path, 0x31434553464D5545ULL, 1);
  emuflow::core::BinaryReader request(request_path);
  emuflow::core::ReadAndValidateHeader(request, {0x31484553464D5545ULL, 1, "canonical header"});
  const auto fields = request.Read<std::uint32_t>();
  if (fields > request.remaining() / 6) throw std::runtime_error("native canonical header is truncated");
  ExactCanonicalHash hash; hash.Write("{"); std::string previous; unsigned tags = 0;
  for (std::uint32_t field = 0; field < fields; ++field) {
    const auto key = request.ReadString32(); const auto tag = request.Read<std::uint8_t>();
    if (field && key <= previous) throw std::runtime_error("native canonical header order is invalid");
    previous = key; hash.Write(std::string(field ? ",\n  " : "\n  ") + ExactQuote(key) + ": ");
    if (!tag) { hash.Write(request.ReadString32()); continue; }
    const std::array<std::string,4> keys{"capture_requirements","cut_nodes","dependency_edges","logic_segments"};
    if (tag > 4 || key != keys[tag - 1] || (tags & (1U << tag))) throw std::runtime_error("native canonical relation tag is invalid");
    tags |= 1U << tag; hash.Write("["); std::uint64_t count = 0;
    nodes.Reset(); captures.Reset();
    if (tag == 1) {
      for (std::uint32_t i = 0; i < captures.count; ++i) {
        const auto c = ReadExactHashCapture(captures); hash.Element(count);
        if (!c.kind) hash.Object({{"cut_net",ExactQuote(c.net)},{"endpoint",ExactQuote(c.endpoint)},
            {"fpga",ExactQuote(c.fpga)},{"id",ExactQuote(ExactIdentity("capture",i))},{"kind","\"top-output\""}});
        else hash.Object({{"bit",ExactBitDecimal(c.bit)},{"cut_net",ExactQuote(c.net)},{"endpoint",ExactQuote(c.endpoint)},
            {"fpga",ExactQuote(c.fpga)},{"id",ExactQuote(ExactIdentity("capture",i))},{"kind","\"architectural-state\""},{"port",ExactQuote(c.port)}});
      }
    } else if (tag == 2) {
      for (std::uint32_t i = 0; i < nodes.count; ++i) {
        const auto n = ReadExactHashNode(nodes); hash.Element(count);
        hash.Object({{"capture_segment_ids",ExactStringArray(n.captures)},{"combinational_dependency_depth",std::to_string(n.depth)},
            {"cut_class",ExactQuote(n.kind)},{"dependency_level",std::to_string(n.level)},{"net",ExactQuote(n.net)},
            {"predecessor_cut_nets",ExactStringArray(n.predecessors)},{"sink_fpgas",ExactStringArray(n.sinks)},
            {"source_fpgas",ExactStringArray({n.source})},{"source_segment_ids",ExactStringArray(n.sources)}});
      }
    } else if (tag == 3) {
      for (std::uint32_t i = 0; i < nodes.count; ++i) {
        const auto n = ReadExactHashNode(nodes);
        for (std::size_t j = 0; j < n.predecessors.size(); ++j) {
          hash.Element(count); hash.Object({{"from",ExactQuote(n.predecessors[j])},
              {"segment",ExactQuote(n.sources[j + (n.flag != 0)])},{"to",ExactQuote(n.net)}});
        }
      }
    } else {
      std::uint64_t capture_start = 0;
      for (std::uint32_t i = 0; i < nodes.count; ++i) {
        const auto n = ReadExactHashNode(nodes); capture_start += n.sources.size();
        if (!n.flag) continue; hash.Element(count);
        if (n.flag == 2) hash.Object({{"fpga",ExactQuote(n.source)},{"id",ExactQuote(n.sources[0])},
            {"kind","\"launch_to_tx\""},{"sink_cut_net",ExactQuote(n.net)},{"source_semantics","\"configuration-stable-constant\""}});
        else hash.Object({{"fpga",ExactQuote(n.source)},{"id",ExactQuote(n.sources[0])},{"kind","\"launch_to_tx\""},{"sink_cut_net",ExactQuote(n.net)}});
      }
      nodes.Reset();
      for (std::uint32_t i = 0; i < nodes.count; ++i) {
        const auto n = ReadExactHashNode(nodes);
        for (std::size_t j = 0; j < n.predecessors.size(); ++j) {
          hash.Element(count); hash.Object({{"fpga",ExactQuote(n.source)},{"id",ExactQuote(n.sources[j + (n.flag != 0)])},
              {"kind","\"rx_to_tx\""},{"sink_cut_net",ExactQuote(n.net)},{"source_cut_net",ExactQuote(n.predecessors[j])}});
        }
      }
      for (std::uint32_t i = 0; i < captures.count; ++i) {
        const auto c = ReadExactHashCapture(captures); hash.Element(count);
        hash.Object({{"capture_requirement",ExactQuote(ExactIdentity("capture",i))},{"fpga",ExactQuote(c.fpga)},
            {"id",ExactQuote(ExactIdentity("segment",capture_start + i))},{"kind","\"rx_to_capture\""},{"source_cut_net",ExactQuote(c.net)}});
      }
    }
    hash.FinishArray(count);
  }
  request.RequireEnd(); if (tags != 30) throw std::runtime_error("native canonical relation coverage is incomplete");
  if (fields) hash.Write("\n"); hash.Write("}\n");
  const auto digest = hash.Finish() + "\n";
  emuflow::core::AtomicBinaryWriter output(output_path); output.WriteBytes(digest.data(), digest.size()); output.Commit();
}

namespace {
// Disjoint buffered views fill one pre-sized private artifact. Each byte is
// written once and hashed as it is emitted; the outer small-manifest commit
// performs the only durability barrier for the final payload.
class ExactOutputSection {
 public:
  ExactOutputSection(const std::string& path, std::uint64_t offset, std::uint64_t bytes)
      : stream_(path, std::ios::binary | std::ios::in | std::ios::out), remaining_(bytes) {
    stream_.seekp(offset);
    if (!stream_) throw std::runtime_error("cannot open native exact output section");
  }
  template <typename T> void Write(const T& value) { WriteBytes(&value, sizeof(value)); }
  void WriteBytes(const void* data, std::size_t size) {
    if (size > remaining_) throw std::runtime_error("native exact section exceeds its extent");
    stream_.write(static_cast<const char*>(data), size);
    if (!stream_) throw std::runtime_error("cannot write native exact output section");
    digest_.update(static_cast<const unsigned char*>(data), size); remaining_ -= size;
  }
  std::array<unsigned char,32> Close() {
    if (remaining_) throw std::runtime_error("native exact section is incomplete");
    stream_.flush(); stream_.close();
    if (!stream_) throw std::runtime_error("cannot close native exact output section");
    return digest_.finish();
  }
 private:
  std::fstream stream_;
  std::uint64_t remaining_;
  emuflow::core::Sha256 digest_;
};
}  // namespace

void emuflow::native::WriteStaticExactSections(
    const std::string& nodes_path, const std::string& captures_path,
    const std::string& request_path, const std::string& output_path) {
  ExactHashTable nodes(nodes_path, 0x314E4553464D5545ULL, 2);
  ExactHashTable captures(captures_path, 0x31434553464D5545ULL, 1);
  emuflow::core::BinaryReader request(request_path);
  emuflow::core::ReadAndValidateHeader(request, {0x31574553464D5545ULL, 1, "exact section request"});
  const auto format = request.Read<std::uint32_t>();
  const auto part_count = request.Read<std::uint32_t>();
  const std::uint64_t section_count = 2ULL + 2ULL * part_count;
  if ((format != 1 && format != 2) || !part_count ||
      part_count > request.remaining() / 5 || section_count > UINT32_MAX) {
    throw std::runtime_error("native exact section request is invalid");
  }
  std::vector<std::string> parts;
  for (std::uint32_t i = 0; i < part_count; ++i) {
    auto part = request.ReadString32();
    if (i && part <= parts.back()) throw std::runtime_error("native exact parts are invalid");
    parts.push_back(std::move(part));
  }
  auto part_index = [&](const std::string& fpga) {
    const auto found = std::lower_bound(parts.begin(), parts.end(), fpga);
    if (found == parts.end() || *found != fpga) throw std::runtime_error("native exact FPGA identity is invalid");
    return static_cast<std::size_t>(found - parts.begin());
  };
  const auto staged_path = request.ReadString32();
  if (staged_path == nodes_path || staged_path == captures_path || staged_path == request_path || staged_path == output_path) {
    throw std::runtime_error("native exact output paths conflict");
  }
  request.RequireEnd();
  // One packed UTF-8 identity pool allows independent integer/name checks,
  // without recreating a name-to-ordinal hash table or per-edge objects.
  std::string names;
  std::vector<std::uint64_t> name_offsets{0};
  std::string previous;
  std::uint64_t launches = 0, dependencies = 0;
  std::vector<std::uint64_t> sizes(section_count, 0), expected_counts(section_count, 0);
  for (std::uint32_t i = 0; i < nodes.count; ++i) {
    const auto node = ReadExactHashNode(nodes);
    if (i && node.net <= previous) throw std::runtime_error("native exact cut order is invalid");
    previous = node.net; names += node.net; name_offsets.push_back(names.size());
    launches += node.flag != 0; dependencies += node.predecessors.size();
    const auto section = 2 + 2 * part_index(node.source);
    sizes[0] += 4 + node.net.size(); ++expected_counts[0];
    sizes[1] += 4 + 4ULL * node.sources.size(); ++expected_counts[1];
    sizes[section] += 14ULL * node.sources.size(); expected_counts[section] += node.sources.size();
  }
  const auto total_segments = launches + dependencies + captures.count;
  if (total_segments >= UINT32_MAX) throw std::runtime_error("native exact segment population is too large");
  for (std::uint32_t i = 0; i < captures.count; ++i) {
    const auto capture = ReadExactHashCapture(captures);
    const auto part = part_index(capture.fpga);
    sizes[2 + 2 * part] += 14; ++expected_counts[2 + 2 * part];
    sizes[3 + 2 * part] += 9 + 4 + capture.endpoint.size(); ++expected_counts[3 + 2 * part];
    if (capture.kind) sizes[3 + 2 * part] += 4 + capture.port.size() + (format == 1 ? 4 : 4 + capture.bit.size());
  }
  std::vector<std::uint64_t> positions{28};
  for (const auto size : sizes) positions.push_back(positions.back() + size);
  {
    std::ofstream staged(staged_path, std::ios::binary | std::ios::trunc);
    const std::array<char,28> preamble{};
    staged.write(preamble.data(), preamble.size()); staged.seekp(positions.back() - 1); staged.put(0); staged.close();
    if (!staged) throw std::runtime_error("cannot initialize native exact artifact");
  }
  std::vector<std::unique_ptr<ExactOutputSection>> sections;
  std::vector<std::uint64_t> counts(section_count, 0);
  for (std::size_t i = 0; i < section_count; ++i) sections.push_back(std::make_unique<ExactOutputSection>(staged_path, positions[i], sizes[i]));
  auto write_string = [&](std::size_t section, const std::string& value) {
    sections[section]->Write(static_cast<std::uint32_t>(value.size()));
    sections[section]->WriteBytes(value.data(), value.size());
  };
  auto segment = [&](std::size_t section, std::uint32_t id, std::uint8_t kind,
                     std::uint8_t flags, std::uint32_t source, std::uint32_t sink) {
    auto& writer = *sections[section];
    writer.Write(id); writer.Write(kind); writer.Write(flags); writer.Write(source); writer.Write(sink);
    ++counts[section];
  };
  nodes.Reset();
  std::uint32_t launch = 0, edge = static_cast<std::uint32_t>(launches);
  for (std::uint32_t i = 0; i < nodes.count; ++i) {
    const auto node = ReadExactHashNode(nodes);
    write_string(0, node.net); ++counts[0];
    sections[1]->Write(static_cast<std::uint32_t>(node.sources.size())); ++counts[1];
    if (node.flag) {
      if (node.sources[0] != ExactIdentity("segment", launch)) throw std::runtime_error("native exact launch numbering disagrees");
      sections[1]->Write(launch);
      segment(2 + 2 * part_index(node.source), launch++, 0, node.flag == 2, UINT32_MAX, i);
    }
    for (std::size_t j = 0; j < node.predecessors.size(); ++j) {
      const auto source = node.predecessor_indices[j];
      if (names.compare(name_offsets[source], name_offsets[source + 1] - name_offsets[source], node.predecessors[j]) != 0 ||
          node.sources[j + (node.flag != 0)] != ExactIdentity("segment", edge)) {
        throw std::runtime_error("native exact dependency coordinates disagree");
      }
      sections[1]->Write(edge++);
    }
  }
  if (launch != launches || edge != launches + dependencies) throw std::runtime_error("native exact source coverage disagrees");
  nodes.Reset(); edge = static_cast<std::uint32_t>(launches);
  for (std::uint32_t i = 0; i < nodes.count; ++i) {
    const auto node = ReadExactHashNode(nodes);
    for (const auto source : node.predecessor_indices) segment(2 + 2 * part_index(node.source), edge++, 1, 0, source, i);
  }
  nodes.Reset(); captures.Reset();
  std::uint32_t capture_number = 0;
  bool have_capture = captures.count != 0;
  ExactHashCapture capture;
  if (have_capture) capture = ReadExactHashCapture(captures);
  for (std::uint32_t i = 0; i < nodes.count; ++i) {
    const auto node = ReadExactHashNode(nodes);
    const auto first_capture = capture_number;
    while (have_capture && capture.net == node.net) {
      const auto part = part_index(capture.fpga);
      auto& writer = *sections[3 + 2 * part];
      writer.Write(capture_number); writer.Write(i); writer.Write(capture.kind);
      write_string(3 + 2 * part, capture.endpoint);
      if (capture.kind) {
        write_string(3 + 2 * part, capture.port);
        if (format == 1) {
          if (capture.bit.size() > 4) throw std::runtime_error("native exact legacy bit is invalid");
          std::uint64_t value = 0;
          for (const auto byte : capture.bit) value = value * 256 + static_cast<unsigned char>(byte);
          if (value >= UINT32_MAX) throw std::runtime_error("native exact legacy bit is invalid");
          writer.Write(static_cast<std::uint32_t>(value));
        } else {
          writer.Write(static_cast<std::uint32_t>(capture.bit.size())); writer.WriteBytes(capture.bit.data(), capture.bit.size());
        }
      }
      ++counts[3 + 2 * part];
      segment(2 + 2 * part, static_cast<std::uint32_t>(launches + dependencies + capture_number), 2, 0, i, capture_number);
      ++capture_number;
      have_capture = capture_number < captures.count;
      if (have_capture) capture = ReadExactHashCapture(captures);
    }
    if ((have_capture && capture.net < node.net) || node.captures.size() != capture_number - first_capture) {
      throw std::runtime_error("native exact capture coverage disagrees");
    }
    std::string last;
    for (const auto& id : node.captures) {
      if (id.size() < 13 || id.compare(0, 7, "segment") != 0 || (!last.empty() && id <= last)) {
        throw std::runtime_error("native exact capture identity is invalid");
      }
      const auto number = std::stoull(id.substr(7));
      if (id != ExactIdentity("segment", number) || number < launches + dependencies + first_capture ||
          number >= launches + dependencies + capture_number) {
        throw std::runtime_error("native exact capture numbering disagrees");
      }
      last = id;
    }
  }
  if (have_capture || capture_number != captures.count) throw std::runtime_error("native exact capture population disagrees");
  if (counts != expected_counts) throw std::runtime_error("native exact section counts disagree");
  emuflow::core::AtomicBinaryWriter summary(output_path);
  summary.Write(std::uint64_t{0x314F5753464D5545ULL}); summary.Write(std::uint32_t{2}); summary.Write(static_cast<std::uint32_t>(section_count));
  for (std::size_t i = 0; i < section_count; ++i) {
    const auto digest = sections[i]->Close();
    summary.Write(positions[i]); summary.Write(sizes[i]); summary.Write(counts[i]); summary.WriteBytes(digest.data(), digest.size());
  }
  for (const auto count : std::array<std::uint64_t,5>{nodes.count, launches + dependencies, total_segments,
                                                   dependencies + captures.count, captures.count}) summary.Write(count);
  summary.Commit();
}

#if !defined(EMUFLOW_NATIVE_STAGE_LIBRARY)
int main(int argc, char** argv) {
  if (argc == 2 && std::string(argv[1]) == "--help") {
    std::cout << "usage: emuflow_static_exact_analyzer [--indexed-input EMUIR] INPUT OUTPUT\n"
                 "       emuflow_static_exact_analyzer --hash-semantics NODES CAPTURES HEADER OUTPUT\n"
                 "       emuflow_static_exact_analyzer --write-exact-sections NODES CAPTURES REQUEST OUTPUT\n"
                 "       emuflow_static_exact_analyzer --finalize-semantics TOPOLOGY METADATA OUTPUT\n"
                 "       emuflow_static_exact_analyzer --resolve-captures EMUIR REQUEST TOPOLOGY PARTS OUTPUT\n";
    return 0;
  }
  if (argc == 6 && std::string(argv[1]) == "--write-exact-sections") {
    try {
      emuflow::native::WriteStaticExactSections(argv[2], argv[3], argv[4], argv[5]);
      return 0;
    } catch (const std::exception& error) {
      std::cerr << "emuflow_static_exact_analyzer: " << error.what() << '\n';
      return 1;
    }
  }
  if (argc == 6 && std::string(argv[1]) == "--hash-semantics") {
    try {
      emuflow::native::HashStaticExactSemantics(argv[2], argv[3], argv[4], argv[5]);
      return 0;
    } catch (const std::exception& error) {
      std::cerr << "emuflow_static_exact_analyzer: " << error.what() << '\n';
      return 1;
    }
  }
  if (argc == 7 && std::string(argv[1]) == "--resolve-captures") {
    try {
      emuflow::native::ResolveStaticExactCaptures(argv[2], argv[3], argv[4], argv[5], argv[6]);
      return 0;
    } catch (const std::exception& error) {
      std::cerr << "emuflow_static_exact_analyzer: " << error.what() << '\n';
      return 1;
    }
  }
  if (argc == 5 && std::string(argv[1]) == "--finalize-semantics") {
    try {
      emuflow::native::FinalizeStaticExact(argv[2], argv[3], argv[4]);
      return 0;
    } catch (const std::exception& error) {
      std::cerr << "emuflow_static_exact_analyzer: " << error.what() << '\n';
      return 1;
    }
  }
  if (argc == 5 && std::string(argv[1]) == "--indexed-input") {
    try {
      emuflow::native::AnalyzeStaticExact(argv[3], argv[4], argv[2]);
      return 0;
    } catch (const std::exception& error) {
      std::cerr << "emuflow_static_exact_analyzer: " << error.what() << '\n';
      return 1;
    }
  }
  if (argc != 3) {
    std::cerr << "usage: emuflow_static_exact_analyzer [--indexed-input EMUIR] INPUT OUTPUT\n";
    return 2;
  }
  try {
    emuflow::native::AnalyzeStaticExact(argv[1], argv[2]);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "emuflow_static_exact_analyzer: " << error.what() << '\n';
    return 1;
  }
}
#endif
