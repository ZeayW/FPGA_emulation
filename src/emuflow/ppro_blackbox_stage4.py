"""Stage-4 payload/TDM interval and aggregate latency fitting."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Any, Dict, Mapping, Sequence

from .errors import ValidationError
from .ppro_blackbox_calibration import validate_blackbox_observation


PAYLOAD_FIT_SCHEMA = "emuflow.ppro-payload-fit/v1"
LATENCY_FIT_SCHEMA = "emuflow.ppro-latency-fit/v1"
TRANSPORT_FIT_SCHEMA = "emuflow.ppro-transport-cost-fit/v1"
_GENERATOR_ID = "ppro-blackbox-communication-probe-v2"
_FEATURES = (
    "endpoint_ns",
    "per_hop_ns",
    "serialization_unit_ns",
    "tdm_level_ns",
    "contention_flow_ns",
    "multicast_sink_ns",
)


def _validated(observations: Sequence[Mapping[str, Any]]) -> list[Dict[str, Any]]:
    if not observations:
        raise ValidationError("Stage-4 fitting requires observations")
    return [validate_blackbox_observation(item) for item in observations]


def _integer(metrics: Mapping[str, Any], name: str, context: str) -> int:
    value = metrics.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise ValidationError(f"{context}: missing integer metric {name}")
    return int(value)


def _communication_dimensions(item: Mapping[str, Any]) -> Dict[str, int]:
    if item["workload"]["generator_id"] != _GENERATOR_ID:
        raise ValidationError("Stage-4 observation has an unknown generator id")
    design = item["metrics"]["design"]
    return {
        name: _integer(design, name, "Stage-4 observation")
        for name in (
            "bidirectional",
            "endpoint_count",
            "fanout",
            "flow_count",
            "forced_tdm_ratio",
            "probe_width_bits",
            "sink_fpga_index",
            "source_fpga_index",
        )
    }


def fit_payload_intervals(
    observations: Sequence[Mapping[str, Any]], *, min_repeats: int = 2
) -> Dict[str, Any]:
    if min_repeats < 2:
        raise ValidationError("payload fitting requires at least two repeats")
    normalized = _validated(observations)
    groups: dict[tuple[tuple[int, ...], int, str], list[Dict[str, Any]]] = defaultdict(list)
    excluded = 0
    for item in normalized:
        if item["experiment"]["kind"] != "payload_capacity":
            raise ValidationError("payload fitting received a non-payload observation")
        try:
            dims = _communication_dimensions(item)
        except ValidationError:
            if item["execution"]["outcome"] not in {"pass", "link_capacity_infeasible"}:
                excluded += 1
                continue
            raise
        signature = (
            dims["source_fpga_index"],
            dims["sink_fpga_index"],
            dims["bidirectional"],
            dims["flow_count"],
            dims["fanout"],
            dims["forced_tdm_ratio"],
        )
        groups[(signature, dims["probe_width_bits"], item["identity"]["role"])].append(item)

    links = []
    holdout_checks = []
    for signature in sorted({key[0] for key in groups}):
        stable_fit = {}
        tdm_levels = {}
        for (group_signature, width, role), items in sorted(groups.items()):
            if group_signature != signature:
                continue
            if len(items) < min_repeats:
                raise ValidationError(f"payload point width {width} lacks repeated trials")
            outcomes = {item["execution"]["outcome"] for item in items}
            if len(outcomes) != 1:
                raise ValidationError(f"payload point width {width} is not reproducible")
            outcome = next(iter(outcomes))
            if outcome not in {"pass", "link_capacity_infeasible"}:
                excluded += len(items)
                continue
            if role == "fit":
                stable_fit[width] = outcome
                if outcome == "pass":
                    ratios = {
                        _integer(item["metrics"]["communication"], "maximum_tdm_ratio", "payload observation")
                        for item in items
                    }
                    if len(ratios) != 1:
                        raise ValidationError(f"payload point width {width} has unstable TDM ratio")
                    tdm_levels[width] = next(iter(ratios))
        passing = sorted(width for width, outcome in stable_fit.items() if outcome == "pass")
        failing = sorted(
            width for width, outcome in stable_fit.items() if outcome == "link_capacity_infeasible"
        )
        if not passing or not failing:
            raise ValidationError("payload signature requires pass and link-infeasible points")
        lower = passing[-1]
        upper_candidates = [width for width in failing if width > lower]
        if not upper_candidates:
            raise ValidationError("payload observations are non-monotonic")
        upper = upper_candidates[0]
        if any(outcome == "link_capacity_infeasible" for width, outcome in stable_fit.items() if width <= lower):
            raise ValidationError("payload observations are non-monotonic")
        if any(outcome == "pass" for width, outcome in stable_fit.items() if width >= upper):
            raise ValidationError("payload observations are non-monotonic")
        source, sink, bidirectional, flow_count, fanout, forced_ratio = signature
        links.append(
            {
                "source": f"F{source}",
                "sink": f"F{sink}",
                "bidirectional": bool(bidirectional),
                "flow_count": flow_count,
                "fanout": fanout,
                "forced_tdm_ratio": forced_ratio,
                "lower_successful_width_bits": lower,
                "upper_infeasible_width_bits": upper,
                "observed_tdm_levels": [
                    {"width_bits": width, "maximum_tdm_ratio": tdm_levels[width]}
                    for width in sorted(tdm_levels)
                ],
                "provenance": "black_box_fitted",
            }
        )
        for (group_signature, width, role), items in sorted(groups.items()):
            if group_signature != signature or role != "holdout":
                continue
            expected = (
                "pass"
                if width <= lower
                else "link_capacity_infeasible"
                if width >= upper
                else "unresolved_interval"
            )
            observed = items[0]["execution"]["outcome"]
            holdout_checks.append(
                {
                    "source": f"F{source}",
                    "sink": f"F{sink}",
                    "width_bits": width,
                    "expected": expected,
                    "observed": observed,
                    "matches": expected == "unresolved_interval" or expected == observed,
                }
            )
    if not links:
        raise ValidationError("payload fitting produced no identifiable link signature")
    return {
        "schema": PAYLOAD_FIT_SCHEMA,
        "link_signatures": links,
        "holdout_checks": holdout_checks,
        "excluded_observations": excluded,
        "all_resolved_holdouts_match": all(item["matches"] for item in holdout_checks),
    }


def _nnls(features: Sequence[Sequence[float]], targets: Sequence[float]) -> list[float]:
    if not features or len(features) != len(targets):
        raise ValidationError("latency fit has no aligned samples")
    width = len(features[0])
    if any(len(row) != width for row in features):
        raise ValidationError("latency fit feature rows disagree")
    coefficients = [0.0] * width
    predictions = [0.0] * len(targets)
    for _ in range(2000):
        largest_change = 0.0
        for column in range(width):
            denominator = sum(row[column] ** 2 for row in features)
            if denominator == 0.0:
                continue
            old = coefficients[column]
            numerator = sum(
                row[column] * (target - (prediction - row[column] * old))
                for row, target, prediction in zip(features, targets, predictions)
            )
            new = max(0.0, numerator / denominator)
            delta = new - old
            if delta:
                for index, row in enumerate(features):
                    predictions[index] += row[column] * delta
            coefficients[column] = new
            largest_change = max(largest_change, abs(delta))
        if largest_change < 1e-10:
            break
    return coefficients


def _latency_row(item: Mapping[str, Any], payload_bits: int) -> tuple[list[float], float]:
    dims = _communication_dimensions(item)
    if item["execution"]["outcome"] != "pass":
        raise ValidationError("latency model accepts only successful timing observations")
    routes = item["metrics"]["routes"]
    if not routes:
        raise ValidationError("latency observation lacks route evidence")
    hops = max(route["effective_hops"] for route in routes)
    ratio = _integer(item["metrics"]["communication"], "maximum_tdm_ratio", "latency observation")
    delay = item["metrics"]["timing"].get("sr0_worst_cross_fpga_delay_ns")
    if not isinstance(delay, (int, float)) or delay < 0:
        raise ValidationError("latency observation lacks a non-negative sr0 delay")
    serialized_bits = dims["probe_width_bits"] * dims["flow_count"]
    return (
        [
            1.0,
            float(hops),
            float(math.ceil(serialized_bits / payload_bits)),
            float(max(0, ratio - 1)),
            float(max(0, dims["flow_count"] - 1)),
            float(max(0, dims["fanout"] - 1)),
        ],
        float(delay),
    )


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = fraction * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def fit_latency_model(
    observations: Sequence[Mapping[str, Any]],
    *,
    payload_bits_candidates: Sequence[int],
    bootstrap_samples: int = 128,
) -> Dict[str, Any]:
    normalized = _validated(observations)
    candidates = sorted(set(payload_bits_candidates))
    if not candidates or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in candidates):
        raise ValidationError("latency payload candidates must be positive integers")
    fit_items = []
    holdout_items = []
    excluded = 0
    for item in normalized:
        if item["experiment"]["kind"] != "latency":
            raise ValidationError("latency fitting received a non-latency observation")
        if item["execution"]["outcome"] != "pass":
            excluded += 1
            continue
        (fit_items if item["identity"]["role"] == "fit" else holdout_items).append(item)
    if len(fit_items) < len(_FEATURES) + 1:
        raise ValidationError("latency fitting requires more fit samples than coefficients")

    best = None
    for payload_bits in candidates:
        pairs = [_latency_row(item, payload_bits) for item in fit_items]
        features = [pair[0] for pair in pairs]
        targets = [pair[1] for pair in pairs]
        coefficients = _nnls(features, targets)
        residuals = [
            target - sum(value * coefficient for value, coefficient in zip(row, coefficients))
            for row, target in zip(features, targets)
        ]
        rmse = math.sqrt(sum(value * value for value in residuals) / len(residuals))
        candidate = (rmse, payload_bits, coefficients, features, targets)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    assert best is not None
    rmse, payload_bits, coefficients, features, targets = best

    if bootstrap_samples < 16:
        raise ValidationError("latency bootstrap_samples must be >= 16")
    generator = random.Random(0)
    boot = [[] for _ in coefficients]
    for _ in range(bootstrap_samples):
        indices = [generator.randrange(len(features)) for _ in features]
        sampled = _nnls([features[index] for index in indices], [targets[index] for index in indices])
        for column, value in enumerate(sampled):
            boot[column].append(value)
    parameters = {}
    for index, name in enumerate(_FEATURES):
        column_values = [row[index] for row in features]
        parameters[name] = {
            "nominal": coefficients[index],
            "aggressive": _percentile(boot[index], 0.05),
            "conservative": _percentile(boot[index], 0.95),
            "identifiable": len(set(column_values)) > 1 or name == "endpoint_ns",
        }

    holdout_checks = []
    for item in holdout_items:
        row, actual = _latency_row(item, payload_bits)
        predicted = sum(value * coefficient for value, coefficient in zip(row, coefficients))
        holdout_checks.append(
            {
                "id": item["identity"]["id"],
                "actual_ns": actual,
                "predicted_ns": predicted,
                "absolute_error_ns": abs(predicted - actual),
                "relative_error": abs(predicted - actual) / actual if actual else 0.0,
            }
        )
    return {
        "schema": LATENCY_FIT_SCHEMA,
        "payload_bits_per_cycle": payload_bits,
        "parameters": parameters,
        "fit_rmse_ns": rmse,
        "fit_samples": len(fit_items),
        "holdout_checks": holdout_checks,
        "holdout_max_relative_error": max(
            (item["relative_error"] for item in holdout_checks), default=None
        ),
        "excluded_observations": excluded,
        "provenance": "black_box_fitted",
    }


def fit_transport_cost_model(
    observations: Sequence[Mapping[str, Any]], *, bootstrap_samples: int = 128
) -> Dict[str, Any]:
    """Fit incremental transport cost from same-RTL local/cross pairs."""
    normalized = _validated(observations)
    groups: dict[tuple[str, int, str], list[Dict[str, Any]]] = defaultdict(list)
    excluded = 0
    for item in normalized:
        if item["experiment"]["kind"] != "transport_cost":
            raise ValidationError("transport fitting received a non-transport observation")
        if item["execution"]["outcome"] != "pass":
            excluded += 1
            continue
        _communication_dimensions(item)
        repeat = _integer(item["metrics"]["design"], "repeat_index", "transport observation")
        groups[(item["workload"]["rtl_sha256"], repeat, item["identity"]["role"])].append(item)

    fit_rows = []
    holdout_rows = []
    resources: set[str] = set()
    for key, items in sorted(groups.items()):
        local = [
            item
            for item in items
            if _integer(item["metrics"]["design"], "local_baseline", "transport observation") == 1
        ]
        cross = [
            item
            for item in items
            if _integer(item["metrics"]["design"], "local_baseline", "transport observation") == 0
        ]
        if len(local) != 1 or len(cross) != 1:
            raise ValidationError(
                "transport cost requires exactly one local and one cross run per RTL/repeat"
            )
        local_item, cross_item = local[0], cross[0]
        dims = _communication_dimensions(cross_item)
        ratio = _integer(
            cross_item["metrics"]["communication"],
            "maximum_tdm_ratio",
            "transport observation",
        )
        features = [
            float(dims["endpoint_count"]),
            float(
                dims["probe_width_bits"]
                * dims["flow_count"]
                * (2 if dims["bidirectional"] else 1)
            ),
            float(max(0, ratio - 1)),
            float(max(0, dims["fanout"] - 1)),
        ]
        names = set(local_item["metrics"]["resource_demand"]) | set(
            cross_item["metrics"]["resource_demand"]
        )
        deltas = {}
        for name in sorted(names):
            delta = cross_item["metrics"]["resource_demand"].get(
                name, 0.0
            ) - local_item["metrics"]["resource_demand"].get(name, 0.0)
            if delta < -1e-9:
                raise ValidationError(
                    "transport pair changed unrelated DUT mapping or optimization"
                )
            deltas[name] = max(0.0, delta)
        resources.update(deltas)
        row = {"id": cross_item["identity"]["id"], "features": features, "deltas": deltas}
        (fit_rows if key[2] == "fit" else holdout_rows).append(row)

    feature_names = (
        "per_endpoint",
        "per_transport_bit",
        "per_tdm_level",
        "per_multicast_sink",
    )
    if len(fit_rows) < len(feature_names) + 1:
        raise ValidationError(
            "transport fitting requires more paired fit samples than coefficients"
        )
    if bootstrap_samples < 16:
        raise ValidationError("transport bootstrap_samples must be >= 16")
    fitted_resources = {}
    generator = random.Random(0)
    features = [row["features"] for row in fit_rows]
    for resource in sorted(resources):
        targets = [row["deltas"].get(resource, 0.0) for row in fit_rows]
        coefficients = _nnls(features, targets)
        predictions = [
            sum(value * coefficient for value, coefficient in zip(row, coefficients))
            for row in features
        ]
        residuals = [target - prediction for target, prediction in zip(targets, predictions)]
        rmse = math.sqrt(sum(value * value for value in residuals) / len(residuals))
        boot = [[] for _ in coefficients]
        for _ in range(bootstrap_samples):
            indices = [generator.randrange(len(features)) for _ in features]
            sampled = _nnls(
                [features[index] for index in indices],
                [targets[index] for index in indices],
            )
            for column, value in enumerate(sampled):
                boot[column].append(value)
        fitted_resources[resource] = {
            "parameters": {
                name: {
                    "nominal": coefficients[index],
                    "aggressive": _percentile(boot[index], 0.05),
                    "conservative": _percentile(boot[index], 0.95),
                    "identifiable": len({row[index] for row in features}) > 1,
                }
                for index, name in enumerate(feature_names)
            },
            "fit_rmse": rmse,
        }

    holdout_checks = []
    for row in holdout_rows:
        result = {"id": row["id"], "resources": {}}
        for resource, fit in fitted_resources.items():
            coefficients = [fit["parameters"][name]["nominal"] for name in feature_names]
            predicted = sum(
                value * coefficient for value, coefficient in zip(row["features"], coefficients)
            )
            actual = row["deltas"].get(resource, 0.0)
            result["resources"][resource] = {
                "actual": actual,
                "predicted": predicted,
                "relative_error": abs(predicted - actual) / actual if actual else 0.0,
            }
        holdout_checks.append(result)
    return {
        "schema": TRANSPORT_FIT_SCHEMA,
        "resources": fitted_resources,
        "paired_fit_samples": len(fit_rows),
        "holdout_checks": holdout_checks,
        "excluded_observations": excluded,
        "provenance": "black_box_fitted",
    }
