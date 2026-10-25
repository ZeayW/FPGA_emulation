"""Bounded pin-contract records for tests; no grouping/placement optimizer."""
from emuflow.pin_planning import (
    CHIMEW_PIN_PLAN_PROVIDER, PIN_PLAN_SCHEMA, _assignment_metrics, _baseline_assignment,
)


def make_pin_plan_fixture(schedule, platform, positions, *, assignments=None,
                          crossing_weight=1.0, position_weight=1.0, output_path=None):
    if len(schedule["entries"]) > 4096:
        raise AssertionError("pin contract fixture exceeds 4096 signals")
    assignments = _baseline_assignment(schedule) if assignments is None else assignments
    weights = {"crossing": crossing_weight, "position": position_weight}
    metrics = _assignment_metrics(schedule, platform, positions, assignments, crossing_weight, position_weight)
    baseline = _assignment_metrics(schedule, platform, positions, _baseline_assignment(schedule), crossing_weight, position_weight)
    plan = {
        "schema": PIN_PLAN_SCHEMA, "design": schedule["design"], "platform": platform.name,
        "provider": CHIMEW_PIN_PLAN_PROVIDER,
        "configuration": {"refinement_iterations": 0}, "weights": weights,
        "metrics": {"signals": len(assignments), "groups": len({v[0] for v in assignments.values()}), **metrics,
                    **{f"baseline_logical_lane_{k}": v for k, v in baseline.items()}},
        "entries": [{"schedule_entry": entry["id"], "group": assignments[entry["id"]][0],
                     "physical_lane": assignments[entry["id"]][1], "logical_lane": entry["lane"]}
                    for entry in sorted(schedule["entries"], key=lambda e: e["id"])],
    }
    if output_path is not None:
        from emuflow.pin_plan_storage import write_pin_plan_storage
        return write_pin_plan_storage(output_path, plan)
    return plan
