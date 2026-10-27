#include "partition/cluster_catalog.hpp"
#include "storage/schema/cluster_catalog_schema.hpp"
#include "common/artifact_io.hpp"
#include "ir/emuir_view.hpp"
#include "common/json_record.hpp"
#include "partition/primary_cluster_view.hpp"
#include "ir/resource_view.hpp"
#include "storage/store.hpp"
#include <zlib.h>
#include <algorithm>
#include <array>
#include <cstdio>
#include <climits>
#include <limits>
#include <map>
#include <memory>
#include <numeric>

namespace emuflow::core {
namespace {
void Check(bool valid, const char* message) {
  if (!valid) throw StageError(ErrorCode::kInvalidArtifact, message);
}
// Each compressor and its pending SQL chunk is bounded independently of the
// circuit. Records retain the existing zlib-level-1 column contract.
class Section {
 public:
  Section(SqliteDatabase& database, std::string name)
      : name_(std::move(name)), chunks_(database.Prepare("INSERT INTO section_chunks VALUES(?,?,?)")),
        summary_(database.Prepare("INSERT INTO sections VALUES(?,?,?,?,?,?)")) {
    Check(deflateInit(&stream_, 1) == Z_OK, "cannot initialize cluster compression");
    initialized_ = true; pending_.reserve(kChunk);
  }
  ~Section() { if (initialized_) deflateEnd(&stream_); }
  void Integer(std::uint32_t value) { Bytes(&value, sizeof(value)); ++records_; }
  void String(const std::string& value) {
    Check(value.size() < UINT32_MAX, "cluster string exceeds uint32 size");
    const auto size = static_cast<std::uint32_t>(value.size());
    Bytes(&size, sizeof(size)); Bytes(value.data(), value.size()); ++records_;
  }
  void Finish() {
    int status;
    do {
      stream_.next_out = buffer_.data(); stream_.avail_out = buffer_.size();
      status = deflate(&stream_, Z_FINISH);
      Check(status == Z_OK || status == Z_STREAM_END, "cluster compression finalization failed");
      Append(buffer_.data(), buffer_.size() - stream_.avail_out);
    } while (status != Z_STREAM_END);
    if (!pending_.empty()) Flush();
    summary_.BindText(1, name_); summary_.BindText(2, "zlib-level-1-typed-columns");
    summary_.BindInteger(3, records_); summary_.BindInteger(4, raw_);
    summary_.BindInteger(5, compressed_); summary_.BindInteger(6, chunk_);
    summary_.Execute(); summary_.Reset();
  }
 private:
  void Bytes(const void* data, std::size_t count) {
    raw_ += count;
    const auto* bytes = static_cast<const unsigned char*>(data);
    while (count) {
      const auto size = std::min<std::size_t>(count, UINT_MAX);
      stream_.next_in = const_cast<Bytef*>(bytes); stream_.avail_in = size;
      do {
        stream_.next_out = buffer_.data(); stream_.avail_out = buffer_.size();
        Check(deflate(&stream_, Z_NO_FLUSH) == Z_OK, "cluster compression failed");
        Append(buffer_.data(), buffer_.size() - stream_.avail_out);
      } while (stream_.avail_in);
      bytes += size; count -= size;
    }
  }
  void Append(const unsigned char* data, std::size_t count) {
    compressed_ += count;
    while (count) {
      auto size = std::min(count, kChunk - pending_.size());
      pending_.insert(pending_.end(), data, data + size); data += size; count -= size;
      if (pending_.size() == kChunk) Flush();
    }
  }
  void Flush() {
    chunks_.BindText(1, name_); chunks_.BindInteger(2, chunk_++);
    chunks_.BindBlob(3, pending_.data(), pending_.size()); chunks_.Execute(); chunks_.Reset();
    pending_.clear();
  }
  static constexpr std::size_t kChunk = 1024 * 1024;
  std::string name_;
  SqliteStatement chunks_, summary_;
  z_stream stream_{};
  bool initialized_ = false;
  std::array<unsigned char, 65536> buffer_{};
  std::vector<unsigned char> pending_;
  std::uint64_t records_ = 0, raw_ = 0, compressed_ = 0, chunk_ = 0;
};
FileGeneration Generation(CheckedRequestReader& request) {
  return {request.Read<std::uint64_t>(), request.Read<std::uint64_t>(), request.Read<std::uint64_t>(),
          request.Read<std::int64_t>(), request.Read<std::int64_t>(),
          request.Read<std::int64_t>(), request.Read<std::int64_t>()};
}
std::string Identity(CheckedRequestReader& request) {
  auto value = request.ReadString32();
  Check(value.find('\0') == std::string::npos, "cluster identity contains NUL");
  JsonQuote(value); return value;
}
std::vector<unsigned char> CandidateBits(const ArtifactRef& artifact, std::uint64_t nets, std::uint64_t& name_bytes) {
  CheckedRequestReader reader(artifact.path(), artifact.generation().bytes);
  Check(reader.Read<std::uint64_t>() == 0x31544343464d5545ULL && reader.Read<std::uint32_t>() == 1,
        "unsupported candidate columns");
  Check(reader.Read<std::uint64_t>() == nets, "candidate/source net counts disagree");
  const auto eligible = reader.Read<std::uint64_t>();
  Check(eligible <= nets, "candidate count exceeds source domain");
  std::vector<unsigned char> bits((nets + 7) / 8);
  reader.ReadBytes(bits.data(), bits.size());
  // The candidate name pool is a derived view; the bitmap is authoritative for
  // this consumer. Validate its complete envelope/CRC without rebuilding names.
  const auto remaining = reader.remaining();
  Check(remaining >= 8 * (eligible + 1) + 8, "candidate columns are truncated");
  const auto names = remaining - 8 * (eligible + 1) - 8;
  name_bytes = names;
  std::array<unsigned char, 65536> buffer{};
  for (std::uint64_t left = names; left;) {
    auto size = std::min<std::uint64_t>(left, buffer.size()); reader.ReadBytes(buffer.data(), size); left -= size;
  }
  std::uint64_t previous = 0;
  for (std::uint64_t i = 0; i <= eligible; ++i) {
    auto offset = reader.Read<std::uint64_t>();
    Check(offset >= previous && offset <= names && (i || offset == 0), "candidate name offsets disagree");
    previous = offset;
  }
  Check(previous == names && reader.Read<std::uint64_t>() == names, "candidate name bounds disagree");
  reader.RequireEnd();
  std::uint64_t observed = 0;
  for (auto byte : bits) for (; byte; byte &= byte - 1) ++observed;
  Check(observed == eligible, "candidate bitmap count disagrees");
  return bits;
}
}

void PublishClusterCatalog(const std::vector<ArtifactRef>& inputs, const std::string& output) {
  Check(inputs.size() == 3 || inputs.size() == 4, "cluster catalog input coverage disagrees");
  CheckedRequestReader request(inputs[2].path(), inputs[2].generation().bytes);
  Check(request.Read<std::uint64_t>() == 0x31514343464d5545ULL && request.Read<std::uint32_t>() == 1,
        "unsupported cluster catalog request");
  for (unsigned index : {0U, 1U})
    if (!(Generation(request) == inputs[index].generation()))
      throw StageError(ErrorCode::kMixedGeneration, "cluster catalog source generation changed");
  PrimaryClusterView primary(inputs[1], inputs[0]); EmuirView source(inputs[0]);
  primary.ValidateMembers(source);
  const auto document = request.ReadString32(false, request.remaining());
  SqliteArtifactWriter writer(output, "phase3-cluster-catalog-v3", inputs);
  {
    auto& database = writer.database();
    database.Execute("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE; PRAGMA cache_size=-65536;");
    database.Execute(kClusterCatalogDDL);
    auto json = database.Prepare("SELECT json_valid(?),json_extract(?,'$.schema')");
    json.BindText(1, document); json.BindText(2, document);
    Check(json.Step() && json.TypedInteger(0) == 1 && json.TypedText(1) == "emuflow.clusters/v1",
          "cluster catalog document is invalid");
    std::map<std::string, std::unique_ptr<Section>> sections;
    for (const auto* name : {"cluster_ids", "member_counts", "member_names", "resource_counts", "resource_names", "resource_values", "fixed_indices", "fixed_values", "group_counts", "group_values", "extra_values", "eligible_nets", "document"})
      sections.emplace(name, std::make_unique<Section>(database, name));
    auto& eligible = *sections.at("eligible_nets");
    const auto present = request.Read<std::uint32_t>(), mode = request.Read<std::uint32_t>();
    Check(present <= 1 && mode <= 2 && (inputs.size() == 4) == (mode == 0), "cluster eligible input kind disagrees");
    std::uint64_t eligible_count = 0;
    if (mode <= 1) {
      std::vector<unsigned char> bits;
      std::uint64_t candidate_name_bytes=0, candidate_offset=0;
      std::unique_ptr<BinaryReader> candidate_names, candidate_offsets;
      if (mode == 0) {
        if (!(Generation(request) == inputs[3].generation()))
          throw StageError(ErrorCode::kMixedGeneration, "cluster candidate generation changed");
        bits = CandidateBits(inputs[3], source.counts().nets, candidate_name_bytes);
        candidate_names=std::make_unique<BinaryReader>(inputs[3].path());
        candidate_offsets=std::make_unique<BinaryReader>(inputs[3].path());
        candidate_names->Seek(28+bits.size());
        candidate_offsets->Seek(28+bits.size()+candidate_name_bytes);
        Check(candidate_offsets->Read<std::uint64_t>()==0,"candidate first name offset disagrees");
      } else {
        const auto bytes = request.Read<std::uint64_t>();
        Check(bytes == (source.counts().nets + 7) / 8 && bytes <= request.remaining(), "cluster eligible bitmap size disagrees");
        bits.resize(bytes); request.ReadBytes(bits.data(), bits.size());
      }
      if (source.counts().nets % 8 && !bits.empty())
        Check((bits.back() >> (source.counts().nets % 8)) == 0, "cluster eligible bitmap padding is invalid");
      auto rows = source.store().database().Prepare("SELECT seq,id FROM net_records ORDER BY id");
      while (rows.Step()) {
        auto net = rows.TypedInteger(0);
        Check(net >= 0 && static_cast<std::uint64_t>(net) < source.counts().nets, "cluster eligible net identity is invalid");
        if (bits[net >> 3] & (1U << (net & 7))) {
          auto name=rows.TypedText(1);
          if(candidate_names) {
            auto end=candidate_offsets->Read<std::uint64_t>();
            Check(end>=candidate_offset && end-candidate_offset==name.size(),"candidate name/source identity disagrees");
            std::string actual(name.size(),'\0'); candidate_names->ReadBytes(actual.data(),actual.size());
            Check(actual==name,"candidate name/source identity disagrees"); candidate_offset=end;
          }
          eligible.String(name); ++eligible_count;
        }
      }
      Check(!candidate_names || candidate_offset==candidate_name_bytes,"candidate name coverage disagrees");
    } else {
      eligible_count = request.Read<std::uint64_t>();
      Check(eligible_count <= request.remaining() / 4, "cluster eligible names are truncated");
      for (std::uint64_t i = 0; i < eligible_count; ++i) eligible.String(Identity(request));
    }
    Check(present || !eligible_count, "absent cluster eligibility has records");
    std::map<std::uint32_t, std::string> fixed;
    auto fixed_count = request.Read<std::uint64_t>();
    Check(fixed_count <= primary.clusters() && fixed_count <= request.remaining() / 8, "cluster fixed constraints are truncated");
    std::uint32_t previous = 0;
    for (std::uint64_t i = 0; i < fixed_count; ++i) {
      const auto cluster = request.Read<std::uint32_t>();
      Check(cluster < primary.clusters() && (!i || previous < cluster), "cluster fixed constraint order disagrees"); previous = cluster;
      fixed.emplace(cluster, Identity(request));
    }
    std::map<std::uint32_t, std::vector<std::string>> groups;
    auto group_count = request.Read<std::uint64_t>();
    Check(group_count <= primary.clusters() && group_count <= request.remaining() / 8, "cluster group constraints are truncated");
    for (std::uint64_t i = 0; i < group_count; ++i) {
      auto cluster = request.Read<std::uint32_t>(), count = request.Read<std::uint32_t>();
      Check(cluster < primary.clusters() && (!i || previous < cluster) && count && count <= request.remaining() / 4,
            "cluster group constraint order/count disagrees"); previous = cluster;
      auto& values = groups[cluster];
      for (std::uint32_t j = 0; j < count; ++j) values.push_back(Identity(request));
    }
    request.RequireEnd();
    std::array<unsigned, 12> order{}; std::iota(order.begin(), order.end(), 0);
    std::sort(order.begin(), order.end(), [](auto a, auto b) { return std::string_view(kResourceFields[a]) < kResourceFields[b]; });
    for (std::uint64_t cluster = 0; cluster < primary.clusters(); ++cluster) {
      char id[32]; std::snprintf(id, sizeof(id), "c%06llu", static_cast<unsigned long long>(cluster));
      sections.at("cluster_ids")->String(id);
      const auto members = primary.Weight(cluster, 0);
      sections.at("member_counts")->Integer(members);
      for (std::uint64_t i = 0; i < members; ++i) sections.at("member_names")->String(primary.Name(primary.Member(cluster, i)));
      std::uint32_t resources = 0;
      for (auto dimension : order) if (primary.Weight(cluster, dimension + 1)) ++resources;
      sections.at("resource_counts")->Integer(resources);
      for (auto dimension : order) if (auto amount = primary.Weight(cluster, dimension + 1)) {
        sections.at("resource_names")->String(kResourceFields[dimension]);
        sections.at("resource_values")->String(std::to_string(amount));
      }
      if (auto found = fixed.find(cluster); found != fixed.end()) {
        sections.at("fixed_indices")->Integer(cluster); sections.at("fixed_values")->String(found->second);
      }
      auto found = groups.find(cluster);
      sections.at("group_counts")->Integer(found == groups.end() ? 0 : found->second.size());
      if (found != groups.end()) for (const auto& value : found->second) sections.at("group_values")->String(value);
      sections.at("extra_values")->String("{}");
    }
    sections.at("document")->String(document);
    for (auto& section : sections) section.second->Finish();
    const auto counts = JsonRecord(std::map<std::string, JsonRecord>{{"clusters", primary.clusters()}, {"instances", primary.cells()}, {"eligible_nets", eligible_count}}).Encode();
    auto put = database.Prepare("INSERT INTO metadata VALUES(?,?)");
    put.BindText(1, "counts"); put.BindText(2, counts); put.Execute(); put.Reset();
    put.BindText(1, "eligible_nets_present"); put.BindText(2, present ? "true" : "false"); put.Execute();
    sections.clear();
  }
  writer.Commit();
}
}
