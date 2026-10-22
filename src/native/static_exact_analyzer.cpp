#include <algorithm>
#include <cstdint>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/native_stages.hpp"

namespace {

constexpr std::uint64_t kInputMagic = 0x31584553464d5545ULL;
constexpr std::uint64_t kOutputMagic = 0x315845534f4d5545ULL;
constexpr std::uint32_t kVersion = 3;
constexpr emuflow::core::ArtifactSchema kInputSchema{
    kInputMagic, kVersion, "Static Exact analyzer input"};

struct Capture {
  std::uint32_t cut = 0;
  std::uint32_t part = 0;
  std::uint32_t net = 0;
  std::uint32_t sink = 0;
};

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

void AnalyzeImpl(const std::string& input_path, const std::string& output_path) {
  emuflow::core::BinaryReader reader(input_path);
  emuflow::core::ReadAndValidateHeader(reader, kInputSchema);
  const auto instance_count = reader.Read<std::uint32_t>();
  const auto net_count = reader.Read<std::uint32_t>();
  const auto cut_count = reader.Read<std::uint32_t>();
  const auto part_count = reader.Read<std::uint32_t>();
  const auto endpoint_count = reader.Read<std::uint32_t>();
  if (part_count == 0) throw std::runtime_error("native input has no parts");
  RequireCount(instance_count, sizeof(std::uint8_t) + sizeof(std::uint32_t),
               reader.remaining(), "native instance");
  RequireCount(net_count, 2 * sizeof(std::uint32_t), reader.remaining(),
               "native net");
  RequireCount(cut_count, 3 * sizeof(std::uint32_t), reader.remaining(),
               "native cut");
  RequireCount(endpoint_count, sizeof(std::int32_t), reader.remaining(),
               "native endpoint");

  std::vector<std::uint8_t> instance_states(instance_count, 0);
  std::vector<std::uint32_t> instance_parts(instance_count, 0);
  for (std::uint32_t index = 0; index < instance_count; ++index) {
    const auto state = reader.Read<std::uint8_t>();
    const auto part = reader.Read<std::uint32_t>();
    if (state > 1 || part >= part_count) {
      throw std::runtime_error("native instance record is invalid");
    }
    instance_states[index] = state;
    instance_parts[index] = part;
  }

  RequireCount(net_count, 2 * sizeof(std::uint32_t), reader.remaining(),
               "native net");
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

  std::vector<Capture> captures;
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
          captures.push_back({cut_index, sink_part, cut_net, sink_index});
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
          captures.push_back(
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
              captures.push_back(
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
  std::sort(captures.begin(), captures.end(),
            [](const auto& left, const auto& right) {
              if (left.net != right.net) return left.net < right.net;
              if (left.sink != right.sink) return left.sink < right.sink;
              if (left.cut != right.cut) return left.cut < right.cut;
              return left.part < right.part;
            });
  captures.erase(std::unique(captures.begin(), captures.end(),
                             [](const auto& left, const auto& right) {
                               return left.cut == right.cut &&
                                      left.part == right.part &&
                                      left.net == right.net &&
                                      left.sink == right.sink;
                             }),
                 captures.end());

  emuflow::core::AtomicBinaryWriter output(output_path);
  output.Write(kOutputMagic);
  output.Write(kVersion);
  output.Write(cut_count);
  output.Write(static_cast<std::uint64_t>(captures.size()));
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
  for (const auto& capture : captures) {
    output.Write(capture.cut);
    output.Write(capture.part);
    output.Write(capture.net);
    output.Write(capture.sink);
  }
  output.Commit();
}

}  // namespace

void emuflow::native::AnalyzeStaticExact(const std::string& input_path,
                                         const std::string& output_path) {
  AnalyzeImpl(input_path, output_path);
}

#if !defined(EMUFLOW_NATIVE_STAGE_LIBRARY)
int main(int argc, char** argv) {
  if (argc == 2 && std::string(argv[1]) == "--help") {
    std::cout << "usage: emuflow_static_exact_analyzer INPUT OUTPUT\n";
    return 0;
  }
  if (argc != 3) {
    std::cerr << "usage: emuflow_static_exact_analyzer INPUT OUTPUT\n";
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
