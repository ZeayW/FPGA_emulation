"""Stage-4 payload/TDM interval and aggregate latency fitting."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Any, Dict, Mapping, Sequence

from .errors import ValidationError
from .ppro_blackbox_calibration import validate_blackbox_observation
from .ppro_blackbox_route_evidence import maximum_capacity_payload_hops


PAYLOAD_FIT_SCHEMA = "emuflow.ppro-payload-fit/v2"
LATENCY_FIT_SCHEMA = "emuflow.ppro-latency-fit/v2"
TRANSPORT_FIT_SCHEMA = "emuflow.ppro-transport-cost-fit/v1"
_GENERATOR_ID = "ppro-blackbox-communication-probe-v3"
_LATENCY_BASE_FEATURES = (
    "endpoint_ns",
    "per_hop_ns",
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
            "pairing_token",
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
        holdout_start = len(holdout_checks)
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
        if not passing:
            raise ValidationError("payload signature requires successful fit points")
        first_failure = failing[0] if failing else None
        if first_failure is not None and any(width >= first_failure for width in passing):
            raise ValidationError("payload observations are non-monotonic")
        ratios = [tdm_levels[width] for width in passing]
        if any(left > right for left, right in zip(ratios, ratios[1:])):
            raise ValidationError("payload TDM ratio decreases as width increases")
        ratio_one = [width for width in passing if tdm_levels[width] == 1]
        tdm = [width for width in passing if tdm_levels[width] > 1]
        if not ratio_one or not tdm:
            raise ValidationError("payload signature requires repeated ratio-one and TDM points")
        ratio_one_lower = ratio_one[-1]
        ratio_one_upper_candidates = [width for width in tdm if width > ratio_one_lower]
        if not ratio_one_upper_candidates:
            raise ValidationError("payload ratio-one/TDM boundary is non-monotonic")
        ratio_one_upper = ratio_one_upper_candidates[0]
        source, sink, bidirectional, flow_count, fanout, forced_ratio = signature
        links.append(
            {
                "source": f"F{source}",
                "sink": f"F{sink}",
                "bidirectional": bool(bidirectional),
                "flow_count": flow_count,
                "fanout": fanout,
                "forced_tdm_ratio": forced_ratio,
                "ratio_one_lower_width_bits": ratio_one_lower,
                "ratio_one_upper_width_bits": ratio_one_upper,
                "maximum_successful_width_bits": passing[-1],
                "minimum_infeasible_width_bits": first_failure,
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
            observed = items[0]["execution"]["outcome"]
            if observed == "pass":
                observed_ratios = {
                    _integer(
                        item["metrics"]["communication"],
                        "maximum_tdm_ratio",
                        "payload holdout",
                    )
                    for item in items
                }
                if len(observed_ratios) != 1:
                    raise ValidationError(f"payload holdout width {width} has unstable TDM ratio")
                observed_class = (
                    "pass_ratio_one" if next(iter(observed_ratios)) == 1 else "pass_tdm"
                )
            else:
                observed_class = observed
            expected = "unresolved_interval"
            if first_failure is not None and width >= first_failure:
                expected = "link_capacity_infeasible"
            elif width <= ratio_one_lower:
                expected = "pass_ratio_one"
            elif width >= ratio_one_upper:
                expected = "pass_tdm"
            holdout_checks.append(
                {
                    "source": f"F{source}",
                    "sink": f"F{sink}",
                    "width_bits": width,
                    "expected": expected,
                    "observed": observed_class,
                    "matches": expected == "unresolved_interval" or expected == observed_class,
                }
            )
        if not any(
            group_signature == signature and role == "holdout"
            for group_signature, _, role in groups
        ):
            raise ValidationError("payload signature requires independent holdout trials")
        signature_holdouts = holdout_checks[holdout_start:]
        if not any(
            item["expected"] != "unresolved_interval" for item in signature_holdouts
        ):
            raise ValidationError("payload signature requires a resolved independent holdout")
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


def _matrix_rank(rows: Sequence[Sequence[float]], *, tolerance: float = 1e-10) -> int:
    if not rows:
        return 0
    matrix = [list(map(float, row)) for row in rows]
    columns = len(matrix[0])
    rank = 0
    for column in range(columns):
        pivot = max(range(rank, len(matrix)), key=lambda index: abs(matrix[index][column]))
        if abs(matrix[pivot][column]) <= tolerance:
            continue
        matrix[rank], matrix[pivot] = matrix[pivot], matrix[rank]
        scale = matrix[rank][column]
        matrix[rank] = [value / scale for value in matrix[rank]]
        for row_index in range(len(matrix)):
            if row_index == rank:
                continue
            factor = matrix[row_index][column]
            if abs(factor) <= tolerance:
                continue
            matrix[row_index] = [
                value - factor * pivot_value
                for value, pivot_value in zip(matrix[row_index], matrix[rank])
            ]
        rank += 1
        if rank == len(matrix) or rank == columns:
            break
    return rank


def _latency_row(item: Mapping[str, Any]) -> tuple[list[float], int, float]:
    dims = _communication_dimensions(item)
    if item["execution"]["outcome"] != "pass":
        raise ValidationError("latency model accepts only successful timing observations")
    routes = item["metrics"]["routes"]
    if not routes:
        raise ValidationError("latency observation lacks route evidence")
    serialized_bits = dims["probe_width_bits"] * dims["flow_count"]
    ratio = _integer(item["metrics"]["communication"], "maximum_tdm_ratio", "latency observation")
    # A route row counts logical transported paths.  A wide payload may be
    # striped over several route rows, so no individual row must carry the
    # entire payload, but their capacity must cover the full logical width.
    # TDM changes the schedule and delay; it does not reduce this path count.
    routed_payload_signals = serialized_bits
    assignment_by_partition = {
        assignment["partition"]: assignment["fpga"]
        for assignment in item["metrics"]["assignments"]
        if isinstance(assignment.get("partition"), str)
        and isinstance(assignment.get("fpga"), str)
    }
    sinks = [
        assignment_by_partition[f"P{index}"]
        for index in range(1, dims["fanout"] + 1)
        if f"P{index}" in assignment_by_partition
    ]
    if len(sinks) != dims["fanout"]:
        if dims["fanout"] != 1:
            raise ValidationError("latency observation lacks consumer assignment evidence")
        sinks = [f"F{dims['sink_fpga_index']}"]
    hops = maximum_capacity_payload_hops(
        routes,
        source=f"F{dims['source_fpga_index']}",
        sinks=sinks,
        minimum_signal_count=routed_payload_signals,
    )
    if hops is None:
        raise ValidationError("latency observation lacks a full-width payload path")
    delay = item["metrics"]["timing"].get("sr0_worst_cross_fpga_delay_ns")
    if not isinstance(delay, (int, float)) or delay < 0:
        raise ValidationError("latency observation lacks a non-negative sr0 delay")
    return (
        [
            1.0,
            float(hops),
            float(max(0, dims["flow_count"] - 1)),
            float(max(0, dims["fanout"] - 1)),
        ],
        ratio,
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
    bootstrap_samples: int = 128,
) -> Dict[str, Any]:
    normalized = _validated(observations)
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
    if not holdout_items:
        raise ValidationError("latency fitting requires independent holdout samples")

    fit_rows = [_latency_row(item) for item in fit_items]
    holdout_rows = [_latency_row(item) for item in holdout_items]
    tdm_ratios = sorted({ratio for _, ratio, _ in fit_rows if ratio > 1})
    fit_ratio_set = {ratio for _, ratio, _ in fit_rows}
    holdout_ratio_set = {ratio for _, ratio, _ in holdout_rows}
    missing_holdouts = sorted(fit_ratio_set - holdout_ratio_set)
    if missing_holdouts:
        raise ValidationError(
            "latency fitting lacks independent holdout coverage for TDM ratios "
            + ", ".join(map(str, missing_holdouts))
        )
    if any(ratio not in fit_ratio_set for ratio in holdout_ratio_set):
        raise ValidationError("latency holdout contains an unfitted TDM ratio")

    feature_names = list(_LATENCY_BASE_FEATURES) + [
        f"tdm_ratio_{ratio}_ns" for ratio in tdm_ratios
    ]

    def expand(row: list[float], ratio: int) -> list[float]:
        return row + [1.0 if ratio == candidate else 0.0 for candidate in tdm_ratios]

    features = [expand(row, ratio) for row, ratio, _ in fit_rows]
    targets = [target for _, _, target in fit_rows]
    if len(fit_items) < len(feature_names) + 1:
        raise ValidationError("latency fitting requires more fit samples than coefficients")
    if _matrix_rank(features) != len(feature_names):
        raise ValidationError("latency fit matrix does not identify every model coefficient")
    coefficients = _nnls(features, targets)
    residuals = [
        target - sum(value * coefficient for value, coefficient in zip(row, coefficients))
        for row, target in zip(features, targets)
    ]
    rmse = math.sqrt(sum(value * value for value in residuals) / len(residuals))

    if bootstrap_samples < 16:
        raise ValidationError("latency bootstrap_samples must be >= 16")
    generator = random.Random(0)
    boot = [[] for _ in coefficients]
    predictions = [
        sum(value * coefficient for value, coefficient in zip(row, coefficients))
        for row in features
    ]
    for _ in range(bootstrap_samples):
        # Preserve the complete controlled design matrix in every replicate.
        # Row-pair resampling can omit a rare categorical TDM state entirely
        # and manufacture a zero lower bound even though that state is
        # independently identifiable.  Residual bootstrap varies the measured
        # response while retaining every calibrated state.
        sampled_targets = [
            prediction + residuals[generator.randrange(len(residuals))]
            for prediction in predictions
        ]
        sampled = _nnls(features, sampled_targets)
        for column, value in enumerate(sampled):
            boot[column].append(value)
    parameters = {}
    for index, name in enumerate(_LATENCY_BASE_FEATURES):
        parameters[name] = {
            "nominal": coefficients[index],
            "aggressive": _percentile(boot[index], 0.05),
            "conservative": _percentile(boot[index], 0.95),
            "identifiable": True,
        }
    tdm_ratio_delay_ns = {}
    for offset, ratio in enumerate(tdm_ratios, start=len(_LATENCY_BASE_FEATURES)):
        tdm_ratio_delay_ns[str(ratio)] = {
            "nominal": coefficients[offset],
            "aggressive": _percentile(boot[offset], 0.05),
            "conservative": _percentile(boot[offset], 0.95),
            "identifiable": True,
        }

    holdout_checks = []
    for item, (base_row, ratio, actual) in zip(holdout_items, holdout_rows):
        row = expand(base_row, ratio)
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
        "parameters": parameters,
        "tdm_ratio_delay_ns": tdm_ratio_delay_ns,
        "observed_tdm_ratios": sorted(fit_ratio_set),
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
    groups: dict[tuple[str, str], list[Dict[str, Any]]] = defaultdict(list)
    excluded = 0
    for item in normalized:
        if item["experiment"]["kind"] != "transport_cost":
            raise ValidationError("transport fitting received a non-transport observation")
        if item["execution"]["outcome"] != "pass":
            excluded += 1
            continue
        _communication_dimensions(item)
        token = _integer(item["metrics"]["design"], "pairing_token", "transport observation")
        groups[(str(token), item["identity"]["role"])].append(item)

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
        if local_item["workload"]["rtl_sha256"] != cross_item["workload"]["rtl_sha256"]:
            raise ValidationError("transport local/cross pair does not use identical RTL")
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
        (fit_rows if key[1] == "fit" else holdout_rows).append(row)

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
    if not holdout_rows:
        raise ValidationError("transport fitting requires independent paired holdout samples")
    if bootstrap_samples < 16:
        raise ValidationError("transport bootstrap_samples must be >= 16")
    fitted_resources = {}
    generator = random.Random(0)
    features = [row["features"] for row in fit_rows]
    if _matrix_rank(features) != len(feature_names):
        raise ValidationError("transport fit matrix does not identify every model coefficient")
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
                    "identifiable": True,
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
                "absolute_error": abs(predicted - actual),
                "relative_error": abs(predicted - actual) / actual if actual else None,
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
