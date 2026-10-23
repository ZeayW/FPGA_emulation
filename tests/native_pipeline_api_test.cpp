#include "emuflow_core/native_pipeline.hpp"
#include "emuflow_core/artifact_io.hpp"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <stdexcept>

int main() {
  namespace fs = std::filesystem;
  using emuflow::core::ArtifactRef;
  using emuflow::core::ErrorCode;
  using emuflow::native::Stage;
  using emuflow::native::StageRequest;
  const auto unique = std::chrono::steady_clock::now().time_since_epoch().count();
  const auto root = fs::temp_directory_path() / ("emuflow-pipeline-api-" + std::to_string(unique));
  if (!fs::create_directory(root)) throw std::runtime_error("test directory already exists");
  try {
    if (emuflow::native::ExecuteStage(StageRequest{}).error != ErrorCode::kInvalidRequest)
      throw std::runtime_error("native API executed an unspecified stage");
    const auto source = (root / "rudy.input").string();
    const auto output = (root / "rudy.output").string();
    const std::string payload = "EMUFLOW_CHIMEW_RUDY_INPUT_V1\nGRID 0 0 1 1 1 1\nPARAM 1 1\nCAP 0 1\nNET 0 2 0.1 0.1 0.2 0.2\n";
    { std::ofstream stream(source); stream << payload; }
    StageRequest request{Stage::kRudy, {}, output};
    request.input_artifacts = {ArtifactRef::Bind(source, "chimew-rudy-input-v1")};
    const auto result = emuflow::native::ExecuteStage(request);
    if (!result.ok() || result.outputs.size() != 1 || result.inputs.size() != 1 ||
        result.outputs[0].schema() != "chimew-rudy-output-text") {
      throw std::runtime_error("typed native stage failed: " + result.message);
    }
    auto wrong = request;
    wrong.input_artifacts = {ArtifactRef::Bind(source, "tlr-input-text")};
    const auto mismatch = emuflow::native::ExecuteStage(wrong);
    if (mismatch.error != ErrorCode::kInvalidArtifact)
      throw std::runtime_error("native API guessed a mismatched artifact schema");
    result.outputs[0].RequireUnchanged();
    auto ambiguous = request;
    ambiguous.input_paths = {source};
    if (emuflow::native::ExecuteStage(ambiguous).error != ErrorCode::kInvalidRequest)
      throw std::runtime_error("native API accepted ambiguous ownership");
    StageRequest unsupported_pipe{Stage::kSchedule, {}, output};
    unsupported_pipe.input_artifacts = {ArtifactRef::Bind("-", "exact-schedule-input-v1", true)};
    if (emuflow::native::ExecuteStage(unsupported_pipe).error != ErrorCode::kInvalidRequest)
      throw std::runtime_error("native API accepted a pipe for a file-only reader");
    const auto original_time = fs::last_write_time(source);
    {
      emuflow::core::AtomicBinaryWriter writer(source);
      writer.WriteBytes(payload.data(), payload.size()); writer.Commit();
    }
    fs::last_write_time(source, original_time);
    const auto stale = emuflow::native::ExecuteStage(request);
    if (stale.error != ErrorCode::kMixedGeneration)
      throw std::runtime_error("native API rebound an old reference to a new generation");
    result.outputs[0].RequireUnchanged();
    auto unknown = request;
    unknown.stage = static_cast<Stage>(999);
    if (emuflow::native::ExecuteStage(unknown).error != ErrorCode::kInvalidRequest)
      throw std::runtime_error("unknown native stage did not fail closed");
  } catch (...) { fs::remove_all(root); throw; }
  fs::remove_all(root);
}
