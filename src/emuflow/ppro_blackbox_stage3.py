"""Conservative Stage-3 capacity and effective-topology fitting."""

from __future__ import annotations

import re
import math
from collections import defaultdict
from statistics import median
from typing import Any, Dict, Mapping, Sequence

from .errors import ValidationError
from .ppro_blackbox_calibration import (
    validate_blackbox_observation,
    validate_public_platform_prior,
)
from .ppro_blackbox_route_evidence import shortest_payload_hops


CAPACITY_FIT_SCHEMA = "emuflow.ppro-capacity-fit/v1"
CALIBRATED_CAPACITY_SCHEMA = "emuflow.ppro-calibrated-capacity/v2"
TOPOLOGY_FIT_SCHEMA = "emuflow.ppro-effective-topology-fit/v1"
_CAPACITY_GENERATOR = re.compile(r"^ppro-blackbox-capacity-(.+)-v4$")
_CALIBRATED_AXES = {
    # public-prior resource, ordinary-report resource, public-unit divisor
    "lut": ("clb_lut", "lut", 1.0),
    "ff": ("clb_ff", "ff", 1.0),
    "bram": ("bram_kib", "bram36k", 36.0),
    "uram": ("uram_kib", "uram288", 288.0),
    "dsp": ("dsp", "dsp48", 1.0),
}


def _validated(observations: Sequence[Mapping[str, Any]]) -> list[Dict[str, Any]]:
    if not observations:
        raise ValidationError("Stage-3 fitting requires observations")
    return [validate_blackbox_observation(item) for item in observations]


def _integer_metric(metrics: Mapping[str, Any], name: str, context: str) -> int:
    value = metrics.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise ValidationError(f"{context}: missing integer design metric {name}")
    return int(value)


def fit_capacity_intervals(
    observations: Sequence[Mapping[str, Any]], *, min_repeats: int = 2
) -> Dict[str, Any]:
    """Fit pass/fail intervals; never extrapolate a single-point capacity."""
    if min_repeats < 2:
        raise ValidationError("capacity fitting requires at least two repeats")
    normalized = _validated(observations)
    groups: dict[tuple[str, int, str], list[Dict[str, Any]]] = defaultdict(list)
    excluded = 0
    for item in normalized:
        if item["experiment"]["kind"] != "resource_capacity":
            raise ValidationError("capacity fitting received a non-capacity observation")
        match = _CAPACITY_GENERATOR.fullmatch(item["workload"]["generator_id"])
        if not match:
            raise ValidationError("capacity observation has an unknown generator id")
        design = item["metrics"]["design"]
        if "requested_units" not in design:
            if item["execution"]["outcome"] not in {
                "pass",
                "capacity_infeasible",
            }:
                excluded += 1
                continue
            raise ValidationError("evaluated capacity observation lacks requested_units")
        units = _integer_metric(design, "requested_units", "capacity observation")
        groups[(match.group(1), units, item["identity"]["role"])].append(item)

    axes: Dict[str, Any] = {}
    holdout_checks = []
    for axis in sorted({key[0] for key in groups}):
        holdout_start = len(holdout_checks)
        stable_fit: dict[int, str] = {}
        resource_at_pass: dict[int, Dict[str, float]] = {}
        for (group_axis, units, role), items in sorted(groups.items()):
            if group_axis != axis:
                continue
            if len(items) < min_repeats:
                raise ValidationError(
                    f"capacity {axis} point {units} has fewer than {min_repeats} repeats"
                )
            outcomes = {item["execution"]["outcome"] for item in items}
            if len(outcomes) != 1:
                raise ValidationError(f"capacity {axis} point {units} is not reproducible")
            outcome = next(iter(outcomes))
            if outcome not in {"pass", "capacity_infeasible"}:
                excluded += len(items)
                continue
            if role == "fit":
                stable_fit[units] = outcome
                if outcome == "pass":
                    resource_at_pass[units] = {
                        name: max(item["metrics"]["resource_demand"].get(name, 0.0) for item in items)
                        for name in sorted(
                            set().union(*(item["metrics"]["resource_demand"] for item in items))
                        )
                    }

        passing = sorted(units for units, outcome in stable_fit.items() if outcome == "pass")
        failing = sorted(
            units for units, outcome in stable_fit.items() if outcome == "capacity_infeasible"
        )
        if not passing or not failing:
            raise ValidationError(f"capacity {axis} requires repeated pass and infeasible points")
        lower = passing[-1]
        upper_candidates = [value for value in failing if value > lower]
        if not upper_candidates:
            raise ValidationError(f"capacity {axis} observations are non-monotonic")
        upper = upper_candidates[0]
        if any(outcome == "capacity_infeasible" for units, outcome in stable_fit.items() if units <= lower):
            raise ValidationError(f"capacity {axis} observations are non-monotonic")
        if any(outcome == "pass" for units, outcome in stable_fit.items() if units >= upper):
            raise ValidationError(f"capacity {axis} observations are non-monotonic")
        axes[axis] = {
            "lower_successful_units": lower,
            "upper_infeasible_units": upper,
            "interval_width_units": upper - lower,
            "resource_demand_at_lower": resource_at_pass.get(lower, {}),
            "provenance": "black_box_fitted",
        }

        for (group_axis, units, role), items in sorted(groups.items()):
            if group_axis != axis or role != "holdout":
                continue
            outcome = items[0]["execution"]["outcome"]
            expected = (
                "pass"
                if units <= lower
                else "capacity_infeasible"
                if units >= upper
                else "unresolved_interval"
            )
            holdout_checks.append(
                {
                    "axis": axis,
                    "requested_units": units,
                    "expected": expected,
                    "observed": outcome,
                    "matches": expected == "unresolved_interval" or expected == outcome,
                }
            )
        axis_holdouts = holdout_checks[holdout_start:]
        if not axis_holdouts or not any(
            item["expected"] != "unresolved_interval" for item in axis_holdouts
        ):
            raise ValidationError(f"capacity {axis} requires a resolved independent holdout")

    if not axes:
        raise ValidationError("capacity fitting produced no identifiable axis")
    return {
        "schema": CAPACITY_FIT_SCHEMA,
        "axes": axes,
        "holdout_checks": holdout_checks,
        "excluded_observations": excluded,
        "all_resolved_holdouts_match": all(item["matches"] for item in holdout_checks),
    }


