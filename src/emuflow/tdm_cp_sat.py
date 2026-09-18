"""Optional OR-Tools CP-SAT oracle for medium Phase 5 schedules."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, Mapping

from .errors import EmuFlowError, ValidationError
from .platform import Platform


TDM_CP_SAT_ORACLE_PROVIDER = "time-expanded-cp-sat-oracle-v1"
_TIME_SCALE = 100  # 10 ps units keep every CP-SAT product within int64
_SCORE_SCALE = 1_000_000


def _scaled(value: Any, scale: int, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValidationError(f"{name} is not a finite number")
    return int(round(float(value) * scale))


def _load_cp_model() -> Any:
    try:
        from ortools.sat.python import cp_model
    except ImportError as error:
        raise EmuFlowError(
            "the medium-case CP-SAT oracle requires the optional "
            "'cp-sat' dependency (pip install 'emuflow[cp-sat]')"
        ) from error
    return cp_model


def _quantized_worst_score(
    ratio_plan: Mapping[str, Any],
    ready_by_hop: Mapping[int, int],
    slot_by_hop: Mapping[int, int],
) -> int:
    hops = {hop["index"]: hop for hop in ratio_plan["hops"]}
    normalization = ratio_plan["normalization"]
    positive_scale = _scaled(
        normalization["positive_slack_scale_ns"], _TIME_SCALE,
        "positive slack scale",
    )
    negative_scale = _scaled(
        normalization["negative_slack_scale_ns"], _TIME_SCALE,
        "negative slack scale",
    )
    maximum_period = _scaled(
        normalization["max_clock_period_ns"], _TIME_SCALE,
        "maximum clock period",
    )
    scores = []
    for path in ratio_plan["timing_paths"]:
        period = _scaled(path["clock_period_ns"], _TIME_SCALE, "period")
        slack = period - _scaled(
            path["fixed_delay_ns"], _TIME_SCALE, "fixed delay"
        )
        for index in path["hops"]:
            hop = hops[index]
            slack -= _scaled(
                hop["base_delay_ns"], _TIME_SCALE, "base delay"
            )
            slack -= _scaled(hop["beta_ns"], _TIME_SCALE, "beta") * (
                slot_by_hop[index] - ready_by_hop[index]
            )
        if slack >= 0:
            score = (
                slack * period * _SCORE_SCALE
                // (positive_scale * maximum_period)
            )
        else:
            score = (
                slack * _TIME_SCALE * _SCORE_SCALE
                // (negative_scale * period)
            )
        scores.append(score)
    return min(scores)


def solve_cp_sat_slot_schedule(
    routes: Mapping[str, Any],
    platform: Platform,
    ratio_plan: Mapping[str, Any],
    *,
    max_hops: int = 256,
    time_limit_seconds: float = 60.0,
) -> Dict[str, Any]:
    """Prove a medium fixed-ratio/lane schedule optimal with CP-SAT.

    The time-expanded integer model covers collision, multicast precedence,
    per-hop ratio windows, link latency, multiple transport rounds, and the
    global frame barrier. It first maximizes quantized worst normalized slack,
    then minimizes completion and total wait without changing that optimum.
    """

    if (
        isinstance(max_hops, bool)
        or not isinstance(max_hops, int)
        or max_hops <= 0
        or isinstance(time_limit_seconds, bool)
        or not isinstance(time_limit_seconds, (int, float))
        or not math.isfinite(float(time_limit_seconds))
        or float(time_limit_seconds) <= 0.0
    ):
        raise ValidationError("CP-SAT oracle limits are invalid")
    from .tdm import (
        COMBINATIONAL_SETTLE_SLOTS,
        RELAY_ENDPOINT_SETTLE_SLOTS,
        RUNTIME_BARRIER_SLOTS,
    )
    from .tdm_oracle import (
        _reconstruct_slot_oracle_result,
        _slot_oracle_model,
    )

    oracle = _slot_oracle_model(routes, platform, ratio_plan)
    hop_by_index = oracle["hop_by_index"]
    if len(hop_by_index) > max_hops:
        raise ValidationError(
            f"CP-SAT oracle supports at most {max_hops} routed hops"
        )
    cp_model = _load_cp_model()
    model = cp_model.CpModel()
    frame_slots = oracle["frame_slots"]
    last_transport_slot = frame_slots - RUNTIME_BARRIER_SLOTS - 1
    slots = {
        index: model.new_int_var(0, last_transport_slot, f"slot_{index}")
        for index in hop_by_index
    }
    ready = {
        index: model.new_int_var(0, last_transport_slot, f"ready_{index}")
        for index in hop_by_index
    }
    route_completion = {}
    for route, sink_hops in sorted(oracle["sink_hops_by_route"].items()):
        variable = model.new_int_var(0, frame_slots, f"complete_{route}")
        model.add_max_equality(
            variable,
            [
                slots[index]
                + oracle["link_by_id"][hop_by_index[index]["link"]]
                .latency_cycles
                for index in sink_hops
            ],
        )
        route_completion[route] = variable

    round_ready = {}
    for transport_round in oracle["active_rounds"]:
        variable = model.new_int_var(
            0, last_transport_slot, f"round_ready_{transport_round}"
        )
        prior_routes = [
            route_completion[route] + COMBINATIONAL_SETTLE_SLOTS
            for route, route_round in oracle["round_by_route"].items()
            if route_round < transport_round
        ]
        if prior_routes:
            model.add_max_equality(variable, prior_routes)
        else:
            model.add(variable == 0)
        round_ready[transport_round] = variable

    occupancy = defaultdict(list)
    for index, hop in sorted(hop_by_index.items()):
        parent = oracle["parent"][index]
        if parent is None:
            model.add(ready[index] == round_ready[oracle["round_by_hop"][index]])
        else:
            parent_hop = hop_by_index[parent]
            latency = oracle["link_by_id"][parent_hop["link"]].latency_cycles
            model.add(
                ready[index]
                == slots[parent] + latency + RELAY_ENDPOINT_SETTLE_SLOTS
            )
        latency = oracle["link_by_id"][hop["link"]].latency_cycles
        model.add(slots[index] >= ready[index])
        model.add(slots[index] <= ready[index] + hop["discrete_ratio"] - 1)
        model.add(slots[index] <= last_transport_slot - latency)
        occupancy[(hop["domain"], hop["lane"])].append(slots[index])
    for variables in occupancy.values():
        if len(variables) > 1:
            model.add_all_different(variables)

    normalization = ratio_plan["normalization"]
    positive_scale = _scaled(
        normalization["positive_slack_scale_ns"], _TIME_SCALE,
        "positive slack scale",
    )
    negative_scale = _scaled(
        normalization["negative_slack_scale_ns"], _TIME_SCALE,
        "negative slack scale",
    )
    maximum_period = _scaled(
        normalization["max_clock_period_ns"], _TIME_SCALE,
        "maximum clock period",
    )
    worst_score = model.new_int_var(
        -100 * _SCORE_SCALE,
        100 * _SCORE_SCALE,
        "worst_normalized_slack",
    )
    for path in ratio_plan["timing_paths"]:
        period = _scaled(
            path["clock_period_ns"], _TIME_SCALE, "clock period"
        )
        fixed = _scaled(path["fixed_delay_ns"], _TIME_SCALE, "fixed delay")
        constant = period - fixed
        terms = []
        for index in path["hops"]:
            hop = hop_by_index[index]
            constant -= _scaled(
                hop["base_delay_ns"], _TIME_SCALE, "hop base delay"
            )
            beta = _scaled(hop["beta_ns"], _TIME_SCALE, "hop beta")
            terms.append(beta * (ready[index] - slots[index]))
        beta_sum = sum(
            _scaled(hop_by_index[index]["beta_ns"], _TIME_SCALE, "hop beta")
            for index in path["hops"]
        )
        slack = model.new_int_var(
            constant - beta_sum * frame_slots,
            constant,
            f"slack_{path['index']}",
        )
        model.add(slack == constant + sum(terms))
        nonnegative = model.new_bool_var(f"slack_nonnegative_{path['index']}")
        model.add(slack >= 0).only_enforce_if(nonnegative)
        model.add(slack <= -1).only_enforce_if(nonnegative.negated())
        model.add(
            worst_score * positive_scale * maximum_period
            <= slack * period * _SCORE_SCALE
        ).only_enforce_if(nonnegative)
        model.add(
            worst_score * negative_scale * period
            <= slack * _TIME_SCALE * _SCORE_SCALE
        ).only_enforce_if(nonnegative.negated())

    completion = model.new_int_var(0, frame_slots, "completion")
    model.add_max_equality(completion, list(route_completion.values()))
    total_wait = model.new_int_var(
        0, len(hop_by_index) * frame_slots, "total_wait"
    )
    model.add(total_wait == sum(slots[i] - ready[i] for i in slots))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_seconds)
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0

    def solve_optimal(objective: Any, maximize: bool) -> int:
        if maximize:
            model.maximize(objective)
        else:
            model.minimize(objective)
        status = solver.solve(model)
        if status != cp_model.OPTIMAL:
            label = solver.status_name(status)
            raise EmuFlowError(
                f"CP-SAT oracle did not prove an optimum: {label}"
            )
        return int(solver.value(objective))

    score = solve_optimal(worst_score, True)
    model.add(worst_score == score)
    best_completion = solve_optimal(completion, False)
    model.add(completion == best_completion)
    best_wait = solve_optimal(total_wait, False)

    slot_by_hop = {index: int(solver.value(var)) for index, var in slots.items()}
    reconstructed = _reconstruct_slot_oracle_result(
        oracle, ratio_plan, slot_by_hop
    )
    quantized = _quantized_worst_score(
        ratio_plan,
        reconstructed["ready_by_hop"],
        reconstructed["slot_by_hop"],
    )
    if quantized != score:
        raise ValidationError(
            "CP-SAT quantized objective does not match independent timing"
        )
    result = {
        "provider": TDM_CP_SAT_ORACLE_PROVIDER,
        "status": "optimal",
        "quantization": {
            "time_units_per_ns": _TIME_SCALE,
            "score_units": _SCORE_SCALE,
        },
        "optimal_quantized_worst_normalized_slack": score,
        "optimal_completion_slot": best_completion,
        "optimal_total_wait_slots": best_wait,
        **reconstructed,
        "solver": {
            "status": "OPTIMAL",
            "num_conflicts": int(solver.num_conflicts),
            "num_branches": int(solver.num_branches),
            "wall_time_seconds": float(solver.wall_time),
        },
    }
    validate_cp_sat_slot_schedule(routes, platform, ratio_plan, result)
    return result


def validate_cp_sat_slot_schedule(
    routes: Mapping[str, Any],
    platform: Platform,
    ratio_plan: Mapping[str, Any],
    result: Mapping[str, Any],
) -> Dict[str, Any]:
    """Independently reconstruct a CP-SAT schedule and its public metrics."""

    from .tdm_oracle import (
        _reconstruct_slot_oracle_result,
        _slot_oracle_model,
    )

    if (
        result.get("provider") != TDM_CP_SAT_ORACLE_PROVIDER
        or result.get("status") != "optimal"
        or result.get("quantization")
        != {
            "time_units_per_ns": _TIME_SCALE,
            "score_units": _SCORE_SCALE,
        }
    ):
        raise ValidationError("CP-SAT oracle certificate is invalid")
    model = _slot_oracle_model(routes, platform, ratio_plan)
    reconstructed = _reconstruct_slot_oracle_result(
        model, ratio_plan, result.get("slot_by_hop", {})
    )
    for key, value in reconstructed.items():
        if result.get(key) != value:
            raise ValidationError(
                f"CP-SAT oracle {key} does not match reconstruction"
            )
    expected_score = _quantized_worst_score(
        ratio_plan,
        reconstructed["ready_by_hop"],
        reconstructed["slot_by_hop"],
    )
    if (
        result.get("optimal_quantized_worst_normalized_slack")
        != expected_score
        or result.get("optimal_completion_slot")
        != reconstructed["completion_slot"]
        or result.get("optimal_total_wait_slots")
        != reconstructed["total_wait_slots"]
    ):
        raise ValidationError("CP-SAT oracle optimum metrics are inconsistent")
    solver = result.get("solver")
    if not isinstance(solver, Mapping) or solver.get("status") != "OPTIMAL":
        raise ValidationError("CP-SAT oracle has no optimal solver certificate")
    return {
        "status": "pass",
        "hops": len(model["hop_by_index"]),
        "transport_rounds": len(model["active_rounds"]),
    }
