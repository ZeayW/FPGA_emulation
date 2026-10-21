#include <algorithm>
#include <array>
#include <cstdint>
#include <iostream>
#include <set>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "emuflow_core/artifact_io.hpp"

namespace {

constexpr std::uint64_t kInputMagic = 0x31584553464d5545ULL;
constexpr std::uint64_t kOutputMagic = 0x315845534f4d5545ULL;
constexpr std::uint32_t kVersion = 3;

struct Instance {
  bool state = false;
  std::uint32_t part = 0;
};

struct Sink {
  std::int32_t instance = -1;
};

struct Net {
  std::vector<std::int32_t> drivers;
  std::vector<Sink> sinks;
};

struct Cut {
  std::uint32_t net = 0;
  std::uint32_t source_part = 0;
  std::vector<std::uint32_t> sink_parts;
};

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

void Analyze(const std::string& input_path, const std::string& output_path) {
  emuflow::core::BinaryReader reader(input_path);
  if (reader.Read<std::uint64_t>() != kInputMagic ||
      reader.Read<std::uint32_t>() != kVersion) {
    throw std::runtime_error("unsupported native input header");
  }
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

  std::vector<Instance> instances(instance_count);
  for (auto& instance : instances) {
    const auto state = reader.Read<std::uint8_t>();
    instance.part = reader.Read<std::uint32_t>();
    if (state > 1 || instance.part >= part_count) {
      throw std::runtime_error("native instance record is invalid");
    }
    instance.state = state != 0;
  }

  RequireCount(net_count, 2 * sizeof(std::uint32_t), reader.remaining(),
               "native net");
  std::vector<Net> nets(net_count);
  std::uint64_t observed_endpoints = 0;
  for (std::uint32_t index = 0; index < net_count; ++index) {
    auto& net = nets[index];
    const auto drivers = reader.Read<std::uint32_t>();
    RequireCount(drivers, sizeof(std::int32_t), reader.remaining(),
                 "native net driver");
    net.drivers.resize(drivers);
    for (auto& driver : net.drivers) {
      driver = reader.Read<std::int32_t>();
      if (driver < -1 ||
          (driver >= 0 && static_cast<std::uint32_t>(driver) >= instance_count)) {
        throw std::runtime_error("native net driver is invalid");
      }
    }
    const auto sinks = reader.Read<std::uint32_t>();
    observed_endpoints += sinks;
    if (observed_endpoints > endpoint_count ||
        sinks > reader.remaining() / sizeof(std::int32_t)) {
      throw std::runtime_error("native endpoint count is invalid");
    }
    net.sinks.resize(sinks);
    for (auto& sink : net.sinks) {
      sink.instance = reader.Read<std::int32_t>();
      if (sink.instance < -1 ||
          (sink.instance >= 0 &&
           static_cast<std::uint32_t>(sink.instance) >= instance_count)) {
        throw std::runtime_error("native net sink is invalid");
      }
    }
  }
  if (observed_endpoints != endpoint_count) {
    throw std::runtime_error("native endpoint count is invalid");
  }

  RequireCount(cut_count, 3 * sizeof(std::uint32_t), reader.remaining(),
               "native cut");
  std::vector<Cut> cuts(cut_count);
  std::vector<std::int32_t> cut_by_net(net_count, -1);
  for (std::uint32_t index = 0; index < cut_count; ++index) {
    auto& cut = cuts[index];
    cut.net = reader.Read<std::uint32_t>();
    cut.source_part = reader.Read<std::uint32_t>();
    const auto sinks = reader.Read<std::uint32_t>();
    RequireCount(sinks, sizeof(std::uint32_t), reader.remaining(),
                 "native cut sink");
    cut.sink_parts.resize(sinks);
    if (cut.net >= net_count || cut.source_part >= part_count ||
        cut_by_net[cut.net] != -1) {
      throw std::runtime_error("native cut record is invalid");
    }
    cut_by_net[cut.net] = static_cast<std::int32_t>(index);
    std::uint32_t previous_sink = 0;
    bool have_sink = false;
    for (auto& sink : cut.sink_parts) {
      sink = reader.Read<std::uint32_t>();
      if (sink >= part_count || (have_sink && sink <= previous_sink)) {
        throw std::runtime_error("native cut sink is invalid");
      }
      previous_sink = sink;
      have_sink = true;
    }
  }
  reader.RequireEnd();

  std::vector<std::vector<std::uint32_t>> incoming(instance_count);
  std::vector<std::vector<std::uint32_t>> outgoing(instance_count);
  for (std::uint32_t net_index = 0; net_index < net_count; ++net_index) {
    for (const auto& sink : nets[net_index].sinks) {
      if (sink.instance >= 0) {
        incoming[static_cast<std::uint32_t>(sink.instance)].push_back(net_index);
      }
    }
    for (const auto driver : nets[net_index].drivers) {
      if (driver >= 0) {
        outgoing[static_cast<std::uint32_t>(driver)].push_back(net_index);
      }
    }
  }

  std::vector<std::vector<std::uint32_t>> dependencies(cut_count);
  std::vector<std::uint8_t> local_launches(cut_count, 0);
  GenerationMarks dependency_marks(instance_count);
  std::vector<std::uint32_t> work;
  for (std::uint32_t cut_index = 0; cut_index < cut_count; ++cut_index) {
    const auto& cut = cuts[cut_index];
    std::vector<std::uint32_t> drivers;
    for (const auto driver : nets[cut.net].drivers) {
      if (driver >= 0) drivers.push_back(static_cast<std::uint32_t>(driver));
    }
    std::sort(drivers.begin(), drivers.end());
    drivers.erase(std::unique(drivers.begin(), drivers.end()), drivers.end());
    if (drivers.size() != 1) {
      throw std::runtime_error("exact cut lacks one logic driver");
    }
    dependency_marks.Next();
    work = drivers;
    std::set<std::uint32_t> found;
    while (!work.empty()) {
      const auto instance_index = work.back();
      work.pop_back();
      if (!dependency_marks.Insert(instance_index)) continue;
      const auto& instance = instances[instance_index];
      if (instance.part != cut.source_part) {
        throw std::runtime_error("exact cut source cone crosses a boundary");
      }
      if (instance.state) {
        local_launches[cut_index] = 1;
        continue;
      }
      for (const auto incoming_net : incoming[instance_index]) {
        const auto predecessor = cut_by_net[incoming_net];
        if (predecessor >= 0) {
          const auto& predecessor_cut = cuts[static_cast<std::uint32_t>(predecessor)];
          if (std::binary_search(predecessor_cut.sink_parts.begin(),
                                 predecessor_cut.sink_parts.end(),
                                 cut.source_part)) {
            found.insert(static_cast<std::uint32_t>(predecessor));
            continue;
          }
        }
        for (const auto upstream : nets[incoming_net].drivers) {
          if (upstream < 0) {
            local_launches[cut_index] = 1;
          } else if (instances[static_cast<std::uint32_t>(upstream)].part ==
                     cut.source_part) {
            work.push_back(static_cast<std::uint32_t>(upstream));
          }
        }
      }
    }
    dependencies[cut_index].assign(found.begin(), found.end());
  }

  std::vector<Capture> captures;
  GenerationMarks capture_marks(instance_count);
  std::vector<EndpointRef> endpoint_work;
  for (std::uint32_t cut_index = 0; cut_index < cut_count; ++cut_index) {
    const auto& cut = cuts[cut_index];
    const auto& cut_net = nets[cut.net];
    for (const auto sink_part : cut.sink_parts) {
      endpoint_work.clear();
      for (std::uint32_t sink_index = 0;
           sink_index < cut_net.sinks.size(); ++sink_index) {
        const auto& endpoint = cut_net.sinks[sink_index];
        if (endpoint.instance < 0) {
          captures.push_back({cut_index, sink_part, cut.net, sink_index});
        } else if (instances[static_cast<std::uint32_t>(endpoint.instance)].part ==
                   sink_part) {
          endpoint_work.push_back({cut.net, sink_index});
        }
      }
      capture_marks.Next();
      while (!endpoint_work.empty()) {
        const auto endpoint_ref = endpoint_work.back();
        endpoint_work.pop_back();
        const auto& endpoint =
            nets[endpoint_ref.net].sinks[endpoint_ref.sink];
        const auto instance_index = static_cast<std::uint32_t>(endpoint.instance);
        if (instances[instance_index].state) {
          captures.push_back(
              {cut_index, sink_part, endpoint_ref.net, endpoint_ref.sink});
          continue;
        }
        if (!capture_marks.Insert(instance_index)) continue;
        for (const auto outgoing_net : outgoing[instance_index]) {
          const auto downstream = cut_by_net[outgoing_net];
          const auto& outgoing_sinks = nets[outgoing_net].sinks;
          for (std::uint32_t sink_index = 0;
               sink_index < outgoing_sinks.size(); ++sink_index) {
            const auto& sink = outgoing_sinks[sink_index];
            if (downstream >= 0 &&
                cuts[static_cast<std::uint32_t>(downstream)].source_part ==
                    sink_part &&
                sink.instance >= 0 &&
                instances[static_cast<std::uint32_t>(sink.instance)].part !=
                    sink_part) {
              continue;
            }
            if (sink.instance < 0) {
              captures.push_back(
                  {cut_index, sink_part, outgoing_net, sink_index});
            } else if (instances[static_cast<std::uint32_t>(sink.instance)].part ==
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
    const auto count = static_cast<std::uint32_t>(dependencies[cut_index].size());
    output.Write(count);
    for (const auto dependency : dependencies[cut_index]) {
      output.Write(dependency);
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
    Analyze(argv[1], argv[2]);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "emuflow_static_exact_analyzer: " << error.what() << '\n';
    return 1;
  }
}
