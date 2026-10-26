#include "emuflow_core/stage_runtime.hpp"

#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/sqlite.hpp"
#include "emuflow_core/store.hpp"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <sys/stat.h>
#if !defined(_WIN32)
#include <sys/resource.h>
#endif

namespace emuflow::core {
namespace {
constexpr RegisteredSchema schemas[] = {
    {"hop-refiner-input-v1", ArtifactEncoding::kText, 0, 1},
    {"hop-refiner-output-v1", ArtifactEncoding::kText, 0, 1},
    {"sqlite-publication-request-v1", ArtifactEncoding::kBinary, 0x31505351464d5545ULL, 1},
    {"boundary-identity-selector-v1", ArtifactEncoding::kText, 0, 1},
    {"boundary-timing-selector-v1", ArtifactEncoding::kText, 0, 1},
    {"logic-segment-identity-selector-v1", ArtifactEncoding::kText, 0, 1},
    {"logic-segment-timing-selector-v1", ArtifactEncoding::kText, 0, 1},
    {"local-path-timing-selector-v1", ArtifactEncoding::kText, 0, 1},
    {"openparf-atomic-source-selector-v1", ArtifactEncoding::kText, 0, 1},
    {"xilinx-mapped-json-v1", ArtifactEncoding::kText, 0, 1},
    {"transport-selector-v1", ArtifactEncoding::kText, 0, 1},
    {"physical-timing-measurements-v1", ArtifactEncoding::kText, 0, 1},
    {"vivado-local-identity-private-v1", ArtifactEncoding::kSqlite, 0, 0, true,
     "SELECT json_extract(value,'$.schema') FROM metadata WHERE key='header'", "emuflow.local-path-identity/v2"},
    {"physical-input-document-v1", ArtifactEncoding::kText, 0, 1},
    {"openparf-name-map-selector-v1", ArtifactEncoding::kText, 0, 1},
    {"openparf-placement-selector-v2", ArtifactEncoding::kText, 0, 2},
    {"openparf-name-map-store-v1", ArtifactEncoding::kSqlite, 0x454d554e, 1, true,
     "SELECT json_extract(value,'$.schema') FROM metadata WHERE key='header'", "emuflow.openparf-atomic-name-map/v2"},
    {"system-route-selector-v1", ArtifactEncoding::kText, 0, 1},
    {"yosys-source-document-v1", ArtifactEncoding::kText, 0, 1},
    {"sta-source-document-v1", ArtifactEncoding::kText, 0, 1},
    {"source-communication-tsv-v1", ArtifactEncoding::kText, 0, 1},
    {"source-communication-request-v1", ArtifactEncoding::kBinary, 0x31514353434d5545ULL, 1},
    {"source-metric-request-v1", ArtifactEncoding::kBinary, 0x31514d53434d5545ULL, 1},
    {"source-metric-weights-tsv-v1", ArtifactEncoding::kText, 0, 1},
    {"patron-source-proxy-tsv-v1", ArtifactEncoding::kText, 0, 1},
    {"patron-source-board-request-v1", ArtifactEncoding::kBinary, 0x3151505354415045ULL, 1},
    {"source-net-map-tsv-v1", ArtifactEncoding::kText, 0, 1},
    {"source-net-map-request-v1", ArtifactEncoding::kBinary, 0x31514d4e534d5545ULL, 1},
    {"sta-path-selector-v1", ArtifactEncoding::kText, 0, 1},
    {"net-weight-store-v1", ArtifactEncoding::kSqlite, 0, 1, true,
     "SELECT 'net-weight-store-v1' WHERE (SELECT count(*) FROM pragma_table_info('net_weights') "
     "WHERE name IN ('net_id','criticality','path_count','weight'))=4", "net-weight-store-v1"},
    {"vivado-optimization-proofs-v1", ArtifactEncoding::kSqlite, 0, 0, true,
     "SELECT json_extract(value,'$') FROM metadata WHERE key='schema'", "emuflow.vivado-optimization-proofs/v1"},
    {"system-qor-store-v1", ArtifactEncoding::kSqlite, 0x454d5551, 1, true,
     "SELECT json_extract(value,'$.schema') FROM metadata WHERE key='header'", "emuflow.qor-report/v4"},
    {"qor-report-selector-v1", ArtifactEncoding::kText, 0, 1},
    {"qor-report-store-v1", ArtifactEncoding::kSqlite, 0x454d5551, 1, true,
     "SELECT json_extract(value,'$.schema') FROM metadata WHERE key='header'", "emuflow.qor-report/v4"},
    {"openparf-site-store-v1", ArtifactEncoding::kSqlite, 0, 0, true,
     "SELECT value FROM metadata WHERE key='schema'", "emuflow.openparf-atomic-site-database/v1"},
    {"tdm-schedule-selector-v4", ArtifactEncoding::kText, 0, 4},
    {"pin-plan-selector-v1", ArtifactEncoding::kText, 0, 1},
    {"chimew-binding-selector-v1", ArtifactEncoding::kText, 0, 1},
    {"position-hints-selector-v1", ArtifactEncoding::kText, 0, 1},
    {"chimew-bank-report-selector-v1", ArtifactEncoding::kText, 0, 1},
    {"chimew-bank-input-selector-v1", ArtifactEncoding::kText, 0, 1},
    {"chimew-bank-input-store-v1", ArtifactEncoding::kSqlite, 0x454d5543, 1, false,
     "SELECT 'emuflow.chimew-bank-channel-storage/v1' FROM metadata WHERE key='header' AND "
     "json_extract(value,'$.schema') IN ('emuflow.chimew-bank-channel-input/v1','emuflow.chimew-bank-channel-input/v2')",
     "emuflow.chimew-bank-channel-storage/v1"},
    {"position-hints-store-v1", ArtifactEncoding::kSqlite, 0x454d5548, 1, false,
     "SELECT json_extract(value,'$.schema') FROM metadata WHERE key='header'", "emuflow.signal-position-hints/v1"},
    {"chimew-bank-report-store-v1", ArtifactEncoding::kSqlite, 0x454d5552, 1, false,
     "SELECT json_extract(value,'$.schema') FROM metadata WHERE key='header'", "emuflow.chimew-bank-channel-report/v1"},
    {"transport-store-v1", ArtifactEncoding::kSqlite, 0x454d5558, 1, false,
     "SELECT 'emuflow.transport-storage/v1' FROM metadata WHERE key='platform' AND json_type(value)='text' "
     "AND (SELECT json_type(value) FROM metadata WHERE key='design')='text' "
     "AND (SELECT json_type(value) FROM metadata WHERE key='frame_slots')='integer'", "emuflow.transport-storage/v1"},
    {"chimew-binding-store-v1", ArtifactEncoding::kSqlite, 0x454d5542, 1, false,
     "SELECT 'emuflow.chimew-binding-storage/v1' FROM metadata WHERE key='header' AND "
     "json_extract(value,'$.schema') IN ('emuflow.chimew-phase6-electrical-binding/v1',"
     "'emuflow.chimew-phase6-electrical-binding/v2','emuflow.chimew-phase6-electrical-binding/v3')",
     "emuflow.chimew-binding-storage/v1"},
    {"pin-plan-store-v1", ArtifactEncoding::kSqlite, 0x454d5550, 1, false,
     "SELECT json_extract(value,'$.schema') FROM metadata WHERE key='header'", "emuflow.placement-aware-pin-plan/v1"},
    {"atomic-ownership-request-v1", ArtifactEncoding::kBinary, 0x314f5341464d5545ULL, 1},
    {"atomic-ownership-summary-v1", ArtifactEncoding::kText, 0, 1},
    {"openparf-atomic-source-store-v1", ArtifactEncoding::kSqlite, 0x454d5541, 1, false,
     "SELECT json_extract(value,'$.schema') FROM metadata WHERE key='header'","emuflow.openparf-atomic-source/v1"},
    {"openparf-placement-certificate-v3", ArtifactEncoding::kSqlite, 0x454d5550, 3, false,
     "SELECT value FROM metadata WHERE key='schema'","emuflow.openparf-atomic-placement-database/v3"},
    {"physical-cell-check-request-v1", ArtifactEncoding::kBinary, 0x31564350464d5545ULL, 1},
    {"physical-cell-check-summary-v1", ArtifactEncoding::kText, 0, 1},
    {"cluster-catalog-request-v1", ArtifactEncoding::kBinary, 0x31514343464d5545ULL, 1},
    {"phase3-cluster-catalog-v3", ArtifactEncoding::kSqlite, 0x454d5543, 1, true,
     "SELECT 'emuflow.phase3-clusters-storage/v3' WHERE EXISTS(SELECT 1 FROM pragma_table_info('sections') WHERE name='compressed_bytes') "
     "AND EXISTS(SELECT 1 FROM pragma_table_info('section_chunks') WHERE name='payload') "
     "AND json_valid((SELECT value FROM metadata WHERE key='counts'))", "emuflow.phase3-clusters-storage/v3"},
    {"assignment-publication-request-v1", ArtifactEncoding::kBinary, 0x31514241554d5545ULL, 1},
    {"assignment-cut-validation-v1", ArtifactEncoding::kText, 0, 1},
    {"assignment-publication-request-v2", ArtifactEncoding::kBinary, 0x31514241554d5545ULL, 2},
    {"xilinx-physical-handoff-v1", ArtifactEncoding::kSqlite, 0x454d5550, 1, true,
     "SELECT schema FROM manifest WHERE singleton=1","emuflow.xilinx-physical-handoff-sqlite/v1"},
    {"rwroute-export-request-v2", ArtifactEncoding::kBinary, 0x31515752554d5545ULL, 2},
    {"rwroute-input-tsv-v1", ArtifactEncoding::kText, 0, 1},
    {"rwroute-export-summary-v1", ArtifactEncoding::kText, 0, 1},
    {"vivado-lineage-import-request-v1", ArtifactEncoding::kText, 0, 1},
    {"vivado-lineage-inventory-v2", ArtifactEncoding::kText, 0, 2},
    {"vivado-lineage-summary-v2", ArtifactEncoding::kText, 0, 2},
    {"vivado-lineage-store-v2", ArtifactEncoding::kSqlite, 0x454d564c, 2, true,
     "SELECT value FROM metadata WHERE key='schema'","emuflow.vivado-cell-lineage/v2"},
    {"vivado-source-functions-v1", ArtifactEncoding::kSqlite, 0x454d5646, 1, true,
     "SELECT value FROM metadata WHERE key='schema'","emuflow.vivado-source-functions/v1"},
    {"exact-capture-validation-v1", ArtifactEncoding::kText, 0, 1},
    {"partition-load-request-v1", ArtifactEncoding::kBinary, 0x3151454c504d5545ULL, 1},
    {"partition-load-summary-v1", ArtifactEncoding::kText, 0, 1},
    {"native-compile-request-v1", ArtifactEncoding::kText, 0, 1},
    {"native-compile-adapter-v1", ArtifactEncoding::kText, 0, 1},
    {"native-compile-state-v1", ArtifactEncoding::kText, 0, 1},
    {"multi-fpga-flow-report-v2", ArtifactEncoding::kText, 0, 2},
    {"external-tool-request-v1", ArtifactEncoding::kBinary, 0x31514552544c4645ULL, 1},
    {"external-tool-executable-v1", ArtifactEncoding::kText, 0, 1},
    {"emuir-compact-v2", ArtifactEncoding::kSqlite, 0x454d5549, 2, false,
     "SELECT value FROM metadata WHERE key='storage_schema'","emuflow.emuir-sqlite/v2"},
    {"emuir-indexed-v1", ArtifactEncoding::kSqlite, 0x454d5549, 1, true,
     "SELECT value FROM metadata WHERE key='storage_schema'","emuflow.emuir-sqlite/v1"},
    {"assignment-index-v3", ArtifactEncoding::kSqlite, 0x454d5541, 3, true,
     "SELECT json_extract(value,'$') FROM metadata WHERE key='schema'","emuflow.phase3-assignment-sqlite/v3"},
    {"assignment-index-v4", ArtifactEncoding::kSqlite, 0x454d5541, 4, true,
     "SELECT json_extract(value,'$') FROM metadata WHERE key='schema'","emuflow.phase3-assignment-sqlite/v4"},
    {"exact-semantic-binary-v2", ArtifactEncoding::kBinary, 0x5443415845554d45ULL, 2},
    {"source-launch-clock-store-v1", ArtifactEncoding::kSqlite, 0x454d534c, 1, true,
     "SELECT value FROM metadata WHERE key='schema'","emuflow.source-launch-clocks/v1"},
    {"source-launch-build-request-v1", ArtifactEncoding::kBinary, 0x3151424c534d5545ULL, 1},
    {"opensta-timing-input-v1", ArtifactEncoding::kText, 0, 1},
    {"split-netlists-v2", ArtifactEncoding::kSqlite, 0x454d5553, 2, true,
     "SELECT json_extract(value,'$') FROM metadata WHERE key='schema'","emuflow.split-netlists-sqlite/v2"},
    {"boundary-identity-store-v1", ArtifactEncoding::kSqlite, 0x454d5542, 1, true,
     "SELECT json_extract(value,'$.schema') FROM metadata WHERE key='header'","emuflow.boundary-identity/v1"},
    {"boundary-timing-store-v1", ArtifactEncoding::kSqlite, 0x454d5554, 1, true,
     "SELECT json_extract(value,'$.schema') FROM metadata WHERE key='header'","emuflow.boundary-timing/v1"},
    {"boundary-timing-store-v2", ArtifactEncoding::kSqlite, 0x454d5554, 2, true,
     "SELECT json_extract(value,'$.schema') FROM metadata WHERE key='header'","emuflow.boundary-timing/v1"},
    {"boundary-timing-store-v3", ArtifactEncoding::kSqlite, 0x454d5554, 3, true,
     "SELECT json_extract(value,'$.schema') FROM metadata WHERE key='header'","emuflow.boundary-timing/v1"},
    {"logic-segment-identity-store-v1", ArtifactEncoding::kSqlite, 0x454d5549, 1, true,
     "SELECT json_extract(value,'$.schema') FROM metadata WHERE key='header'","emuflow.logic-segment-identity/v1"},
    {"logic-segment-timing-store-v1", ArtifactEncoding::kSqlite, 0x454d5547, 1, true,
     "SELECT json_extract(value,'$.schema') FROM metadata WHERE key='header'","emuflow.logic-segment-timing/v1"},
    {"logic-segment-timing-store-v2", ArtifactEncoding::kSqlite, 0x454d5547, 2, true,
     "SELECT json_extract(value,'$.schema') FROM metadata WHERE key='header'","emuflow.logic-segment-timing/v1"},
    {"logic-segment-timing-store-v3", ArtifactEncoding::kSqlite, 0x454d5547, 3, true,
     "SELECT json_extract(value,'$.schema') FROM metadata WHERE key='header'","emuflow.logic-segment-timing/v1"},
    {"local-path-timing-store-v1", ArtifactEncoding::kSqlite, 0x454d554c, 1, true,
     "SELECT json_extract(value,'$.schema') FROM metadata WHERE key='header'","emuflow.local-path-timing/v1"},
    {"local-path-timing-store-v2", ArtifactEncoding::kSqlite, 0x454d554c, 2, true,
     "SELECT json_extract(value,'$.schema') FROM metadata WHERE key='header'","emuflow.local-path-timing/v1"},
    {"local-path-timing-store-v3", ArtifactEncoding::kSqlite, 0x454d554c, 3, true,
     "SELECT json_extract(value,'$.schema') FROM metadata WHERE key='header'","emuflow.local-path-timing/v1"},
    // Existing physical index relations have zero application/user PRAGMAs.
    // Their stage-owned metadata and complete ordered records remain mandatory.
    {"xilinx-mapped-index-v2", ArtifactEncoding::kSqlite, 0, 0, true,
     "SELECT value FROM metadata WHERE key='schema'","emuflow.xilinx-mapped-index/v2"},
    {"openparf-atom-index-private-v1", ArtifactEncoding::kSqlite, 0, 0, true,
     "SELECT 'openparf-atom-index-private-v1' WHERE (SELECT count(*) FROM pragma_table_info('atoms') "
     "WHERE name IN ('seq','instance','cell_type'))=3","openparf-atom-index-private-v1"},
    {"openparf-bookshelf-nets-v1", ArtifactEncoding::kText, 0, 1},
    {"openparf-bookshelf-summary-v1", ArtifactEncoding::kText, 0, 1},
    {"partition-cluster-columns-v2", ArtifactEncoding::kBinary, 0x314c4350464d5545ULL, 2},
    {"patron-initial-columns-v1", ArtifactEncoding::kBinary, 0x315450494e415045ULL, 1},
    {"patron-board-request-v2", ArtifactEncoding::kBinary, 0x3151455254415045ULL, 2},
    {"timing-path-store-v1", ArtifactEncoding::kSqlite, 0, 1, true,
     "SELECT 'timing-path-store-v1' WHERE (SELECT count(*) FROM pragma_table_info('paths') "
     "WHERE name IN ('ordinal','path_id','clock_domain','clock_period_ns','slack_ns','start_instance','end_instance'))=7 "
     "AND (SELECT count(*) FROM pragma_table_info('path_nets') WHERE name IN ('path_ordinal','net_ordinal','net_id'))=3",
     "timing-path-store-v1"},
    {"timing-path-store-v2", ArtifactEncoding::kSqlite, 0, 2, true,
     "SELECT 'timing-path-store-v2' WHERE (SELECT count(*) FROM pragma_table_info('paths') "
     "WHERE name IN ('ordinal','path_id','clock_domain','clock_period_ns','slack_ns','start_instance','end_instance'))=7 "
     "AND (SELECT count(*) FROM pragma_table_info('path_nets') WHERE name IN ('path_ordinal','net_ordinal','net_id'))=3 "
     "AND (SELECT count(*) FROM pragma_table_info('clock_budgets') WHERE name IN "
     "('path_ordinal','source_clock','source_period_ns','launch_edge_ns','capture_edge_ns','source_offset_ns',"
     "'target_cycle_time_ns','multicycle_adjustment_ns','check_margin_ns','source_latency_ns','target_clock_delay_ns',"
     "'clock_skew_ns','output_delay','path_delay','latch_check'))=15",
     "timing-path-store-v2"},
    {"patron-cluster-order-v1", ArtifactEncoding::kBinary, 0x3152444f54415045ULL, 1},
    {"patron-model-summary-v1", ArtifactEncoding::kText, 0, 1},
    {"exact-analysis-input-v3", ArtifactEncoding::kBinary, 0x31584553464d5545ULL, 3},
    {"exact-indexed-request-v4", ArtifactEncoding::kBinary, 0x31584553464d5545ULL, 4},
    {"exact-topology-v3", ArtifactEncoding::kBinary, 0x315845534f4d5545ULL, 3},
    {"exact-identities-v1", ArtifactEncoding::kBinary, 0x314d4553464d5545ULL, 1},
    {"exact-parts-v1", ArtifactEncoding::kBinary, 0x31504553464d5545ULL, 1},
    {"exact-nodes-v2", ArtifactEncoding::kBinary, 0x314e4553464d5545ULL, 2},
    {"exact-captures-v1", ArtifactEncoding::kBinary, 0x31434553464d5545ULL, 1},
    {"phase3-exact-data-v1", ArtifactEncoding::kBinary, 0x5443415845554d45ULL, 1},
    {"phase3-exact-data-v2", ArtifactEncoding::kBinary, 0x5443415845554d45ULL, 2},
    {"exact-hash-request-v1", ArtifactEncoding::kBinary, 0x31484553464d5545ULL, 1},
    {"exact-section-request-v1", ArtifactEncoding::kBinary, 0x31574553464d5545ULL, 1},
    {"exact-schedule-board-request-v2", ArtifactEncoding::kBinary, 0x3142534d44544545ULL, 2},
    {"candidate-policy-request-v1", ArtifactEncoding::kBinary, 0x31514543464d5545ULL, 1},
    {"candidate-id-columns-v1", ArtifactEncoding::kBinary, 0x31544343464d5545ULL, 1},
    {"partition-cluster-request-v1", ArtifactEncoding::kBinary, 0x31515243464d5545ULL, 1},
    {"patron-input-v1", ArtifactEncoding::kBinary, 0x314e525441504d45ULL, 1},
    {"chimew-bank-input-v1", ArtifactEncoding::kBinary, 0x0033434248434d45ULL, 1},
    {"chimew-group-input-v1", ArtifactEncoding::kBinary, 0x0033524748434d45ULL, 1},
    {"chimew-refine-input-v1", ArtifactEncoding::kBinary, 0x0032465248434d45ULL, 1},
    // Text compatibility/IPC schemas have stage-owned grammars. Binding does
    // not consume a pipe or duplicate their parser; the body checks the grammar.
    {"system-route-seal-request-v1", ArtifactEncoding::kBinary, 0x31515352464d5545ULL, 1},
    {"system-route-seal-summary-v1", ArtifactEncoding::kText, 0, 1},
    {"global-route-input-v1", ArtifactEncoding::kText, 0, 1},
    {"chimew-rudy-input-v1", ArtifactEncoding::kText, 0, 1},
    {"patron-output-v1", ArtifactEncoding::kBinary, 0x3154554f54415045ULL, 1},
    {"patron-output-text", ArtifactEncoding::kText, 0, 1},
    {"tlr-output-v1", ArtifactEncoding::kSqlite, 0x45545252, 1},
    {"system-route-store-v3", ArtifactEncoding::kSqlite, 0x454d5552, 3, false,
     "SELECT json_extract(value,'$.schema') FROM metadata WHERE key='header'","emuflow.system-routes/v1"},
    {"exact-schedule-generation-request-v1", ArtifactEncoding::kBinary, 0x3147534d44544545ULL, 1},
    {"tdm-schedule-store-v4", ArtifactEncoding::kSqlite, 0x454d5554, 3, false,
     "SELECT json_extract(value,'$') FROM metadata WHERE key='logical_schema'","emuflow.tdm-schedule/v1"},
    {"exact-sha256-text-v1", ArtifactEncoding::kText, 0, 1},
    {"exact-section-summary-v2", ArtifactEncoding::kBinary, 0x314f5753464d5545ULL, 2},
    {"chimew-bank-output-text", ArtifactEncoding::kText, 0, 1},
    {"chimew-group-output-text", ArtifactEncoding::kText, 0, 1},
    {"chimew-refine-output-text", ArtifactEncoding::kText, 0, 1},
    {"chimew-rudy-output-text", ArtifactEncoding::kText, 0, 1},
};

FileGeneration Generation(const std::string& path) {
#if defined(_WIN32)
  struct _stat64 status{};
  const auto code = _stat64(path.c_str(), &status);
#else
  struct stat status{};
  const auto code = stat(path.c_str(), &status);
#endif
  if (code != 0 || !std::filesystem::is_regular_file(path)) {
    throw StageError(ErrorCode::kMissingArtifact, "native input is not a regular file: " + path);
  }
  FileGeneration result;
  result.device = status.st_dev; result.inode = status.st_ino;
  result.bytes = status.st_size;
#if defined(__APPLE__)
  result.modified_seconds = status.st_mtimespec.tv_sec;
  result.modified_nanoseconds = status.st_mtimespec.tv_nsec;
  result.changed_seconds = status.st_ctimespec.tv_sec;
  result.changed_nanoseconds = status.st_ctimespec.tv_nsec;
#elif !defined(_WIN32)
  result.modified_seconds = status.st_mtim.tv_sec;
  result.modified_nanoseconds = status.st_mtim.tv_nsec;
  result.changed_seconds = status.st_ctim.tv_sec;
  result.changed_nanoseconds = status.st_ctim.tv_nsec;
#else
  result.modified_seconds = status.st_mtime;
  result.changed_seconds = status.st_ctime;
#endif
  return result;
}

struct Usage {
  double user = 0, system = 0;
  std::uint64_t peak_rss_kib = 0;
  bool has_io = false;
  std::uint64_t read_bytes = 0, write_bytes = 0;
};
void CheckFinalizedSqlite(const std::string& path) {
  for (const char* suffix : {"-wal", "-journal"}) {
    std::error_code error;
    const auto size = std::filesystem::file_size(path + suffix, error);
    if (!error && size != 0)
      throw StageError(ErrorCode::kInvalidArtifact, "native SQLite input has an unfinalized sidecar: " + path);
  }
}
Usage CurrentUsage() {
  Usage result;
#if !defined(_WIN32)
  struct rusage usage{};
  if (getrusage(RUSAGE_SELF, &usage) == 0) {
    result.user = usage.ru_utime.tv_sec + usage.ru_utime.tv_usec / 1e6;
    result.system = usage.ru_stime.tv_sec + usage.ru_stime.tv_usec / 1e6;
    result.peak_rss_kib = usage.ru_maxrss;
#if defined(__APPLE__)
    result.peak_rss_kib /= 1024;
#endif
  }
#endif
#if defined(__linux__)
  std::ifstream io("/proc/self/io");
  std::string name; std::uint64_t value = 0; unsigned fields = 0;
  while (io >> name >> value) {
    if (name == "read_bytes:") { result.read_bytes = value; fields |= 1; }
    if (name == "write_bytes:") { result.write_bytes = value; fields |= 2; }
  }
  result.has_io = fields == 3;
#endif
  return result;
}
std::string Quote(const std::string& value) {
  std::ostringstream out; out << '"';
  for (unsigned char c : value) {
    if (c == '"' || c == '\\') out << '\\' << c;
    else if (c < 32) out << "\\u00" << "0123456789abcdef"[c >> 4]
                        << "0123456789abcdef"[c & 15];
    else out << c;
  }
  out << '"'; return out.str();
}
}  // namespace

void RequireFinalizedSqlite(const std::string& path) { CheckFinalizedSqlite(path); }

const char* ErrorName(ErrorCode code) {
  switch (code) {
    case ErrorCode::kNone: return "none";
    case ErrorCode::kInvalidRequest: return "invalid-request";
    case ErrorCode::kMissingArtifact: return "missing-artifact";
    case ErrorCode::kInvalidArtifact: return "invalid-artifact";
    case ErrorCode::kMixedGeneration: return "mixed-generation";
    case ErrorCode::kExecutionFailed: return "execution-failed";
    case ErrorCode::kPublicationFailed: return "publication-failed";
  }
  return "execution-failed";
}
const RegisteredSchema& FindSchema(const std::string& name) {
  for (const auto& schema : SchemaRegistry()) if (schema.name == name) return schema;
  throw StageError(ErrorCode::kInvalidRequest, "unknown native artifact schema: " + name);
}
const std::vector<RegisteredSchema>& SchemaRegistry() {
  static const std::vector<RegisteredSchema> registry(std::begin(schemas),std::end(schemas));
  return registry;
}
bool FileGeneration::operator==(const FileGeneration& other) const {
  return device == other.device && inode == other.inode && bytes == other.bytes &&
      modified_seconds == other.modified_seconds && modified_nanoseconds == other.modified_nanoseconds &&
      changed_seconds == other.changed_seconds && changed_nanoseconds == other.changed_nanoseconds;
}
ArtifactRef ArtifactRef::Bind(const std::string& path, const std::string& schema_name,
                              bool allow_pipe) {
  const auto& schema = FindSchema(schema_name);
  ArtifactRef result; result.schema_ = schema_name;
  if (path == "-" && allow_pipe) {
    result.path_ = path; result.pipe_ = true; return result;
  }
  std::error_code error;
  result.path_ = std::filesystem::canonical(path, error).string();
  if (error) throw StageError(ErrorCode::kMissingArtifact, "cannot open native input: " + path);
  result.generation_ = Generation(result.path_);
  try {
    if (schema.encoding == ArtifactEncoding::kBinary) {
      BinaryReader reader(result.path_);
      ReadAndValidateHeader(reader, {schema.magic, schema.version, schema.name});
    } else if (schema.encoding == ArtifactEncoding::kSqlite) {
      RequireFinalizedSqlite(result.path_);
      SqliteDatabase database(result.path_, schema.immutable_read_only
          ? SqliteDatabase::Mode::kImmutableReadOnly : SqliteDatabase::Mode::kReadOnly);
      ValidateSqliteSchema(database,schema);
    }
  } catch (const StageError&) { throw; }
  catch (const std::exception& e) { throw StageError(ErrorCode::kInvalidArtifact, e.what()); }
  result.RequireUnchanged();
  return result;
}
void ArtifactRef::RequireUnchanged() const {
  if (pipe_) return;
  try {
    if (FindSchema(schema_).encoding == ArtifactEncoding::kSqlite)
      RequireFinalizedSqlite(path_);
    if (!(Generation(path_) == generation_))
      throw StageError(ErrorCode::kMixedGeneration, "native input generation changed: " + path_);
  } catch (const StageError& e) {
    if (e.code() == ErrorCode::kMixedGeneration) throw;
    throw StageError(ErrorCode::kMixedGeneration, "native input generation disappeared: " + path_);
  }
}
StageResult RunStage(const std::string& stage,
                     const std::function<std::vector<ArtifactRef>()>& bind,
                     const std::function<void(const std::vector<ArtifactRef>&)>& body,
                     const std::function<std::vector<ArtifactRef>()>& outputs) {
  StageResult result; result.stage = stage;
  const auto start = std::chrono::steady_clock::now();
  const auto before = CurrentUsage();
  try {
    if (stage.empty()) throw StageError(ErrorCode::kInvalidRequest, "native stage is missing");
    result.inputs = bind();
    for (const auto& input : result.inputs) input.RequireUnchanged();
    body(result.inputs);
    for (const auto& input : result.inputs) input.RequireUnchanged();
    if (outputs) result.outputs = outputs();
  } catch (const StageError& e) { result.error = e.code(); result.message = e.what(); }
  catch (const std::exception& e) { result.error = ErrorCode::kExecutionFailed; result.message = e.what(); }
  const auto after = CurrentUsage();
  result.metrics.wall_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
  result.metrics.user_cpu_seconds = after.user - before.user;
  result.metrics.system_cpu_seconds = after.system - before.system;
  result.metrics.process_peak_rss_kib = after.peak_rss_kib;
  if (before.has_io && after.has_io && after.read_bytes >= before.read_bytes && after.write_bytes >= before.write_bytes) {
    result.metrics.has_io_counters = true;
    result.metrics.read_bytes = after.read_bytes - before.read_bytes;
    result.metrics.write_bytes = after.write_bytes - before.write_bytes;
  }
  if (result.message.size() > 16384) {
    std::size_t end = 16384;
    while (end && (static_cast<unsigned char>(result.message[end]) & 0xc0) == 0x80) --end;
    result.message.resize(end);
    result.message += " [truncated]";
  }
  return result;
}
StageResult CheckNativeRuntime() {
  return RunStage("native.runtime-check", [] { return std::vector<ArtifactRef>{}; }, [](const auto&) {
    SqliteDatabase database(":memory:",SqliteDatabase::Mode::kReadOnly);
    auto probe = database.Prepare("SELECT sqlite_version(),json_extract('{\"probe\":true}','$.probe'),json_valid('{\"probe\":true}')");
    if (!probe.Step() || probe.Integer(1) != 1 || probe.Integer(2) != 1)
      throw StageError(ErrorCode::kExecutionFailed,"native SQLite JSON feature probe failed");
    std::istringstream version(probe.Text(0));
    int major = 0, minor = 0; char dot = 0;
    if (!(version >> major >> dot >> minor) || dot != '.' || major < 3 || (major == 3 && minor < 22))
      throw StageError(ErrorCode::kExecutionFailed,"native runtime requires SQLite 3.22 or newer");
  });
}
std::string StageResultJson(const StageResult& result) {
  std::ostringstream out; out << std::setprecision(17);
  out << "{\"schema\":\"emuflow.native-stage-result/v1\",\"stage\":" << Quote(result.stage)
      << ",\"status\":\"" << (result.ok() ? "pass" : "fail") << "\",\"error\":{\"code\":"
      << Quote(ErrorName(result.error)) << ",\"message\":" << Quote(result.message)
      << "},\"metrics\":{\"wall_seconds\":" << result.metrics.wall_seconds
      << ",\"user_cpu_seconds\":" << result.metrics.user_cpu_seconds
      << ",\"system_cpu_seconds\":" << result.metrics.system_cpu_seconds
      << ",\"cpu_scope\":\"self-process-interval\""
      << ",\"process_lifetime_peak_rss_kib\":" << result.metrics.process_peak_rss_kib
      << ",\"rss_scope\":\"self-lifetime-high-water\",\"io_scope\":\"self-proc-io-delta\",\"read_bytes\":";
  if (result.metrics.has_io_counters) out << result.metrics.read_bytes; else out << "null";
  out << ",\"write_bytes\":";
  if (result.metrics.has_io_counters) out << result.metrics.write_bytes; else out << "null";
  out << "},\"inputs\":";
  const auto write_refs = [&](const std::vector<ArtifactRef>& refs) {
    out << '[';
    bool first = true;
    for (const auto& input : refs) {
    if (!first) out << ',';
    first = false;
    const auto& g = input.generation();
    out << "{\"path\":" << Quote(input.path()) << ",\"schema\":" << Quote(input.schema())
        << ",\"pipe\":" << (input.is_pipe() ? "true" : "false")
        << ",\"generation\":{\"device\":" << g.device << ",\"inode\":" << g.inode
        << ",\"bytes\":" << g.bytes << ",\"mtime_seconds\":" << g.modified_seconds
        << ",\"mtime_nanoseconds\":" << g.modified_nanoseconds
        << ",\"ctime_seconds\":" << g.changed_seconds << ",\"ctime_nanoseconds\":" << g.changed_nanoseconds << "}}";
    }
    out << ']';
  };
  write_refs(result.inputs); out << ",\"outputs\":"; write_refs(result.outputs);
  out << "}\n"; return out.str();
}
void PublishStageResult(const std::string& path, const StageResult& result) {
  try {
    auto refs = result.inputs;
    refs.insert(refs.end(), result.outputs.begin(), result.outputs.end());
    for (const auto& input : refs) {
      std::error_code error;
      if (!input.is_pipe() && std::filesystem::equivalent(path, input.path(), error))
        throw StageError(ErrorCode::kInvalidRequest, "native report aliases a stage artifact");
    }
    const auto text = StageResultJson(result);
    AtomicBinaryWriter output(path); output.WriteBytes(text.data(), text.size()); output.Commit();
  } catch (const StageError&) { throw; }
  catch (const std::exception& e) { throw StageError(ErrorCode::kPublicationFailed, e.what()); }
}
}  // namespace emuflow::core
