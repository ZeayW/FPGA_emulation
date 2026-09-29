"""OpenPARF driver that publishes continuous global-placement coordinates.

The upstream Bookshelf writer accepts only discrete legal site locations.  A
global-only OpenPARF run intentionally has not performed that legalization, so
using the upstream writer would either misrepresent the result or abort.  This
small driver keeps the upstream placement engine intact and replaces only its
final serializer with a finite-coordinate writer for EmuFlow's downstream
architecture-aware legalizer.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
from pathlib import Path
from typing import Any


OPENPARF_CONTINUOUS_METRICS_SCHEMA = (
    "emuflow.openparf-continuous-metrics/v3"
)


def apply_high_fanout_net_weights(engine: Any) -> dict[str, Any]:
    """Account for the routing demand hidden by one-HPWL-per-net models.

    OpenPARF's Bookshelf reader gives every net unit weight.  That is a poor
    proxy for an FPGA control net with hundreds of spatially distributed
    sinks: its HPWL gradient sees only the bounding-box extrema while the
    detailed router must build the complete distribution tree.  Apply a
    bounded square-root fanout weight before any placement operator runs.

    The policy is deliberately compact and architecture independent.  It
    changes neither the netlist nor legality, and it avoids an unbounded
    degree-proportional force that would collapse reset sinks into one hot
    spot.  The downstream exact legalizer and router remain authoritative.
    """

    threshold = int(engine.params.emuflow_high_fanout_weight_threshold)
    exponent = float(engine.params.emuflow_high_fanout_weight_exponent)
    maximum = float(engine.params.emuflow_high_fanout_weight_maximum)
    if threshold < 2:
        raise RuntimeError("high-fanout weight threshold must be at least 2")
    if not math.isfinite(exponent) or exponent <= 0.0:
        raise RuntimeError("high-fanout weight exponent must be finite and positive")
    if not math.isfinite(maximum) or maximum < 1.0:
        raise RuntimeError("high-fanout maximum weight must be finite and at least 1")

    starts = engine.data_cls.net_pin_map.b_starts
    degrees = starts[1:] - starts[:-1]
    if int(degrees.shape[0]) != int(engine.data_cls.net_weights.shape[0]):
        raise RuntimeError("OpenPARF net degree and weight tensors disagree")
    factors = (degrees.to(dtype=engine.data_cls.net_weights.dtype) / threshold).pow(
        exponent
    )
    factors.clamp_(min=1.0, max=maximum)
    engine.data_cls.net_weights.mul_(factors)
    # The per-instance wirelength preconditioner was derived from the original
    # unit weights during DataCollections construction.  Recompute it once so
    # optimization step scaling matches the actual objective.
    engine.data_cls.compute_wl_precond(
        engine.params, engine.placedb, engine.dtype, engine.device
    )
    weighted = degrees > threshold
    report = {
        "policy": "bounded-sqrt-fanout-v1",
        "threshold": threshold,
        "exponent": exponent,
        "maximum_weight": maximum,
        "weighted_nets": int(weighted.sum().item()),
        "maximum_degree": int(degrees.max().item()) if degrees.numel() else 0,
        "observed_maximum_weight": (
            float(factors.max().item()) if factors.numel() else 1.0
        ),
    }
    engine._emuflow_high_fanout_weighting = report
    return report


def _overflow_limits(engine: Any, overflow: list[float]) -> tuple[list[int], list[float]]:
    """Return checked area types and their OpenPARF-compatible limits.

    OpenPARF requires logic to reach ``stop_overflow``. Generic upstream
    flows permit sparse single-site resources such as DSP and BRAM to remain
    below twice that value before discrete legalization. Route A delegates
    all hard-macro legality to its exact architecture-aware Xilinx legalizer,
    so its continuous certificate gates only the slice area type.
    """

    io_area_types = {
        int(engine.placedb.getAreaTypeIndexFromName(name))
        for name in engine.params.io_at_names
    }
    logic_area_types = {
        int(engine.placedb.getAreaTypeIndexFromName(name))
        for name in engine.params.logic_area_type_names
    }
    generic_clusters = bool(
        getattr(engine.params, "generic_cluster_placement_flag", False)
    )
    checked_area_types = [
        area_type
        for area_type, group in enumerate(engine.data_cls.area_type_inst_groups)
        if len(group) > 10
        and area_type not in io_area_types
        # In Route A, sparse hard macros are intentionally left to the exact
        # architecture-aware legalizer. Requiring their continuous pseudo-
        # density to converge after slice-area inflation rejects otherwise
        # legal guidance and says nothing about final DSP/BRAM/URAM sites.
        and (not generic_clusters or area_type in logic_area_types)
    ]
    stop_overflow = float(engine.params.stop_overflow)
    limits = [
        stop_overflow if area_type in logic_area_types else 2.0 * stop_overflow
        for area_type in checked_area_types
    ]
    if any(limit <= 0.0 for limit in limits) or len(overflow) < len(
        engine.data_cls.area_type_inst_groups
    ):
        raise RuntimeError("OpenPARF overflow limits are invalid")
    return checked_area_types, limits


def write_continuous_placement(engine: Any, output_path: Path) -> None:
    """Serialize every real instance from ``data_cls.pos`` without snapping."""

    positions = engine.data_cls.pos[0]
    instances = int(engine.data_cls.inst_locs_xyz.shape[0])
    if int(positions.shape[0]) < instances or int(positions.shape[1]) < 2:
        raise RuntimeError("OpenPARF global position tensor is incomplete")
    xy = positions[:instances, :2].detach().cpu().tolist()
    lines = ["# EmuFlow continuous OpenPARF global-placement guidance"]
    for index, coordinate in enumerate(xy):
        x, y = float(coordinate[0]), float(coordinate[1])
        if not math.isfinite(x) or not math.isfinite(y):
            raise RuntimeError(
                "OpenPARF global placement produced a non-finite coordinate "
                f"for {engine.placedb.instName(index)}"
            )
        lines.append(f"{engine.placedb.instName(index)} {x:.17g} {y:.17g} 0")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_convergence_certificate(engine: Any) -> dict[str, Any]:
    """Return a compact, independently inspectable global-placement gate."""

    positions = engine.data_cls.pos[0]
    instances = int(engine.data_cls.inst_locs_xyz.shape[0])
    xy = positions[:instances, :2].detach().cpu().tolist()
    finite = all(
        math.isfinite(float(coordinate))
        for point in xy
        for coordinate in point
    )
    overflow = [
        float(value)
        for value in engine.op_cls.normalized_overflow_op(positions)
        .detach()
        .cpu()
        .tolist()
    ]
    checked_area_types, overflow_limits = _overflow_limits(engine, overflow)
    stop_overflow = float(engine.params.stop_overflow)
    maximum_checked_overflow = max(
        (overflow[area_type] for area_type in checked_area_types),
        default=0.0,
    )
    maximum_limit_ratio = max(
        (
            overflow[area_type] / limit
            for area_type, limit in zip(checked_area_types, overflow_limits)
        ),
        default=0.0,
    )
    hpwl = [
        float(value)
        for value in engine.op_cls.hpwl_op(positions).detach().cpu().tolist()
    ]
    metric = getattr(engine, "cur_metric_record", None)
    opt_iter = getattr(metric, "opt_iter", None)
    iteration = int(getattr(opt_iter, "iteration", -1))
    x_values = [float(point[0]) for point in xy]
    y_values = [float(point[1]) for point in xy]
    passed = finite and maximum_limit_ratio <= 1.0
    certificate = {
        "schema": OPENPARF_CONTINUOUS_METRICS_SCHEMA,
        "status": "pass" if passed else "fail",
        "iterations": iteration,
        "instances": instances,
        "checked_area_types": checked_area_types,
        "normalized_overflow": overflow,
        "overflow_limits": overflow_limits,
        "maximum_checked_overflow": maximum_checked_overflow,
        "maximum_limit_ratio": maximum_limit_ratio,
        "stop_overflow": stop_overflow,
        "hpwl": hpwl,
        "coordinate_bbox": {
            "min_x": min(x_values, default=0.0),
            "max_x": max(x_values, default=0.0),
            "min_y": min(y_values, default=0.0),
            "max_y": max(y_values, default=0.0),
        },
        "finite_coordinates": finite,
    }
    weighting = getattr(engine, "_emuflow_high_fanout_weighting", None)
    if weighting is not None:
        certificate["high_fanout_net_weighting"] = weighting
    return certificate


def guidance_stop_condition(engine: Any, metrics: list[Any]) -> bool:
    """Stop at the first valid continuous-guidance solution.

    Waiting for every single-site resource to meet the stricter logic limit
    drives augmented multipliers past a valid guidance point when the generic
    discrete legalizer is intentionally disabled.  This condition applies the
    same logic-versus-hard-resource limits used by OpenPARF's best-solution
    gate.
    """

    if not metrics:
        return False
    current = metrics[-1]
    if current.opt_iter.iteration >= engine.params.max_global_place_iters:
        return True
    # The normal density gate can be reached before OpenPARF's routability
    # adjustment threshold.  When adjustment is requested, do not publish a
    # superficially legal but pin/RUDY-blind placement: wait until at least
    # one adjustment stage has run and the upstream adjustment loop has
    # converged (it clears ``gp_adjust_area`` when further inflation is below
    # its configured stop ratios).
    if engine.params.gp_adjust_area and (
        engine.num_gp_adjust_area == 0 or engine.gp_adjust_area
    ):
        return False
    overflow = [
        float(value)
        for value in current.overflow.detach().cpu().tolist()
    ]
    checked_area_types, limits = _overflow_limits(engine, overflow)
    return all(
        overflow[area_type] <= limit
        for area_type, limit in zip(checked_area_types, limits)
    )


def write_convergence_certificate(engine: Any, output_path: Path) -> dict[str, Any]:
    certificate = build_convergence_certificate(engine)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(certificate, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return certificate


def skip_diagnostic_plot(*_arguments: Any, **_keywords: Any) -> None:
    """Suppress upstream bitmap diagnostics in the production hot path."""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run OpenPARF and export continuous global coordinates"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--log", required=True)
    arguments = parser.parse_args()

    # Imported lazily so unit tests for the serializer do not require a built
    # OpenPARF runtime.
    from openparf.flow import place
    from openparf.params import Params
    from openparf.placement import placer

    params = Params()
    params.load(arguments.config)
    if params.legalize_flag or params.detailed_place_flag:
        raise RuntimeError(
            "continuous OpenPARF driver requires legalization and detailed "
            "placement to be disabled"
        )
    Path(arguments.log).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=arguments.log,
        filemode="w",
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )
    os.environ["OMP_NUM_THREADS"] = str(params.num_threads)

    def _write(engine: Any, filename: str) -> None:
        output = Path(filename)
        certificate = write_convergence_certificate(
            engine, output.with_suffix(".continuous-metrics.json")
        )
        if certificate["status"] != "pass":
            raise RuntimeError(
                "OpenPARF global placement did not converge: maximum overflow "
                f"limit ratio {certificate['maximum_limit_ratio']:.6g} exceeds 1"
            )
        write_continuous_placement(engine, output)

    placer.Placer.stop_condition = guidance_stop_condition
    placer.Placer.plot = skip_diagnostic_plot
    placer.Placer.write = _write
    original_init = placer.Placer.__init__

    def _init_with_high_fanout_weights(
        engine: Any, loaded_params: Any, placedb: Any
    ) -> None:
        original_init(engine, loaded_params, placedb)
        apply_high_fanout_net_weights(engine)

    placer.Placer.__init__ = _init_with_high_fanout_weights
    output = Path(params.result_dir) / f"{params.design_name()}.pl"
    place(params, str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
