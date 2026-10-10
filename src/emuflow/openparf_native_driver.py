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


def _active_overflow_is_feasible(engine: Any, metric: Any) -> bool:
    io_area_types = set()
    if bool(getattr(engine.params, "io_legalization_flag", False)):
        io_area_types = {
            int(engine.placedb.getAreaTypeIndexFromName(name))
            for name in engine.params.io_at_names
        }
    mask = engine.data_cls.optimization_area_type_mask
    for area_type, overflow in enumerate(metric.overflow):
        active = mask[area_type]
        if hasattr(active, "item"):
            active = active.item()
        if not bool(active) or area_type in io_area_types:
            continue
        if (
            len(engine.data_cls.area_type_inst_groups[area_type]) > 10
            and _number(overflow) > float(engine.params.stop_overflow)
        ):
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


def _restore_best_feasible(
    engine: Any, state: MutableMapping[str, Any]
) -> None:
    best = getattr(engine, "_emuflow_best_feasible_position", None)
    if best is None:
        return
    engine.data_cls.pos[0].data.copy_(best)
    state["restored_best_feasible"] = True


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
            "stop_reason": None,
        }
        engine._emuflow_stable_convergence = state
        engine._emuflow_best_feasible_position = None

    maximum_iterations = int(engine.params.max_global_place_iters)
    if iteration >= maximum_iterations:
        _restore_best_feasible(engine, state)
        state["stop_reason"] = (
            "maximum-iterations-feasible"
            if state["restored_best_feasible"]
            else "maximum-iterations-infeasible"
        )
        return True
    if not _has_active_area_type(engine):
        _restore_best_feasible(engine, state)
        state["stop_reason"] = "empty-active-subspace"
        return True
    if not _area_adjustment_is_finished(engine):
        return False
    if not _active_overflow_is_feasible(engine, current):
        state["consecutive_feasible_samples"] = 0
        return False

    hpwl = _weighted_hpwl(engine, current)
    if not math.isfinite(hpwl) or hpwl < 0.0:
        raise RuntimeError("OpenPARF produced an invalid weighted HPWL")
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
    status = "pass" if state.get("stop_reason") in {
        "feasible-hpwl-patience",
        "maximum-iterations-feasible",
        "empty-active-subspace",
    } else "fail"
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
        },
    }
    output = native_metrics_path(placement_path)
    output.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def validate_openparf_native_metrics(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read OpenPARF native metrics {path}: {error}") from error
    if (
        not isinstance(value, dict)
        or value.get("schema") != OPENPARF_NATIVE_CONVERGENCE_SCHEMA
        or value.get("status") != "pass"
    ):
        raise RuntimeError("OpenPARF native convergence certificate is invalid")
    stop_reason = value.get("stop_reason")
    if stop_reason not in {
        "feasible-hpwl-patience",
        "maximum-iterations-feasible",
        "empty-active-subspace",
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
    completed_engine: dict[str, Any] = {}

    def call_and_capture(engine: Any) -> Any:
        try:
            return original_call(engine)
        finally:
            completed_engine["value"] = engine

    placer.Placer.stop_condition = stable_native_stop_condition
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
