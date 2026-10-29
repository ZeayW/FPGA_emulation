#pragma once
#include "physical/flow/controller.hpp"
#include "physical/openparf/input_records.hpp"
#include "storage/store.hpp"
#include <memory>
namespace emuflow::core::physical_flow {
namespace fs = std::filesystem;
using openparf_input::R;
using O = R::O;
using A = R::A;
using I = std::int64_t;
using Owners = std::map<std::string, ArtifactRef>;
void Check(bool value, const std::string &why);
R Generation(const ArtifactRef &ref);
R Source(const std::string &role, const ArtifactRef &ref);
R Metadata(const ArtifactRef &ref, const std::string &key);
R Document(const fs::path &path);
void Keep(const StageResult &result);
ArtifactRef Request(const fs::path &path, const R &document,
                    const std::string &schema,
                    const std::vector<ArtifactRef> &refs);
ArtifactRef Resolve(const ArtifactRef &selector, const char *member,
                    const std::string &schema);
struct Partition {
  std::string id, part;
  I index = 0, cells = 0, nets = 0, ports = 0, endpoints = 0;
  R budget, split_item, io;
  bool active() const { return cells || nets || ports || endpoints; }
};
struct WorkerResult {
  R record;
  std::vector<ArtifactRef> artifacts;
};
struct Lease {
  fs::path root, generation;
  int fd = -1;
  Lease(const std::string &output, const std::vector<ArtifactRef> &refs,
        bool resume);
  ~Lease();
};
struct Context {
  ArtifactRef request;
  const volatile std::sig_atomic_t *cancelled;
  R config, manifest, board, schedule_selector, runtime, descriptor,
      architecture;
  Owners owners;
  std::vector<ArtifactRef> sources;
  std::vector<Partition> partitions;
  std::vector<std::size_t> selected;
  std::vector<ArtifactRef> logic_sources, local_sources;
  std::unique_ptr<Lease> lease;
  std::string backend, design, platform;
  bool resume = false, queries = false, partial = false;
  I workers = 1, seed = 1;
  Context(const ArtifactRef &input, const volatile std::sig_atomic_t *flag)
      : request(input), cancelled(flag) {}
  void Stop() const;
  void Admit(const std::string &output);
  void Prepare();
  WorkerResult Implement(const Partition &partition);
  R Assemble(const std::vector<R> &records);
  R Batch(const std::vector<R> &records);
  R Query(const Partition &partition, const ArtifactRef &merged,
          const ArtifactRef &boundary, const fs::path &directory, bool local,
          std::vector<ArtifactRef> &proof) const;
  ArtifactRef Relative(const std::string &path,
                       const std::string &schema) const;
};
} // namespace emuflow::core::physical_flow