def finalize_calibrated_capacity(
    *,
    prior: Mapping[str, Any],
    boundary_fits: Sequence[Mapping[str, Any]],
    normalization_observations: Sequence[Mapping[str, Any]],
    utilization_limit_percent: int,
    utilization_report_tolerance: float = 0.010000001,
) -> Dict[str, Any]:
    """Combine public device limits with black-box capacity evidence.

    Device totals and the user-selected utilization limit are known inputs,
    not parameters that should be reverse engineered by constructing a
    multi-million-cell failure case.  Ordinary PPro reports instead verify
    resource-unit normalization on moderate fit/holdout probes.  Existing
    one-to-one hard-resource pass/fail intervals independently check that the
    configured limit lies inside the observed boundary.
    """

    if (
        isinstance(utilization_limit_percent, bool)
        or not isinstance(utilization_limit_percent, int)
        or not 1 <= utilization_limit_percent <= 100
    ):
        raise ValidationError("capacity utilization limit must be an integer in [1, 100]")
    if (
        isinstance(utilization_report_tolerance, bool)
        or not isinstance(utilization_report_tolerance, (int, float))
        or not 0.0 <= utilization_report_tolerance <= 0.05
    ):
        raise ValidationError("capacity utilization-report tolerance must be in [0, 0.05]")

    normalized_prior = validate_public_platform_prior(prior)
    prior_resources = {
        item["name"]: float(item["value"])
        for item in normalized_prior["device"]["resources"]
    }
    public_capacity = {
        axis: int(math.floor(prior_resources[public_name] / divisor))
        for axis, (public_name, _, divisor) in _CALIBRATED_AXES.items()
    }
    if any(value <= 0 for value in public_capacity.values()):
        raise ValidationError("capacity public prior has a non-positive resource total")

    boundary_axes: Dict[str, Dict[str, Any]] = {}
    boundary_holdouts = []
    excluded = 0
    for index, fit in enumerate(boundary_fits):
        if fit.get("schema") != CAPACITY_FIT_SCHEMA:
            raise ValidationError(f"capacity boundary fit {index} has an invalid schema")
        if fit.get("all_resolved_holdouts_match") is not True:
            raise ValidationError(f"capacity boundary fit {index} failed its holdout gate")
        fit_excluded = fit.get("excluded_observations")
        if isinstance(fit_excluded, bool) or not isinstance(fit_excluded, int):
            raise ValidationError(f"capacity boundary fit {index} lacks excluded count")
        excluded += fit_excluded
        checks = fit.get("holdout_checks")
        if not isinstance(checks, list) or not checks:
            raise ValidationError(f"capacity boundary fit {index} lacks holdouts")
        boundary_holdouts.extend(checks)
        axes = fit.get("axes")
        if not isinstance(axes, Mapping) or not axes:
            raise ValidationError(f"capacity boundary fit {index} lacks axes")
        for axis, record in axes.items():
            if axis not in _CALIBRATED_AXES or not isinstance(record, Mapping):
                raise ValidationError("capacity boundary fit has an unsupported axis")
            if axis in boundary_axes:
                raise ValidationError(f"capacity boundary axis {axis} is duplicated")
            boundary_axes[axis] = dict(record)

    normalized_observations = _validated(normalization_observations)
    normalization_groups: dict[tuple[str, str], list[Dict[str, Any]]] = defaultdict(list)
    normalization_checks = []
    for item in normalized_observations:
        if item["experiment"]["kind"] != "resource_capacity":
            raise ValidationError("capacity normalization received a non-capacity observation")
        match = _CAPACITY_GENERATOR.fullmatch(item["workload"]["generator_id"])
        if not match or match.group(1) not in _CALIBRATED_AXES:
            raise ValidationError("capacity normalization has an unknown generator")
        if item["execution"]["outcome"] != "pass":
            raise ValidationError("capacity normalization requires successful ordinary reports")
        axis = match.group(1)
        role = item["identity"]["role"]
        _, report_resource, _ = _CALIBRATED_AXES[axis]
        demand = item["metrics"]["resource_demand"].get(report_resource)
        rows = item["metrics"]["fpga_utilization"]
        if (
            isinstance(demand, bool)
            or not isinstance(demand, (int, float))
            or demand <= 0
            or len(rows) != 1
        ):
            raise ValidationError(f"capacity normalization {axis} lacks one-FPGA demand")
        observed = rows[0]["resources"].get(report_resource)
        if isinstance(observed, bool) or not isinstance(observed, (int, float)):
            raise ValidationError(f"capacity normalization {axis} lacks utilization")
        expected = float(demand) / public_capacity[axis]
        error = abs(float(observed) - expected)
        check = {
            "axis": axis,
            "case_id": item["identity"]["case_id"],
            "role": role,
            "reported_demand": float(demand),
            "observed_utilization": float(observed),
            "public_capacity_utilization": expected,
            "absolute_error": error,
            "matches": error <= utilization_report_tolerance,
        }
        normalization_checks.append(check)
        normalization_groups[(axis, role)].append(item)

    axes = {}
    for axis, (_, report_resource, _) in _CALIBRATED_AXES.items():
        public = public_capacity[axis]
        effective = int(math.floor(public * utilization_limit_percent / 100.0))
        evidence = []
        boundary = boundary_axes.get(axis)
        if boundary is not None:
            lower_demand = boundary.get("resource_demand_at_lower", {}).get(report_resource)
            lower_units = boundary.get("lower_successful_units")
            upper_units = boundary.get("upper_infeasible_units")
            if (
                isinstance(lower_demand, bool)
                or not isinstance(lower_demand, (int, float))
                or lower_demand <= 0
                or isinstance(lower_units, bool)
                or not isinstance(lower_units, int)
                or isinstance(upper_units, bool)
                or not isinstance(upper_units, int)
            ):
                raise ValidationError(f"capacity boundary {axis} is incomplete")
            if float(lower_demand) > effective:
                raise ValidationError(f"capacity boundary {axis} exceeds the configured limit")
            # BRAM/DSP/URAM v4 probes map one requested unit to one reported
            # primitive.  Their failure point must bracket the public-limit
            # prediction.  LUT/FF use normalization probes instead.
            if axis in {"bram", "dsp", "uram"} and not lower_units <= effective < upper_units:
                raise ValidationError(
                    f"capacity boundary {axis} does not bracket the configured limit"
                )
            evidence.append("black_box_boundary")

        fit_items = normalization_groups.get((axis, "fit"), [])
        holdout_items = normalization_groups.get((axis, "holdout"), [])
        if fit_items or holdout_items:
            if len(fit_items) < 2 or len(holdout_items) < 2:
                raise ValidationError(
                    f"capacity normalization {axis} requires repeated fit and holdout evidence"
                )
            axis_checks = [item for item in normalization_checks if item["axis"] == axis]
            if not all(item["matches"] for item in axis_checks):
                raise ValidationError(f"capacity normalization {axis} disagrees with public units")
            evidence.append("ordinary_report_normalization")

        if not evidence:
            raise ValidationError(f"capacity axis {axis} has no calibration evidence")
        axes[axis] = {
            "public_resource_capacity": public,
            "configured_utilization_limit_percent": utilization_limit_percent,
            "effective_resource_capacity": effective,
            "observation_resource": report_resource,
            "evidence": sorted(evidence),
            "provenance": "public_spec_plus_black_box_validation",
        }

    holdout_checks = boundary_holdouts + [
        item for item in normalization_checks if item["role"] == "holdout"
    ]
    if excluded != 0:
        raise ValidationError("capacity calibration contains excluded observations")
    if not holdout_checks or not all(item.get("matches") is True for item in holdout_checks):
        raise ValidationError("capacity calibration holdout gate failed")
    return {
        "schema": CALIBRATED_CAPACITY_SCHEMA,
        "public_prior_id": normalized_prior["prior"]["id"],
        "utilization_limit_percent": utilization_limit_percent,
        "utilization_report_tolerance": utilization_report_tolerance,
        "axes": axes,
        "holdout_checks": holdout_checks,
        "excluded_observations": 0,
        "all_resolved_holdouts_match": True,
        "provenance": {
            "capacity_source": "public_device_specification",
            "limit_source": "documented_user_constraint",
            "validation_source": "ordinary_black_box_reports",
        },
    }


