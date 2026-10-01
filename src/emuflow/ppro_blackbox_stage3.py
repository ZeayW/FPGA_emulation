"""Conservative Stage-3 capacity and effective-topology fitting."""

from __future__ import annotations

import heapq
import re
from collections import defaultdict
from statistics import median
from typing import Any, Dict, Mapping, Sequence

from .errors import ValidationError
from .ppro_blackbox_calibration import validate_blackbox_observation


CAPACITY_FIT_SCHEMA = "emuflow.ppro-capacity-fit/v1"
TOPOLOGY_FIT_SCHEMA = "emuflow.ppro-effective-topology-fit/v1"
_CAPACITY_GENERATOR = re.compile(r"^ppro-blackbox-capacity-(.+)-v4$")


def _validated(observations: Sequence[Mapping[str, Any]]) -> list[Dict[str, Any]]:
    if not observations:
        raise ValidationError("Stage-3 fitting requires observations")
    return [validate_blackbox_observation(item) for item in observations]


def _integer_metric(metrics: Mapping[str, Any], name: str, context: str) -> int:
    value = metrics.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise ValidationError(f"{context}: missing integer design metric {name}")
    return int(value)


def _payload_route_hops(
    routes: Sequence[Mapping[str, Any]], *, source: str, sink: str, width: int
) -> int | None:
    """Recover an end-to-end probe path from ordinary per-hop route records."""

    graph: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for route in routes:
        signal_count = route.get("signal_count")
        hops = route.get("effective_hops")
        route_source = route.get("source")
        sinks = route.get("sinks")
        if (
            isinstance(signal_count, bool)
            or not isinstance(signal_count, int)
            or signal_count < width
            or isinstance(hops, bool)
            or not isinstance(hops, int)
            or hops < 1
            or not isinstance(route_source, str)
            or not isinstance(sinks, list)
        ):
            continue
        for route_sink in sinks:
            if isinstance(route_sink, str):
                graph[route_source].append((route_sink, hops))
    queue = [(0, source)]
    best = {source: 0}
    while queue:
        distance, node = heapq.heappop(queue)
        if node == sink:
            return distance
        if distance != best[node]:
            continue
        for neighbor, cost in graph.get(node, []):
            candidate = distance + cost
            if candidate < best.get(neighbor, candidate + 1):
                best[neighbor] = candidate
                heapq.heappush(queue, (candidate, neighbor))
    return None


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

    if not axes:
        raise ValidationError("capacity fitting produced no identifiable axis")
    return {
        "schema": CAPACITY_FIT_SCHEMA,
        "axes": axes,
        "holdout_checks": holdout_checks,
        "excluded_observations": excluded,
        "all_resolved_holdouts_match": all(item["matches"] for item in holdout_checks),
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
                    observed_hops = _payload_route_hops(
                        item["metrics"]["routes"],
                        source=f"F{source}",
                        sink=f"F{sink}",
                        width=width,
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
