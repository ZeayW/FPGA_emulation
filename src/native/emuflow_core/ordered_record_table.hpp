#pragma once
#include "emuflow_core/artifact_io.hpp"
#include <array>
#include <string>

namespace emuflow::core {
// Shared envelope and bounded record decoder. Identity/order and source
// semantics are independently checked by each consumer.
class OrderedRecordTable {
 public:
  OrderedRecordTable(const std::string& path, std::uint64_t magic, std::uint32_t version)
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

struct ExactNodeRecord {
  std::string net, kind, source;
  std::uint32_t level, depth;
  std::vector<std::string> sinks, predecessors, sources, captures;
  std::uint8_t flag;
  std::vector<std::uint32_t> predecessor_indices;
};

inline ExactNodeRecord ReadExactNodeRecord(OrderedRecordTable& table) {
  table.Begin(); auto& r = table.reader;
  ExactNodeRecord node;
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

struct ExactCaptureRecord { std::string net, fpga, endpoint, port, bit; std::uint8_t kind; };

inline ExactCaptureRecord ReadExactCaptureRecord(OrderedRecordTable& table) {
  table.Begin(); auto& r = table.reader;
  ExactCaptureRecord capture; capture.kind = r.Read<std::uint8_t>();
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

}  // namespace emuflow::core
