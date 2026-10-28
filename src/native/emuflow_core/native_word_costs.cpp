#include "emuflow_core/native_word_costs.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/stage_runtime.hpp"
#include <array>
#include <charconv>
#include <cmath>
#include <limits>
#include <string_view>
#include <vector>

namespace emuflow::core {
namespace {
constexpr std::size_t kMaximumRow = 256 * 1024;
constexpr const char* kHeader =
    "phase_word\tquery_hex\tsource_kind\tsource_hex\tlaunch_edge\tmeasurement\t"
    "requirement_ns\tdata_ns\tsampling_cost_ns\tclock_adjustment_ns\tstart_hex\t"
    "end_hex\tstart_clock_hex\tend_clock_hex\tlaunch_ns\tcapture_ns";
void Check(bool valid, const char* message) {
  if (!valid) throw StageError(ErrorCode::kInvalidArtifact, message);
}
bool Line(std::istream& input, std::array<char,kMaximumRow+2>& buffer,
          std::string_view& line) {
  input.getline(buffer.data(), buffer.size());
  Check(!input.bad() && !(input.fail() && !input.eof()),
        "native word cost record is unreadable or exceeds its bound");
  const auto count = input.gcount();
  if (!count) return false;
  const auto length = static_cast<std::size_t>(count) - (input.eof() ? 0 : 1);
  Check(length <= kMaximumRow, "native word cost record exceeds its bound");
  line = std::string_view(buffer.data(),length);
  Check(line.find('\0') == std::string_view::npos &&
        line.find('\r') == std::string_view::npos,
        "native word cost record contains an invalid delimiter");
  return true;
}
std::vector<std::string_view> Fields(std::string_view line) {
  std::vector<std::string_view> result;
  std::size_t begin = 0;
  while (true) {
    const auto end = line.find('\t',begin);
    result.push_back(line.substr(begin,end == std::string_view::npos ? end : end-begin));
    Check(result.size() <= 16, "native word cost column count differs");
    if (end == std::string_view::npos) break;
    begin = end+1;
  }
  Check(result.size() == 16, "native word cost column count differs");
  return result;
}
std::string Identity(std::string_view value) {
  Check(!value.empty() && value.size()%2 == 0, "native word cost identity encoding is invalid");
  const auto nibble = [](char c) {
    return c >= '0' && c <= '9' ? c-'0' : c >= 'a' && c <= 'f' ? c-'a'+10 : -1;
  };
  std::string result;
  result.reserve(value.size()/2);
  for (std::size_t i=0; i<value.size(); i+=2) {
    const auto high = nibble(value[i]), low = nibble(value[i+1]);
    Check(high >= 0 && low >= 0, "native word cost identity encoding is invalid");
    const auto c = static_cast<char>(16*high+low);
    Check(c != '\0', "native word cost identity contains NUL");
    result += c;
  }
  try { (void)JsonQuote(result); }
  catch (const std::exception&) {
    throw StageError(ErrorCode::kInvalidArtifact,"native word cost identity is not UTF-8");
  }
  return result;
}
std::int64_t Phase(std::string_view value) {
  std::int64_t result = 0;
  const auto parsed = std::from_chars(value.data(),value.data()+value.size(),result);
  Check(!value.empty() && parsed.ec == std::errc{} && parsed.ptr == value.data()+value.size() &&
        result >= -1 && std::to_string(result) == value,
        "native word cost phase is invalid");
  return result;
}
double Number(std::string_view value) {
  Check(!value.empty() && value.find_first_of(" \t\r\n") == std::string_view::npos,
        "native word cost number is invalid");
  std::size_t consumed = 0;
  double result;
  try { result = std::stod(std::string(value),&consumed); }
  catch (const std::exception&) {
    throw StageError(ErrorCode::kInvalidArtifact,"native word cost number is invalid");
  }
  Check(consumed == value.size() && std::isfinite(result),
        "native word cost number must be finite");
  return result;
}
NativeWordSourceKind Kind(std::string_view kind) {
  if (kind == "dut-clock") return NativeWordSourceKind::kDutClock;
  if (kind == "rx") return NativeWordSourceKind::kRx;
  if (kind == "infrastructure") return NativeWordSourceKind::kInfrastructure;
  if (kind == "ports") return NativeWordSourceKind::kPort;
  if (kind == "unclassified") return NativeWordSourceKind::kUnclassified;
  if (kind == "declared-input") return NativeWordSourceKind::kDeclaredInput;
  throw StageError(ErrorCode::kInvalidArtifact,"native word cost source kind is unknown");
}
NativeWordCost Record(const std::vector<std::string_view>& fields) {
  NativeWordCost result{};
  result.phase_word = Phase(fields[0]);
  result.query = Identity(fields[1]);
  result.source_kind = Kind(fields[2]);
  result.source = Identity(fields[3]);
  if (result.source_kind == NativeWordSourceKind::kRx) {
    Check(result.source.size() >= 2 && result.source[0] == 's' &&
          result.source.find_first_not_of("0123456789",1) == std::string::npos,
          "native word RX source role is invalid");
  }
  if (result.source_kind == NativeWordSourceKind::kDutClock) {
    Check(fields[4] == "rise" || fields[4] == "fall",
          "native word DUT launch edge is missing or invalid");
    result.launch_edge = fields[4] == "rise" ? NativeWordLaunchEdge::kRise : NativeWordLaunchEdge::kFall;
  } else {
    Check(fields[4].empty(), "native word non-DUT source has a launch-edge override");
    result.launch_edge = NativeWordLaunchEdge::kNone;
  }
  const bool context = result.source_kind == NativeWordSourceKind::kPort ||
      result.source_kind == NativeWordSourceKind::kUnclassified;
  Check(context == (fields[5] == "native-source-context-required"),
        "native word source context classification differs");
  if (fields[5] == "native-clock-aware-reference") {
    result.measurement = NativeWordMeasurement::kReferenceCost;
    NativeWordCostEvidence evidence{};
    evidence.requirement_ns = Number(fields[6]);
    evidence.data_ns = Number(fields[7]);
    evidence.sampling_cost_ns = Number(fields[8]);
    evidence.clock_adjustment_ns = Number(fields[9]);
    evidence.start_pin = Identity(fields[10]);
    evidence.end_pin = Identity(fields[11]);
    evidence.start_clock = Identity(fields[12]);
    evidence.end_clock = Identity(fields[13]);
    evidence.launch_edge_ns = Number(fields[14]);
    evidence.capture_edge_ns = Number(fields[15]);
    Check(std::abs(evidence.requirement_ns-1.) <= 1e-5,
          "native word cost lacks its common 1 ns reference");
    Check(evidence.data_ns >= 0 &&
          std::isfinite(evidence.sampling_cost_ns-evidence.data_ns) &&
          std::abs((evidence.sampling_cost_ns-evidence.data_ns)-evidence.clock_adjustment_ns) <= 1e-8,
          "native word sampling cost and signed adjustment disagree");
    result.evidence = std::move(evidence);
  } else {
    Check(fields[5] == "native-no-timed-path" || fields[5] == "native-source-context-required",
          "native word measurement classification is unknown");
    result.measurement = fields[5] == "native-no-timed-path" ?
        NativeWordMeasurement::kNoTimedPath : NativeWordMeasurement::kContextRequired;
    for (std::size_t i=6; i<16; ++i)
      Check(fields[i].empty(), "native word unmeasured row carries numerical or witness evidence");
  }
  return result;
}
}  // namespace

NativeWordCostCounts VisitNativeWordCosts(std::istream& input,
    const std::function<void(const NativeWordCost&)>& visitor) {
  std::array<char,kMaximumRow+2> buffer{};
  std::string_view line;
  Check(Line(input,buffer,line) && line == kHeader, "native word cost header differs");
  NativeWordCostCounts counts;
  while (Line(input,buffer,line)) {
    Check(counts.rows < std::numeric_limits<std::uint64_t>::max(),
          "native word cost row count exceeds its domain");
    const auto record = Record(Fields(line));
    ++counts.rows;
    if (record.measurement == NativeWordMeasurement::kReferenceCost) ++counts.measured;
    else if (record.measurement == NativeWordMeasurement::kNoTimedPath) ++counts.no_timed_path;
    else ++counts.context_required;
    visitor(record);
  }
  return counts;
}
}  // namespace emuflow::core
