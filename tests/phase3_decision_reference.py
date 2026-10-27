"""Small independent candidate-ranking oracle; no stage execution or publishing."""
import math
from typing import Any, Dict
from emuflow.errors import ValidationError

PATRON_PATH_OBJECTIVE_SELECTION_PROVIDER = "patron-path-objective-selection-v2"

def _select_patron_timing_assignment(
    initial: Dict[str, Any],
    candidate: Dict[str, Any],
    patron_trace: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Select a legal PATRON candidate using its Phase-3 path objective.

    Exact cut dependencies, schedule readiness, and physical timing are not
    knowable in Phase 3.  They are materialized and checked by the common
    Phase 4--7 timing contract after the assignment has been selected.
    """

    initial_metrics = patron_trace.get("initial_metrics", {})
    candidate_metrics = patron_trace.get("final_metrics", {})
    initial_objective = initial_metrics.get("objective_key")
    candidate_objective = candidate_metrics.get("objective_key")
    initial_path_rank = (
        initial_metrics.get("maximum_path_hops"),
        initial_metrics.get("maximum_path_tdm_ratio_sum"),
    )
    candidate_path_rank = (
        candidate_metrics.get("maximum_path_hops"),
        candidate_metrics.get("maximum_path_tdm_ratio_sum"),
    )
    if (
        not isinstance(initial_objective, list)
        or not isinstance(candidate_objective, list)
        or len(initial_objective) != 8
        or len(candidate_objective) != 8
        or not all(
            isinstance(value, int) and not isinstance(value, bool)
            and value >= 0
            for value in initial_path_rank + candidate_path_rank
        )
        or not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            for value in initial_objective + candidate_objective
        )
    ):
        raise ValidationError("PATRON v14 timing objective certificate is invalid")
    initial_rank = initial_path_rank + tuple(
        round(float(value) / 1.0e-9) if index < 2 else round(float(value))
        for index, value in enumerate(initial_objective)
    )
    candidate_rank = candidate_path_rank + tuple(
        round(float(value) / 1.0e-9) if index < 2 else round(float(value))
        for index, value in enumerate(candidate_objective)
    )
    assignment_changed = candidate.get("cluster_assignment") != initial.get(
        "cluster_assignment"
    )
    timing_improved = candidate_rank < initial_rank
    # Both assignments have already passed the scheduler-independent Phase 3
    # legality gate.  PATRON first ranks the maximum physical-hop count over
    # the supplied critical paths, then the maximum per-path sum of crossing
    # net TDM ratios.  Generic timing, load, transition, and cut metrics are
    # lower-priority tie breakers; no downstream schedule certificate
    # participates in this choice.
    accepted = assignment_changed and timing_improved
    selection = {
        "status": "pass",
        "provider": PATRON_PATH_OBJECTIVE_SELECTION_PROVIDER,
        "policy": "path-hop-then-maximum-path-ratio-sum-v1",
        "objective_fields": [
            "maximum_path_hops",
            "maximum_path_tdm_ratio_sum",
            "negative_worst_normalized_slack",
            "negative_total_negative_normalized_slack",
            "negative_paths",
            "maximum_predicted_tdm_ratio",
            "maximum_capacity_domain_load",
            "total_path_partition_transitions",
            "total_bit_hops",
            "cut_bits",
        ],
        "initial_timing_rank": list(initial_rank),
        "candidate_timing_rank": list(candidate_rank),
        "assignment_changed": assignment_changed,
        "timing_improved": timing_improved,
        "selected": "candidate" if accepted else "initial",
        "candidate_provider": candidate.get("provider"),
        "initial_provider": initial.get("provider"),
    }
    return (candidate if accepted else initial), selection