def fit_effective_topology(
    observations: Sequence[Mapping[str, Any]], *, min_repeats: int = 2
) -> Dict[str, Any]:
    """Fit only ordered-pair reachability and observable effective hop counts."""
    if min_repeats < 2:
        raise ValidationError("topology fitting requires at least two repeats")
    normalized = _validated(observations)
    groups: dict[tuple[int, int, str], list[Dict[str, Any]]] = defaultdict(list)
    probe_widths: set[int] = set()
    excluded = 0
    for item in normalized:
        if item["experiment"]["kind"] != "topology_reachability":
            raise ValidationError("topology fitting received a non-topology observation")
        if item["workload"]["generator_id"] != "ppro-blackbox-topology-reachability-v2":
            raise ValidationError("topology observation has an unknown generator id")
        design = item["metrics"]["design"]
        if not {"source_fpga_index", "sink_fpga_index", "probe_width_bits"} <= set(design):
            if item["execution"]["outcome"] not in {"pass", "routing_infeasible"}:
                excluded += 1
                continue
            raise ValidationError("evaluated topology observation lacks logical pair metrics")
        source = _integer_metric(design, "source_fpga_index", "topology observation")
        sink = _integer_metric(design, "sink_fpga_index", "topology observation")
        width = _integer_metric(design, "probe_width_bits", "topology observation")
        if source == sink or width <= 0:
            raise ValidationError("reachability fit requires a positive-width cross-FPGA ordered pair")
        probe_widths.add(width)
        groups[(source, sink, item["identity"]["role"])].append(item)

    if len(probe_widths) != 1:
        raise ValidationError("reachability fit cannot mix probe widths")

    edges = []
    holdout_checks = []
    for source, sink in sorted({(key[0], key[1]) for key in groups}):
        fitted_state = None
        for role in ("fit", "holdout"):
            items = groups.get((source, sink, role), [])
            if not items:
                continue
            if len(items) < min_repeats:
                raise ValidationError(
                    f"topology F{source}->F{sink} has fewer than {min_repeats} repeats"
                )
            outcomes = {item["execution"]["outcome"] for item in items}
            if len(outcomes) != 1:
                raise ValidationError(f"topology F{source}->F{sink} is not reproducible")
            outcome = next(iter(outcomes))
            if outcome not in {"pass", "routing_infeasible"}:
                excluded += len(items)
                continue
            state = "reachable" if outcome == "pass" else "unreachable"
            hops = []
            if state == "reachable":
                for item in items:
                    observed_hops = shortest_payload_hops(
                        item["metrics"]["routes"],
                        source=f"F{source}",
                        sink=f"F{sink}",
                        minimum_signal_count=width,
                    )
                    if observed_hops is None:
                        raise ValidationError("topology pass lacks a full-width logical route")
                    hops.append(observed_hops)
                if len(set(hops)) != 1:
                    raise ValidationError(f"topology F{source}->F{sink} hop count is unstable")
            record = {
                "source": f"F{source}",
                "sink": f"F{sink}",
                "state": state,
                "effective_hops": int(median(hops)) if hops else None,
            }
            if role == "fit":
                fitted_state = record
                edges.append(record)
            else:
                if fitted_state is None:
                    raise ValidationError(
                        f"topology F{source}->F{sink} holdout has no fitted pair prediction"
                    )
                holdout_checks.append(
                    {
                        "source": record["source"],
                        "sink": record["sink"],
                        "expected_state": fitted_state["state"],
                        "observed_state": record["state"],
                        "expected_effective_hops": fitted_state["effective_hops"],
                        "observed_effective_hops": record["effective_hops"],
                        "matches": record == fitted_state,
                    }
                )

    if not edges:
        raise ValidationError("topology fitting produced no fit edges")
    if not holdout_checks:
        raise ValidationError("topology fitting requires at least one independent holdout repeat")
    return {
        "schema": TOPOLOGY_FIT_SCHEMA,
        "probe_width_bits": next(iter(probe_widths)),
        "directed_edges": edges,
        "holdout_checks": holdout_checks,
        "all_holdouts_match": all(item["matches"] for item in holdout_checks),
        "excluded_observations": excluded,
        "shared_capacity_groups": {
            "status": "not_identifiable",
            "reason": "single-flow reachability probes do not identify shared physical resources",
        },
    }
