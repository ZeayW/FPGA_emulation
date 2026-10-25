"""OpenPARF native placement driver with stable feasible convergence.

OpenPARF's generic stop condition may accept the first density-feasible
iterate whose HPWL happens not to improve over the immediately preceding
iterate.  Routability-area inflation deliberately perturbs the optimization
landscape, so a single-step test can terminate while HPWL is still oscillating
strongly.  This driver keeps OpenPARF's native global placement, legalization,
and detailed placement intact, but requires a bounded stable window after the
last area adjustment and restores the best density-feasible global-placement
iterate before legalization.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
from pathlib import Path
from typing import Any, MutableMapping

from .openparf_continuous_driver import apply_high_fanout_net_weights


OPENPARF_NATIVE_CONVERGENCE_SCHEMA = (
    "emuflow.openparf-native-convergence/v1"
)


def _number(value: Any) -> float:
    if hasattr(value, "item"):
        value = value.item()
    return float(value)


def _iteration(metric: Any) -> int:
    return int(metric.opt_iter.iteration)


def _weighted_hpwl(engine: Any, metric: Any) -> float:
    weights = engine.params.wirelength_weights
    return sum(
        _number(value) * float(weights[index])
        for index, value in enumerate(metric.hpwl)
    )


def _has_active_area_type(engine: Any) -> bool:
    mask = engine.data_cls.optimization_area_type_mask
    value = mask.any() if hasattr(mask, "any") else any(mask)
    return bool(value.item() if hasattr(value, "item") else value)


def _logic_overflow(engine: Any, metric: Any) -> dict[str, float]:
    names = tuple(getattr(engine.params, "logic_area_type_names", ()))
    if not names:
        raise RuntimeError("OpenPARF has no logic area types for convergence")
    result = {}
    for name in names:
        index = int(engine.placedb.getAreaTypeIndexFromName(name))
        if index < 0 or index >= len(metric.overflow):
            raise RuntimeError(f"OpenPARF logic area type {name!r} is invalid")
        result[str(name)] = _number(metric.overflow[index])
    return result


def validate_native_logic_site_area(engine: Any) -> dict[str, Any]:
    """Check the native density domain using already constructed tensors."""

    expected = getattr(engine.params, "emuflow_expected_logic_site_area", None)
    names = tuple(engine.params.logic_area_type_names)
    if not isinstance(expected, dict) or not names or set(expected) != set(names):
        raise RuntimeError("OpenPARF expected logic site area is missing or invalid")
    actual = {}
    for name in names:
        value = expected[name]
        if (
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0
        ):
            raise RuntimeError("OpenPARF expected logic site area is invalid")
        index = int(engine.placedb.getAreaTypeIndexFromName(name))
        if index < 0 or index >= len(engine.data_cls.initial_density_maps):
            raise RuntimeError(f"OpenPARF logic area type {name!r} is unavailable")
        density = engine.data_cls.initial_density_maps[index]
        if density is None:
            raise RuntimeError(f"OpenPARF logic area type {name!r} is unavailable")
        area = _number(engine.data_cls.total_area) - _number(density.sum())
        if not math.isfinite(area) or not math.isclose(
            area, float(value), rel_tol=1.0e-9, abs_tol=1.0e-6
        ):
            raise RuntimeError(
                f"OpenPARF {name} site area mismatch: native={area}, "
                f"qualified={value}; check Bookshelf gaps and site bounds"
            )
        actual[name] = area
    return {"status": "pass", "expected": dict(expected), "actual": actual}


def validate_native_filler_geometry(engine: Any) -> dict[str, Any]:
    """Require fine-grained logic filler sampling on the density grid."""

    names = tuple(getattr(engine.params, "logic_area_type_names", ()))
    if not names:
        raise RuntimeError("OpenPARF has no logic area types for filler validation")
    maximum_span = float(engine.params.gp_max_logic_filler_bin_span)
    if not math.isfinite(maximum_span) or not 0.0 < maximum_span <= 0.5:
        raise RuntimeError("OpenPARF logic filler bin span is invalid")
    resources = {}
    for name in names:
        index = int(engine.placedb.getAreaTypeIndexFromName(name))
        count = int(engine.data_cls.num_fillers[index])
        size = [float(value) for value in engine.data_cls.filler_sizes[index]]
        bin_size = [
            float(value) for value in engine.data_cls.bin_map_sizes[index]
        ]
        if count <= 0 or not all(
            math.isfinite(value) and value > 0
            for value in size + bin_size
        ):
            raise RuntimeError(f"OpenPARF {name} filler geometry is invalid")
        span = [size[axis] / bin_size[axis] for axis in range(2)]
        if any(value > maximum_span + 1.0e-6 for value in span):
            raise RuntimeError(
                f"OpenPARF {name} filler exceeds the density sampling span: "
                f"size={size}, bin={bin_size}"
            )
        data = engine.data_cls
        available = (
            float(data.total_area)
            - _number(data.initial_density_maps[index].sum())
            - _number(data.total_fixed_areas[index])
        )
        movable = _number(data.total_movable_areas[index])
        filler = float(data.total_filler_areas[index])
        target = float(engine.params.target_density)
        budget = dict(placeable_area=available, movable_area=movable,
                      filler_area=filler, target_density=target)
        _validate_filler_area_budget(budget, count * size[0] * size[1])
        resources[name] = {
            "count": count,
            "size": size,
            "bin_size": bin_size,
            "bin_span": span,
            "area_budget": budget,
        }
    return {
        "provider": "density-bin-bounded-fillers-v2",
        "status": "pass",
        "maximum_bin_span": [maximum_span, maximum_span],
        "resources": resources,
        "area_budget_provider": "target-density-filler-budget-v1",
    }


def _validate_filler_area_budget(budget: Any, sampled_area: float) -> None:
    if not isinstance(budget, dict) or any(
        isinstance(budget.get(key), bool)
        or not isinstance(budget.get(key), (int, float))
        or not math.isfinite(float(budget[key]))
        for key in ("placeable_area", "movable_area", "filler_area", "target_density")
    ):
        raise RuntimeError("OpenPARF filler area budget is invalid")
    available, movable, filler, target = (
        float(budget[key]) for key in
        ("placeable_area", "movable_area", "filler_area", "target_density")
    )
    expected = max(target * available - movable, 0.0)
    if (
        min(available, movable, filler) < 0 or not 0 < target <= 1
        or not math.isclose(filler, expected, rel_tol=1e-6, abs_tol=1e-6)
        or not math.isclose(sampled_area, filler, rel_tol=1e-6, abs_tol=1e-6)
    ):
        raise RuntimeError(
            "OpenPARF filler area budget differs from target density: "
            f"target={target}, available={available}, movable={movable}, "
            f"filler={filler}, sampled={sampled_area}, expected={expected}"
        )


def _active_overflow_is_feasible(engine: Any, metric: Any) -> bool:
    """Qualify routability-bearing logic density before exact legalization.

    LUT and FF areas are the resources inflated by the RUDY and pin-density
    feedback loop.  Sparse MUX and hard-block area types can retain a large
    analytical overflow even though the native direct legalizer assigns every
    instance to an exact legal site.  Treating those analytical values as the
    global-placement stop condition hid the state that matters to fabric
    routability and forced every mixed-resource design to the iteration limit.
    """

    io_area_types = set()
    if bool(getattr(engine.params, "io_legalization_flag", False)):
        io_area_types = {
            int(engine.placedb.getAreaTypeIndexFromName(name))
            for name in engine.params.io_at_names
        }
    logic_names = set(getattr(engine.params, "logic_area_type_names", ()))
    if not logic_names:
        raise RuntimeError("OpenPARF has no logic area types for convergence")
    for name in logic_names:
        area_type = int(engine.placedb.getAreaTypeIndexFromName(name))
        if area_type in io_area_types:
            continue
        if area_type < 0 or area_type >= len(metric.overflow):
            raise RuntimeError(f"OpenPARF logic area type {name!r} is invalid")
        if _number(metric.overflow[area_type]) > float(engine.params.stop_overflow):
            return False
    return True


def _area_adjustment_is_finished(engine: Any) -> bool:
    if not bool(getattr(engine.params, "gp_adjust_area", False)):
        return True
    adjustments = int(getattr(engine, "num_gp_adjust_area", 0))
    maximum = int(getattr(engine.params, "gp_max_adjust_area_iters", 0))
    return adjustments > 0 and (
        not bool(getattr(engine, "gp_adjust_area", True))
        or (maximum > 0 and adjustments >= maximum)
    )


def bounded_native_area_adjustment_condition(
    engine: Any,
    current_metric: Any,
    original_condition: Any,
) -> bool:
    """Reserve enough global-placement iterations after the last inflation.

    Every RUDY/pin-area adjustment resets OpenPARF's optimizer.  Starting a
    final adjustment too close to the hard iteration bound can therefore leave
    a strongly perturbed placement with no time to regain density feasibility
    and satisfy the stable-window contract.  Preserve all native adjustment
    decisions while declining a new reset that cannot receive its configured
    post-adjustment budget.
    """

    if not original_condition(engine, current_metric):
        return False
    reserve = int(
        getattr(
            engine.params,
            "emuflow_post_adjustment_iteration_reserve",
            0,
        )
    )
    if reserve < 0:
        raise RuntimeError(
            "OpenPARF post-adjustment iteration reserve must be nonnegative"
        )
    iteration = _iteration(current_metric)
    maximum = int(engine.params.max_global_place_iters)
    remaining = maximum - iteration
    if remaining >= reserve:
        return True
    engine.gp_adjust_area = False
    engine._emuflow_area_adjustment_cutoff = {
        "iteration": iteration,
        "remaining_iterations": remaining,
        "required_reserve": reserve,
        "completed_adjustments": int(
            getattr(engine, "num_gp_adjust_area", 0)
        ),
    }
    return False


def _restore_best_feasible(
    engine: Any, state: MutableMapping[str, Any]
) -> None:
    best = getattr(engine, "_emuflow_best_feasible_position", None)
    if best is None:
        return
    engine.data_cls.pos[0].data.copy_(best)
    state["restored_best_feasible"] = True


def _restore_best_diagnostic(
    engine: Any, state: MutableMapping[str, Any]
) -> None:
    best = getattr(engine, "_emuflow_best_diagnostic_position", None)
    if best is None:
        return
    engine.data_cls.pos[0].data.copy_(best)
    engine._emuflow_restored_diagnostic_position = best.clone()
    state["restored_best_diagnostic"] = True


def _record_post_adjustment_candidate(
    engine: Any,
    state: MutableMapping[str, Any],
    metric: Any,
    *,
    hpwl: float,
    maximum_logic_overflow: float,
) -> None:
    """Keep the least-disruptive sample after routability inflation.

    OpenPARF recomputes overflow with inflated pseudo areas and resets its
    optimizer after an area adjustment.  Requiring that perturbed metric to
    return to the pre-inflation handoff threshold can spend hundreds of
    iterations optimizing a proxy while delaying the exact legalizer and
    router that actually decide whether the placement is usable.  A short
    post-adjustment window is sufficient to expose the congestion gradient;
    within that window prefer the smallest inflated logic overflow, then HPWL.
    """

    state["post_adjustment_samples"] += 1
    candidate_key = (maximum_logic_overflow, hpwl)
    best_key = state.get("best_post_adjustment_key")
    if best_key is None or candidate_key < tuple(best_key):
        state["best_post_adjustment_key"] = list(candidate_key)
        state["best_diagnostic_max_overflow"] = maximum_logic_overflow
        state["best_diagnostic_hpwl"] = hpwl
        state["best_diagnostic_iteration"] = _iteration(metric)
        state["best_diagnostic_logic_overflow"] = dict(
            state["terminal_logic_overflow"]
        )
        engine._emuflow_best_diagnostic_position = (
            engine.data_cls.pos[0].data.clone()
        )


def stable_native_stop_condition(engine: Any, metrics: list[Any]) -> bool:
    """Require stable feasible progress and restore the best feasible iterate."""

    if not metrics:
        return False
    current = metrics[-1]
    iteration = _iteration(current)
    marker = (
        int(getattr(engine, "num_gp_adjust_area", 0)),
        int(getattr(engine, "last_area_inflation_iter", -1) or -1),
    )
    state = getattr(engine, "_emuflow_stable_convergence", None)
    if state is None or state.get("adjustment_marker") != marker:
        state = {
            "adjustment_marker": marker,
            "first_feasible_iteration": None,
            "best_feasible_iteration": None,
            "best_feasible_hpwl": None,
            "feasible_samples": 0,
            "consecutive_feasible_samples": 0,
            "restored_best_feasible": False,
            "first_diagnostic_iteration": None,
            "best_diagnostic_iteration": None,
            "best_diagnostic_hpwl": None,
            "best_diagnostic_logic_overflow": None,
            "best_diagnostic_max_overflow": None,
            "last_diagnostic_hpwl_improvement_iteration": None,
            "diagnostic_samples": 0,
            "post_adjustment_samples": 0,
            "best_post_adjustment_key": None,
            "restored_best_diagnostic": False,
            "stop_reason": None,
        }
        engine._emuflow_stable_convergence = state
        engine._emuflow_best_feasible_position = None
        engine._emuflow_best_diagnostic_position = None

    state["terminal_logic_overflow"] = _logic_overflow(engine, current)

    maximum_iterations = int(engine.params.max_global_place_iters)
    at_iteration_limit = iteration >= maximum_iterations
    if not _has_active_area_type(engine):
        _restore_best_feasible(engine, state)
        state["stop_reason"] = "empty-active-subspace"
        return True
    hpwl = _weighted_hpwl(engine, current)
    if not math.isfinite(hpwl) or hpwl < 0.0:
        raise RuntimeError("OpenPARF produced an invalid weighted HPWL")
    diagnostic_limit = float(
        getattr(engine.params, "emuflow_diagnostic_handoff_overflow", 0.25)
    )
    diagnostic_patience = int(
        getattr(engine.params, "emuflow_diagnostic_handoff_patience", 64)
    )
    adjustment_threshold = float(
        getattr(engine.params, "gp_adjust_area_overflow_threshold", 0.0)
    )
    if (
        not math.isfinite(diagnostic_limit)
        or not float(engine.params.stop_overflow) < diagnostic_limit < 1.0
        or diagnostic_patience < 0
        or (
            bool(getattr(engine.params, "gp_adjust_area", False))
            and (
                not math.isfinite(adjustment_threshold)
                or adjustment_threshold + 1.0e-12 < diagnostic_limit
            )
        )
    ):
        raise RuntimeError("OpenPARF diagnostic handoff policy is invalid")
    maximum_logic_overflow = max(state["terminal_logic_overflow"].values())
    completed_adjustments = int(getattr(engine, "num_gp_adjust_area", 0))
    last_adjustment_iteration = int(
        getattr(engine, "last_area_inflation_iter", -1) or -1
    )
    diagnostic_adjustment_ready = (
        not bool(getattr(engine.params, "gp_adjust_area", False))
        or (
            completed_adjustments > 0
            and iteration > last_adjustment_iteration
        )
    )
    feedback_samples = int(
        getattr(engine.params, "emuflow_post_adjustment_feedback_samples", 0)
    )
    if feedback_samples <= 0:
        raise RuntimeError(
            "OpenPARF post-adjustment feedback window must be positive"
        )
    if diagnostic_adjustment_ready and completed_adjustments > 0:
        _record_post_adjustment_candidate(
            engine,
            state,
            current,
            hpwl=hpwl,
            maximum_logic_overflow=maximum_logic_overflow,
        )
        if state["post_adjustment_samples"] >= feedback_samples:
            _restore_best_diagnostic(engine, state)
            state["stop_reason"] = "routability-feedback-handoff"
            return True
    if (
        maximum_logic_overflow <= diagnostic_limit
        and diagnostic_adjustment_ready
    ):
        state["diagnostic_samples"] += 1
        if state["first_diagnostic_iteration"] is None:
            state["first_diagnostic_iteration"] = iteration
        best_diagnostic_overflow = state["best_diagnostic_max_overflow"]
        if (
            best_diagnostic_overflow is None
            or maximum_logic_overflow
            < float(best_diagnostic_overflow) - 1.0e-4
        ):
            state["best_diagnostic_max_overflow"] = maximum_logic_overflow
        best_diagnostic_hpwl = state["best_diagnostic_hpwl"]
        if best_diagnostic_hpwl is None or hpwl < float(best_diagnostic_hpwl):
            state["best_diagnostic_hpwl"] = hpwl
            state["best_diagnostic_iteration"] = iteration
            state["best_diagnostic_logic_overflow"] = dict(
                state["terminal_logic_overflow"]
            )
            engine._emuflow_best_diagnostic_position = (
                engine.data_cls.pos[0].data.clone()
            )
            state["last_diagnostic_hpwl_improvement_iteration"] = iteration
    if not _active_overflow_is_feasible(engine, current):
        state["consecutive_feasible_samples"] = 0
        if at_iteration_limit:
            _restore_best_feasible(engine, state)
            if state["restored_best_feasible"]:
                state["stop_reason"] = "maximum-iterations-feasible"
            else:
                _restore_best_diagnostic(engine, state)
                state["stop_reason"] = (
                    "maximum-iterations-diagnostic"
                    if state["restored_best_diagnostic"]
                    else "maximum-iterations-infeasible"
                )
            return True
        last_diagnostic_hpwl_improvement = state[
            "last_diagnostic_hpwl_improvement_iteration"
        ]
        if (
            last_diagnostic_hpwl_improvement is not None
            and iteration - int(last_diagnostic_hpwl_improvement)
            >= diagnostic_patience
        ):
            _restore_best_diagnostic(engine, state)
            state["stop_reason"] = "diagnostic-handoff-patience"
            return True
        return False

    if not _area_adjustment_is_finished(engine) and not at_iteration_limit:
        return False
    state["feasible_samples"] += 1
    state["consecutive_feasible_samples"] += 1
    if state["first_feasible_iteration"] is None:
        state["first_feasible_iteration"] = iteration
    relative_improvement = float(
        getattr(engine.params, "emuflow_relative_hpwl_improvement", 0.0)
    )
    best_hpwl = state["best_feasible_hpwl"]
    if (
        best_hpwl is None
        or hpwl < float(best_hpwl) * (1.0 - relative_improvement)
    ):
        state["best_feasible_hpwl"] = hpwl
        state["best_feasible_iteration"] = iteration
        engine._emuflow_best_feasible_position = (
            engine.data_cls.pos[0].data.clone()
        )

    # The iteration-limit sample is a real optimization result.  Record and
    # restore it before deciding whether bounded placement succeeded; checking
    # the limit before the current density metric incorrectly rejected runs
    # whose first feasible point occurred exactly at the final iteration.
    if at_iteration_limit:
        _restore_best_feasible(engine, state)
        state["stop_reason"] = "maximum-iterations-feasible"
        return True

    best_iteration = int(state["best_feasible_iteration"])
    minimum_feasible_iterations = int(
        getattr(engine.params, "emuflow_min_feasible_iterations", 0)
    )
    patience = int(getattr(engine.params, "emuflow_convergence_patience", 0))
    if minimum_feasible_iterations < 0 or patience < 0:
        raise RuntimeError("OpenPARF stable-convergence limits must be nonnegative")
    if (
        int(state["consecutive_feasible_samples"])
        < minimum_feasible_iterations
        or iteration - best_iteration < patience
    ):
        return False

    _restore_best_feasible(engine, state)
    state["stop_reason"] = "feasible-hpwl-patience"
    return True


def native_metrics_path(placement_path: Path) -> Path:
    return placement_path.with_suffix(".native-metrics.json")


def _write_native_metrics(engine: Any, placement_path: Path) -> None:
    state = getattr(engine, "_emuflow_stable_convergence", {})
    final_hpwl_values = engine.op_cls.hpwl_op(engine.data_cls.pos[0])
    final_hpwl = sum(
        _number(value) * float(engine.params.wirelength_weights[index])
        for index, value in enumerate(final_hpwl_values)
    )
    stop_reason = state.get("stop_reason")
    if stop_reason in {
        "feasible-hpwl-patience",
        "maximum-iterations-feasible",
        "empty-active-subspace",
    }:
        status = "pass"
    elif stop_reason in {
        "diagnostic-handoff-patience",
        "maximum-iterations-diagnostic",
        "routability-feedback-handoff",
    }:
        status = "diagnostic"
    else:
        status = "fail"
    current_metric = getattr(engine, "cur_metric_record", None)
    termination_iteration = (
        _iteration(current_metric) if current_metric is not None else None
    )
    value = {
        "schema": OPENPARF_NATIVE_CONVERGENCE_SCHEMA,
        "status": status,
        "stop_reason": state.get("stop_reason"),
        "iterations": termination_iteration,
        "area_adjustments": int(getattr(engine, "num_gp_adjust_area", 0)),
        "first_feasible_iteration": state.get("first_feasible_iteration"),
        "best_feasible_iteration": state.get("best_feasible_iteration"),
        "best_feasible_hpwl": state.get("best_feasible_hpwl"),
        "final_legal_hpwl": final_hpwl,
        "feasible_samples": int(state.get("feasible_samples", 0)),
        "consecutive_feasible_samples": int(
            state.get("consecutive_feasible_samples", 0)
        ),
        "restored_best_feasible": bool(
            state.get("restored_best_feasible", False)
        ),
        "first_diagnostic_iteration": state.get(
            "first_diagnostic_iteration"
        ),
        "best_diagnostic_iteration": state.get("best_diagnostic_iteration"),
        "best_diagnostic_hpwl": state.get("best_diagnostic_hpwl"),
        "best_diagnostic_logic_overflow": state.get(
            "best_diagnostic_logic_overflow"
        ),
        "best_diagnostic_max_overflow": state.get(
            "best_diagnostic_max_overflow"
        ),
        "diagnostic_samples": int(state.get("diagnostic_samples", 0)),
        "post_adjustment_samples": int(
            state.get("post_adjustment_samples", 0)
        ),
        "last_area_adjustment_iteration": int(
            getattr(engine, "last_area_inflation_iter", -1) or -1
        ),
        "restored_best_diagnostic": bool(
            state.get("restored_best_diagnostic", False)
        ),
        "feasibility_scope": "logic-area-types-before-exact-legalization",
        "terminal_logic_overflow": state.get("terminal_logic_overflow", {}),
        "policy": {
            "minimum_feasible_iterations": int(
                engine.params.emuflow_min_feasible_iterations
            ),
            "convergence_patience": int(
                engine.params.emuflow_convergence_patience
            ),
            "relative_hpwl_improvement": float(
                engine.params.emuflow_relative_hpwl_improvement
            ),
            "diagnostic_handoff_overflow": float(
                engine.params.emuflow_diagnostic_handoff_overflow
            ),
            "diagnostic_handoff_patience": int(
                engine.params.emuflow_diagnostic_handoff_patience
            ),
            "post_adjustment_feedback_samples": int(
                engine.params.emuflow_post_adjustment_feedback_samples
            ),
            "routability_adjustment_required": bool(
                engine.params.gp_adjust_area
            ),
            "routability_adjustment_overflow_threshold": float(
                engine.params.gp_adjust_area_overflow_threshold
            ),
            "sll_weighting": {
                "provider": "leaps-fixed-adam-step-v2",
                "step_size": float(engine.params.wlw_psi_step_size),
                "delta_sll_ema_weight": float(
                    engine.params.wlw_delta_sll_ema_weight
                ),
                "accepted_range": [0.0, 1000.0],
            },
            "density_filler_sampling": engine._emuflow_filler_geometry,
        },
    }
    weighting = getattr(engine, "_emuflow_high_fanout_weighting", None)
    if weighting is not None:
        value["high_fanout_net_weighting"] = weighting
    geometry = getattr(engine, "_emuflow_logic_site_area", None)
    if geometry is not None:
        value["logic_site_area_validation"] = geometry
    cutoff = getattr(engine, "_emuflow_area_adjustment_cutoff", None)
    if cutoff is not None:
        value["area_adjustment_cutoff"] = cutoff
    diagnostic_position = getattr(
        engine, "_emuflow_restored_diagnostic_position", None
    )
    if diagnostic_position is not None:
        import torch

        begin, end = map(int, engine.data_cls.movable_range)
        legal_position = engine.data_cls.pos[0][begin:end]
        displacement = torch.norm(
            legal_position - diagnostic_position[begin:end], dim=1
        )
        value["diagnostic_legalization_displacement"] = {
            "instances": int(displacement.numel()),
            "mean": _number(displacement.mean()),
            "maximum": _number(displacement.max()),
        }
    output = native_metrics_path(placement_path)
    output.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def validate_openparf_native_metrics(
    path: Path, *, allow_detailed_legalization_handoff: bool = False
) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read OpenPARF native metrics {path}: {error}") from error
    if (
        not isinstance(value, dict)
        or value.get("schema") != OPENPARF_NATIVE_CONVERGENCE_SCHEMA
    ):
        raise RuntimeError("OpenPARF native convergence certificate is invalid")
    policy = value.get("policy")
    sll_weighting = (
        policy.get("sll_weighting") if isinstance(policy, dict) else None
    )
    step_size = (
        sll_weighting.get("step_size")
        if isinstance(sll_weighting, dict) else None
    )
    ema_weight = (
        sll_weighting.get("delta_sll_ema_weight")
        if isinstance(sll_weighting, dict) else None
    )
    filler_sampling = (
        policy.get("density_filler_sampling")
        if isinstance(policy, dict) else None
    )
    if (
        not isinstance(sll_weighting, dict)
        or sll_weighting.get("provider") != "leaps-fixed-adam-step-v2"
        or isinstance(step_size, bool)
        or not isinstance(step_size, (int, float))
        or not math.isfinite(float(step_size))
        or float(step_size) <= 0.0
        or isinstance(ema_weight, bool)
        or not isinstance(ema_weight, (int, float))
        or not math.isfinite(float(ema_weight))
        or not 0.0 < float(ema_weight) <= 1.0
        or sll_weighting.get("accepted_range") != [0.0, 1000.0]
    ):
        raise RuntimeError("OpenPARF native SLL weighting policy is invalid")
    if (
        not isinstance(filler_sampling, dict)
        or filler_sampling.get("provider")
        != "density-bin-bounded-fillers-v2"
        or filler_sampling.get("status") != "pass"
        or filler_sampling.get("maximum_bin_span") != [0.5, 0.5]
        or not isinstance(filler_sampling.get("resources"), dict)
        or not filler_sampling["resources"]
    ):
        raise RuntimeError("OpenPARF native density filler policy is invalid")
    for resource, geometry in filler_sampling["resources"].items():
        if not isinstance(resource, str) or not isinstance(geometry, dict):
            raise RuntimeError("OpenPARF native density filler policy is invalid")
        count = geometry.get("count")
        size = geometry.get("size")
        bin_size = geometry.get("bin_size")
        span = geometry.get("bin_span")
        if (
            isinstance(count, bool) or not isinstance(count, int) or count <= 0
            or not all(
                isinstance(values, list) and len(values) == 2
                for values in (size, bin_size, span)
            )
        ):
            raise RuntimeError("OpenPARF native density filler policy is invalid")
        for axis in range(2):
            values = (size[axis], bin_size[axis], span[axis])
            if (
                any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or float(value) <= 0.0
                    for value in values
                )
                or float(span[axis]) > 0.5 + 1.0e-6
                or not math.isclose(
                    float(span[axis]),
                    float(size[axis]) / float(bin_size[axis]),
                    rel_tol=1.0e-9,
                    abs_tol=1.0e-9,
                )
            ):
                raise RuntimeError(
                    "OpenPARF native density filler policy is invalid"
                )
        # Old immutable certificates retain their recorded full-capacity
        # budget. New producers independently qualify the target budget.
        if "area_budget_provider" in filler_sampling:
            if filler_sampling["area_budget_provider"] != "target-density-filler-budget-v1":
                raise RuntimeError("OpenPARF filler area budget provider is invalid")
            _validate_filler_area_budget(
                geometry.get("area_budget"), count * size[0] * size[1]
            )
    stop_reason = value.get("stop_reason")
    globally_feasible = value.get("status") == "pass" and stop_reason in {
        "feasible-hpwl-patience",
        "maximum-iterations-feasible",
        "empty-active-subspace",
    }
    detailed_legalization_handoff = (
        allow_detailed_legalization_handoff
        and value.get("status") == "diagnostic"
        and stop_reason in {
            "diagnostic-handoff-patience",
            "maximum-iterations-diagnostic",
            "routability-feedback-handoff",
        }
        and value.get("restored_best_diagnostic") is True
    )
    if not globally_feasible and not detailed_legalization_handoff:
        raise RuntimeError("OpenPARF native convergence certificate is invalid")
    if stop_reason not in {
        "feasible-hpwl-patience",
        "maximum-iterations-feasible",
        "empty-active-subspace",
        "diagnostic-handoff-patience",
        "maximum-iterations-diagnostic",
        "maximum-iterations-infeasible",
        "routability-feedback-handoff",
    }:
        raise RuntimeError("OpenPARF native placement did not converge")
    if stop_reason in {
        "feasible-hpwl-patience", "maximum-iterations-feasible"
    } and not value.get("restored_best_feasible"):
        raise RuntimeError("OpenPARF did not restore its best feasible placement")
    iterations = value.get("iterations")
    if not isinstance(iterations, int) or iterations < 0:
        raise RuntimeError("OpenPARF native termination iteration is invalid")
    for key in ("best_feasible_hpwl", "final_legal_hpwl"):
        number = value.get(key)
        if number is not None and (
            not isinstance(number, (int, float)) or not math.isfinite(float(number))
        ):
            raise RuntimeError(f"OpenPARF native metric {key} is invalid")
    if detailed_legalization_handoff and value.get("restored_best_feasible"):
        raise RuntimeError(
            "OpenPARF diagnostic handoff cannot claim a restored feasible iterate"
        )
    if value.get("status") == "diagnostic":
        threshold = policy.get("diagnostic_handoff_overflow")
        patience = policy.get("diagnostic_handoff_patience")
        adjustment_required = policy.get("routability_adjustment_required")
        adjustment_threshold = policy.get(
            "routability_adjustment_overflow_threshold"
        )
        overflow = value.get("best_diagnostic_logic_overflow")
        displacement = value.get("diagnostic_legalization_displacement")
        feedback_samples = policy.get("post_adjustment_feedback_samples")
        feedback_handoff = stop_reason == "routability-feedback-handoff"
        observed_feedback_samples = value.get("post_adjustment_samples")
        best_iteration = value.get("best_diagnostic_iteration")
        adjustment_iteration = value.get("last_area_adjustment_iteration")
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(float(threshold))
            or not 0.1 < float(threshold) < 1.0
            or isinstance(patience, bool)
            or not isinstance(patience, int)
            or patience < 0
            or adjustment_required is not True
            or isinstance(adjustment_threshold, bool)
            or not isinstance(adjustment_threshold, (int, float))
            or not math.isfinite(float(adjustment_threshold))
            or float(adjustment_threshold) + 1.0e-12 < float(threshold)
            or isinstance(value.get("area_adjustments"), bool)
            or not isinstance(value.get("area_adjustments"), int)
            or value["area_adjustments"] <= 0
            or not isinstance(overflow, dict)
            or not overflow
            or any(
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not math.isfinite(float(number))
                or (
                    not feedback_handoff
                    and float(number) > float(threshold) + 1.0e-9
                )
                for number in overflow.values()
            )
            or isinstance(feedback_samples, bool)
            or not isinstance(feedback_samples, int)
            or feedback_samples <= 0
            or (
                feedback_handoff
                and (
                    isinstance(observed_feedback_samples, bool)
                    or not isinstance(observed_feedback_samples, int)
                    or observed_feedback_samples < feedback_samples
                    or isinstance(best_iteration, bool)
                    or not isinstance(best_iteration, int)
                    or isinstance(adjustment_iteration, bool)
                    or not isinstance(adjustment_iteration, int)
                    or best_iteration <= adjustment_iteration
                )
            )
            or not isinstance(displacement, dict)
            or isinstance(displacement.get("instances"), bool)
            or not isinstance(displacement.get("instances"), int)
            or displacement["instances"] <= 0
            or any(
                isinstance(displacement.get(name), bool)
                or not isinstance(displacement.get(name), (int, float))
                or not math.isfinite(float(displacement[name]))
                or float(displacement[name]) < 0.0
                for name in ("mean", "maximum")
            )
            or float(displacement["maximum"]) < float(displacement["mean"])
        ):
            raise RuntimeError(
                "OpenPARF diagnostic legalization evidence is invalid"
            )
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run native OpenPARF with stable feasible convergence"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--log", required=True)
    arguments = parser.parse_args()

    from openparf.flow import place, route
    from openparf.params import Params
    from openparf.placement import placer

    params = Params()
    params.load(arguments.config)
    if not bool(getattr(params, "emuflow_stable_global_placement", False)):
        raise RuntimeError("stable native OpenPARF driver was not explicitly enabled")
    post_adjustment_reserve = int(
        getattr(params, "emuflow_post_adjustment_iteration_reserve", -1)
    )
    stable_window = int(params.emuflow_min_feasible_iterations) + int(
        params.emuflow_convergence_patience
    )
    feedback_window = int(params.emuflow_post_adjustment_feedback_samples)
    if feedback_window <= 0 or post_adjustment_reserve < max(
        stable_window, feedback_window
    ):
        raise RuntimeError(
            "OpenPARF post-adjustment reserve must cover the stable window"
        )
    Path(arguments.log).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=arguments.log,
        filemode="w",
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )
    os.environ["OMP_NUM_THREADS"] = str(params.num_threads)

    original_call = placer.Placer.__call__
    original_write = placer.Placer.write
    original_init = placer.Placer.__init__
    original_adjust_area_condition = placer.Placer._gp_adjust_area_condition
    completed_engine: dict[str, Any] = {}

    def init_with_high_fanout_weights(
        engine: Any, loaded_params: Any, placedb: Any
    ) -> None:
        original_init(engine, loaded_params, placedb)
        engine._emuflow_logic_site_area = validate_native_logic_site_area(engine)
        engine._emuflow_filler_geometry = validate_native_filler_geometry(engine)
        apply_high_fanout_net_weights(engine)

    def call_and_capture(engine: Any) -> Any:
        try:
            return original_call(engine)
        finally:
            completed_engine["value"] = engine

    def adjust_area_condition_with_reserve(
        engine: Any, current_metric: Any
    ) -> bool:
        return bounded_native_area_adjustment_condition(
            engine,
            current_metric,
            original_adjust_area_condition,
        )

    placer.Placer.stop_condition = stable_native_stop_condition
    placer.Placer._gp_adjust_area_condition = adjust_area_condition_with_reserve
    placer.Placer.__init__ = init_with_high_fanout_weights
    placer.Placer.__call__ = call_and_capture
    placement_path = Path(params.result_dir) / f"{params.design_name()}.pl"
    place(params, str(placement_path))
    engine = completed_engine.get("value")
    if engine is None:
        raise RuntimeError("OpenPARF native driver did not capture its placer")
    # Some upstream flows pre-create an empty Bookshelf placement and bypass
    # the Python writer on exit.  The captured, legalized engine is the
    # authority; materialize it explicitly before sealing convergence.
    if not placement_path.is_file() or placement_path.stat().st_size == 0:
        original_write(engine, str(placement_path))
    _write_native_metrics(engine, placement_path)
    if params.route_flag:
        route(params, str(placement_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
