#include "physical/flow/context.hpp"
#include <atomic>
#include <optional>
#include <thread>
namespace emuflow::core {
StageResult RunPhysicalFlow(const ArtifactRef &request,
                            const std::string &output,
                            const volatile std::sig_atomic_t *cancelled) {
  physical_flow::Context context(request, cancelled);
  std::vector<ArtifactRef> published;
  return RunStage(
      "physical.run-flow",
      [&] {
        context.Admit(output);
        return context.sources;
      },
      [&](const auto &) {
        context.Prepare();
        context.Stop();
        const auto count = context.selected.size();
        std::vector<std::optional<physical_flow::WorkerResult>> results(count);
        std::vector<std::string> failures(count);
        std::atomic<std::size_t> next{0};
        const auto worker = [&] {
          while (true) {
            const auto index = next.fetch_add(1);
            if (index >= count)
              return;
            try {
              results[index] = context.Implement(
                  context.partitions[context.selected[index]]);
            } catch (const std::exception &error) {
              failures[index] = error.what();
            } catch (...) {
              failures[index] = "unknown physical worker failure";
            }
          }
        };
        std::vector<std::thread> threads;
        try {
          for (std::size_t i = 0;
               i < std::min<std::size_t>(context.workers, count); ++i)
            threads.emplace_back(worker);
        } catch (...) {
          for (auto &thread : threads)
            thread.join();
          throw;
        }
        for (auto &thread : threads)
          thread.join();
        context.Stop();
        std::string errors;
        for (std::size_t i = 0; i < count; ++i)
          if (!failures[i].empty()) {
            if (!errors.empty())
              errors += "; ";
            errors +=
                context.partitions[context.selected[i]].id + ": " + failures[i];
          }
        if (!errors.empty())
          throw StageError(ErrorCode::kExecutionFailed,
                           "multi-FPGA physical flow failed: " + errors);
        std::vector<physical_flow::R> records;
        for (auto &result : results) {
          physical_flow::Check(bool(result),
                               "physical worker did not return a record");
          for (const auto &ref : result->artifacts)
            ref.RequireUnchanged();
          context.sources.insert(context.sources.end(),
                                 result->artifacts.begin(),
                                 result->artifacts.end());
          records.push_back(std::move(result->record));
        }
        if (context.partial) {
          (void)context.Batch(records);
          published.push_back(ArtifactRef::Bind(
              (context.lease->root / "physical-partition-batch.json").string(),
              "physical-partition-batch-v1"));
        } else {
          (void)context.Assemble(records);
          published.push_back(ArtifactRef::Bind(
              (context.lease->root / "multi-fpga-physical-flow-report.json")
                  .string(),
              "physical-flow-report-v1"));
          published.push_back(ArtifactRef::Bind(
              (context.lease->root / "physical-summary.json").string(),
              "physical-summary-v1"));
        }
        for (const auto &source : context.sources)
          source.RequireUnchanged();
      },
      [&] { return published; });
}
} // namespace emuflow::core
