#pragma once
#include "emuflow_core/store.hpp"
#include <cstdint>
#include <functional>
#include <vector>

namespace emuflow::core {
struct ExactLogicCoordinate {
  // kind: launch=0, cut transition=1, capture=2. Missing source is UINT32_MAX;
  // capture target names a capture requirement, not a cut coordinate.
  std::uint32_t source, target, part;
  std::uint8_t kind;
  bool configuration_stable;
};
struct ExactSourceRange {
  const std::uint32_t* first;
  const std::uint32_t* last;
  const std::uint32_t* begin() const { return first; }
  const std::uint32_t* end() const { return last; }
};
struct ExactCaptureEndpoint {
  std::uint32_t capture,segment,cut,part;
  // top-output=0, architectural-state=1. Architectural bits retain their
  // canonical big-endian encoding, including the format's 8192-bit domain.
  std::uint8_t kind;
  std::string endpoint,port;
  std::vector<std::uint8_t> bit;
};

// One native consumer of the published v4 index and its v2 semantic sidecar.
// Required sections are checked during their single visit. No logical JSON
// reconstruction, net-name dictionary or second payload hash pass is created.
class ExactSemanticView {
 public:
  ExactSemanticView(const ArtifactRef& assignment, const ArtifactRef& semantic);
  std::uint32_t cut_count() const { return source_parts_.size(); }
  const std::vector<std::string>& parts() const { return parts_; }
  const std::vector<ExactLogicCoordinate>& segments() const { return segments_; }
  std::uint32_t SourcePart(std::uint32_t cut) const;
  ExactSourceRange Sources(std::uint32_t cut) const;
  // Stream every semantic capture, including endpoints absent from the
  // selected original path library. Each record must own exactly one capture
  // segment in this partition. The callback's record is invocation-scoped.
  void VisitCaptures(const std::function<void(const ExactCaptureEndpoint&)>& visitor) const;
  const std::string& semantic_sha256() const { return semantic_sha256_; }
  void RequireUnchanged() const;
 private:
  ArtifactRef assignment_, semantic_;
  std::string semantic_sha256_;
  std::vector<std::string> parts_;
  std::vector<std::uint32_t> source_parts_, source_ids_;
  std::vector<std::uint64_t> offsets_;
  std::vector<ExactLogicCoordinate> segments_;
  struct CaptureSection { std::uint64_t offset,bytes,records;std::string sha256; };
  std::vector<CaptureSection> capture_sections_;
  std::vector<std::uint32_t> capture_segments_;
};
}  // namespace emuflow::core
