#pragma once
#include "emuflow_core/emuir_view.hpp"
#include "emuflow_core/tool_runtime.hpp"
#include <map>

namespace emuflow::core {
struct SourceLaunchPoint {
  // -1 names an original top-level input; all other coordinates name cells.
  std::int64_t cell;
  std::uint64_t port, bit;
  // Original source endpoint in OpenSTA's SDC spelling, not Verilog text.
  std::string engine_pin;
};

// Resolves the producer's logical Vivado spelling by checking its exact
// forward recipe against original driver endpoints. Never uses an optimized
// actual_start_object as an architectural identity.
class SourceLaunchResolver {
 public:
  explicit SourceLaunchResolver(const ArtifactRef& source);
  SourceLaunchPoint Resolve(const std::string& physical_pin,
                            const std::string& object_kind = {});
  void RequireUnchanged() const;
 private:
  SqliteStore source_;
};

struct SourceLaunchContext {
  // These are existing, validated parent identities, not new graph payloads.
  std::string original_ir_sha256, path_database_sha256, routes_sha256;
  std::string schedule_sha256, semantic_sha256, timing_io_sha256;
  // Paths in the tool environment bind the source Verilog, Liberty, original
  // clocks and I/O. The caller uses the same indexed source emitter as Phase2.
  ToolRequest tool;
  std::vector<ArtifactRef> timing_inputs;
  // Hashes supplied by the same invocation's validated emitters/tool probe.
  // Generations bind those existing seals; this consumer does not rehash them.
  std::map<std::string,std::string> timing_input_sha256;
  std::vector<ArtifactRef> control_inputs;
  const volatile std::sig_atomic_t* cancelled = nullptr;
};

// One native owner and one tool invocation. Physical stores are scanned once
// for complete-contract launch queries and implemented original local starts.
// Only original integer coordinates, clock identities and phase facts persist;
// query names/requests are temporary. Existing output survives any rejection.
ArtifactRef BuildSourceLaunchClocks(const ArtifactRef& source,
    const std::vector<ArtifactRef>& logic_costs,
    const std::vector<ArtifactRef>& local_costs,
    const SourceLaunchContext& context, const std::string& output);
std::vector<std::string> SourceLaunchInputSchemas(const std::string& request);
void BuildSourceLaunchClocksFromRequest(const std::vector<ArtifactRef>& inputs,
    const std::string& output,const volatile std::sig_atomic_t* cancelled = nullptr);

class SourceLaunchClockView {
 public:
  // The caller supplies the original IR's validated parent seal. File
  // generations are retained; this view does not rehash the original graph.
  SourceLaunchClockView(const ArtifactRef& clocks, const ArtifactRef& source,
                        const std::string& original_ir_sha256);
  std::vector<double> LaunchTimes(const std::string& physical_pin,
      const std::string& object_kind,
      const std::map<std::string,double>& target_periods_ns);
  void RequireUnchanged() const;
 private:
  SqliteStore clocks_;
  SourceLaunchResolver source_;
};
}  // namespace emuflow::core
