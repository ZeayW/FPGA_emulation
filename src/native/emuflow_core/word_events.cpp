#include "emuflow_core/word_events.hpp"
#include "emuflow_core/stage_runtime.hpp"
#include <algorithm>
#include <cmath>
#include <deque>
#include <limits>
#include <numeric>

namespace emuflow::core {
namespace {
void Check(bool valid, const char* message) {
  if (!valid) throw StageError(ErrorCode::kInvalidArtifact, message);
}
}

std::uint32_t FirstPeriodicWord(std::uint32_t phase, std::uint32_t words,
                               std::uint32_t first, std::uint32_t last) {
  Check(words && phase < words && first <= last, "invalid periodic word calendar");
  const std::uint64_t rounds = first <= phase ? 0 :
      (std::uint64_t(first) - phase + words - 1) / words;
  const auto index = phase + rounds * words;
  Check(index <= last, "periodic word event exceeds native time domain");
  return static_cast<std::uint32_t>(index);
}

double WordEventTime(const PhysicalWordModel& model, std::uint32_t index) {
  // Match separately rounded multiply/add in exported event timestamps.
  volatile double elapsed = model.period_ns * index;
  const double time = model.epoch_ns + elapsed;
  Check(std::isfinite(time), "nonfinite physical word event time");
  return time;
}

std::uint32_t LastPhysicalWordCapture(const PhysicalWordModel& model) {
  if(model.calendar==PhysicalWordCalendar::kContinuousPhysical) {
    Check(model.continuous_horizon_words>=3,"continuous word projection requires an explicit search horizon");
    return model.continuous_horizon_words-1;
  }
  Check(model.calendar==PhysicalWordCalendar::kDrainedVirtualEpoch,"unsupported physical word calendar");
  Check(model.frame_slots>=3,"physical word projection requires its frame drain boundary");
  return model.frame_slots-2;
}

std::uint64_t ContinuousWordRepeatWords(const PhysicalWordModel& model) {
  std::uint64_t repeat=1;
  for(const auto& transfer:model.transfers) {
    Check(transfer.ratio>=8 && transfer.ratio<=256 && transfer.ratio%8==0 &&
          transfer.phase_word<transfer.ratio/8,"invalid physical eight-bit word transfer");
    const auto words=transfer.ratio/8;
    const auto quotient=repeat/std::gcd(repeat,std::uint64_t(words));
    Check(quotient<=UINT64_MAX/words,"continuous word recurrence exceeds native domain");
    repeat=quotient*words;
  }
  return repeat;
}

std::vector<std::uint32_t> ProjectPhysicalWordEvents(const PhysicalWordModel& model) {
  Check(std::isfinite(model.period_ns) && model.period_ns > 0 &&
        std::isfinite(model.epoch_ns), "invalid physical word clock");
  const auto last_allowed_capture=LastPhysicalWordCapture(model);
  const auto count = model.transfers.size();
  Check(count <= UINT32_MAX && model.root_launch_ns.size() <= UINT32_MAX,
        "physical word population exceeds native domain");
  for (const auto phase : model.root_launch_ns)
    Check(std::isfinite(phase), "missing or nonfinite architectural launch phase");
  std::vector<std::vector<std::uint32_t>> successors(count);
  std::vector<std::size_t> pending(count, 0);
  std::deque<std::uint32_t> ready;
  for (std::size_t i = 0; i < count; ++i) {
    const auto& transfer = model.transfers[i];
    Check(transfer.ratio >= 8 && transfer.ratio <= 256 && transfer.ratio % 8 == 0 &&
          transfer.phase_word < transfer.ratio / 8 && transfer.fixed_latency_words > 0,
          "invalid physical eight-bit word transfer");
    Check(transfer.configuration_stable == transfer.fanins.empty(),
          "physical word transfer lacks explicit complete source semantics");
    for (const auto& fanin : transfer.fanins) {
      Check(std::isfinite(fanin.data_delay_ns) && fanin.data_delay_ns >= 0 &&
            std::isfinite(fanin.sampling_cost_ns) &&
            std::isfinite(fanin.sampling_cost_ns - fanin.data_delay_ns),
            "invalid physical word fanin cost");
      if (fanin.from_event) {
        Check(fanin.source < count && fanin.source != i,
              "physical word fanin has an unknown or self event");
        successors[fanin.source].push_back(static_cast<std::uint32_t>(i));
        ++pending[i];
      } else {
        Check(fanin.source < model.root_launch_ns.size(),
              "physical word fanin lacks its architectural launch phase");
      }
    }
    if (!pending[i]) ready.push_back(static_cast<std::uint32_t>(i));
  }
  // Integer coordinates retain strict pre-edge causality even when two
  // nominal timing costs are zero or have favorable signed clock adjustments.
  std::vector<std::uint32_t> chosen(count);
  std::size_t observed = 0;
  while (!ready.empty()) {
    const auto i = ready.front(); ready.pop_front();
    const auto& transfer = model.transfers[i];
    double guard = transfer.configuration_stable ? model.epoch_ns :
        -std::numeric_limits<double>::infinity();
    std::uint32_t last_capture = 0;
    for (const auto& fanin : transfer.fanins) {
      double source;
      if (fanin.from_event) {
        const auto capture = std::uint64_t(chosen[fanin.source]) +
            model.transfers[fanin.source].fixed_latency_words;
        Check(capture <= UINT32_MAX, "RX word capture exceeds native time domain");
        last_capture = std::max(last_capture, static_cast<std::uint32_t>(capture));
        source = WordEventTime(model, static_cast<std::uint32_t>(capture));
      } else source = model.root_launch_ns[fanin.source];
      const auto candidate = source + fanin.sampling_cost_ns;
      Check(std::isfinite(candidate), "nonfinite physical word readiness guard");
      guard = std::max(guard, candidate);
    }
    const auto words = transfer.ratio / 8;
    Check(transfer.fixed_latency_words<last_allowed_capture,
          model.calendar==PhysicalWordCalendar::kContinuousPhysical ?
          "physical word transfer cannot arrive within search horizon" :
          "physical word transfer cannot arrive before frame drain");
    const auto last = last_allowed_capture - transfer.fixed_latency_words;
    Check(last_capture < last, "no word load after contributing RX capture");
    const auto first = FirstPeriodicWord(transfer.phase_word, words, last_capture + 1, last);
    const auto maximum_round = (last - transfer.phase_word) / words;
    std::uint64_t low = (first - transfer.phase_word) / words, high = maximum_round;
    const auto time_at = [&](std::uint64_t round) {
      return WordEventTime(model, static_cast<std::uint32_t>(transfer.phase_word + round * words));
    };
    Check(time_at(high) >= guard,model.calendar==PhysicalWordCalendar::kContinuousPhysical ?
          "physical readiness exceeds continuous search horizon" :
          "physical readiness misses frame drain or native calendar");
    // A bounded integer bisection avoids epsilon snapping at exact word edges.
    // This is monotonicity of one fixed clock calendar, not of DUT Fmax.
    while (low < high) {
      const auto middle = low + (high - low) / 2;
      if (time_at(middle) >= guard) high = middle;
      else low = middle + 1;
    }
    const auto index = static_cast<std::uint32_t>(transfer.phase_word + low * words);
    const auto load = time_at(low);
    Check((!low || time_at(low - 1) < load) &&
          (low == maximum_round || time_at(low + 1) > load),
          "physical word clock loses edge resolution");
    Check(index >= first && load >= guard, "physical word selection violates readiness");
    chosen[i] = index;
    ++observed;
    for (auto child : successors[i]) {
      Check(pending[child] > 0, "physical word dependency accounting disagrees");
      if (!--pending[child]) ready.push_back(child);
    }
  }
  Check(observed == count, "physical word fanin graph contains a cycle");
  return chosen;
}

void ValidatePhysicalWordEvents(const PhysicalWordModel& model,
    const std::vector<std::uint32_t>& selected,
    const std::vector<double>& support_slack_ns, double slack_tolerance_ns) {
  Check(std::isfinite(model.period_ns) && model.period_ns > 0 &&
        std::isfinite(model.epoch_ns) &&
        std::isfinite(slack_tolerance_ns) && slack_tolerance_ns >= 0,
        "invalid independent word event check context");
  const auto last_allowed_capture=LastPhysicalWordCapture(model);
  Check(selected.size() == model.transfers.size(),
        "independent word event population differs");
  for (auto phase : model.root_launch_ns)
    Check(std::isfinite(phase), "independent word check lacks a launch phase");
  std::size_t offset = 0;
  for (std::size_t i = 0; i < selected.size(); ++i) {
    const auto& transfer = model.transfers[i];
    Check(transfer.ratio >= 8 && transfer.ratio <= 256 && transfer.ratio % 8 == 0 &&
          transfer.phase_word < transfer.ratio / 8 && transfer.fixed_latency_words > 0 &&
          transfer.configuration_stable == transfer.fanins.empty(),
          "independent word transfer semantics are invalid");
    const auto index = selected[i], words = transfer.ratio / 8;
    Check(index > 0 && index % words == transfer.phase_word &&
          std::uint64_t(index) + transfer.fixed_latency_words <= last_allowed_capture,
          model.calendar==PhysicalWordCalendar::kContinuousPhysical ?
          "independent word event violates its phase or continuous search horizon" :
          "independent word event violates its phase or frame drain");
    Check(transfer.fanins.size() <= support_slack_ns.size() - offset,
          "independent word support check coverage is incomplete");
    const bool have_previous = index > words;
    const auto previous = have_previous ? index - words : 0;
    bool previous_excluded = !have_previous;
    const auto load = WordEventTime(model, index);
    const auto delta = have_previous ? WordEventTime(model, previous) - load : 0.0;
    Check(!have_previous || delta < 0, "independent word clock loses edge resolution");
    for (const auto& fanin : transfer.fanins) {
      Check(std::isfinite(fanin.data_delay_ns) && fanin.data_delay_ns >= 0 &&
            std::isfinite(fanin.sampling_cost_ns) &&
            std::isfinite(fanin.sampling_cost_ns - fanin.data_delay_ns),
            "independent word check has an invalid physical cost");
      if (fanin.from_event) {
        Check(fanin.source < selected.size() && fanin.source != i,
              "independent word check has an invalid source event");
        const auto capture = std::uint64_t(selected[fanin.source]) +
            model.transfers[fanin.source].fixed_latency_words;
        Check(index > capture, "independent word event reuses its RX capture edge");
        if (have_previous && previous <= capture) previous_excluded = true;
      } else {
        Check(fanin.source < model.root_launch_ns.size(),
              "independent word check lacks an architectural source");
      }
      const auto slack = support_slack_ns[offset++];
      Check(std::isfinite(slack) && slack >= -slack_tolerance_ns,
            "independent word event fails an OpenSTA support check");
      // Changing only the capture deadline by delta changes an engine check's
      // slack by exactly delta. No producer readiness or data-delay sum enters
      // this rejection of the immediately preceding calendar occurrence.
      if (have_previous && slack + delta < -slack_tolerance_ns) previous_excluded = true;
    }
    Check(previous_excluded, "an earlier word occurrence is not independently excluded");
  }
  Check(offset == support_slack_ns.size(), "independent word support checks contain extra records");
}
}  // namespace emuflow::core
