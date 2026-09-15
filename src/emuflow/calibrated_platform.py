"""Behaviorally calibrated academic multi-FPGA platform models.

The model deliberately captures only externally observable resource, link,
TDM, transport, and timing behavior.  It is not a hardware clone and does not
encode a reference tool's partitioner, router, reports, file paths, or internal
data structures.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .errors import ValidationError
from .io import read_json, write_json
from .platform import Platform
from .resources import RESOURCE_FIELDS


TEMPLATE_SCHEMA = "emuflow.calibrated-platform-template/v1"
OBSERVATIONS_SCHEMA = "emuflow.platform-calibration-observations/v1"
MODEL_SCHEMA = "emuflow.calibrated-academic-platform/v1"
VALIDATION_SCHEMA = "emuflow.calibrated-academic-platform-validation/v1"

_ROLES = {"fit", "holdout"}
_SOURCE_CLASSES = {"authorized_reference_flow", "synthetic_fixture"}
_PUBLICATION_SCOPES = {"internal", "aggregate_only", "public"}
_OUTCOMES = {"pass", "capacity_fail"}
_PROFILES = ("conservative", "nominal", "aggressive")
_PUBLICATION_RANK = {"internal": 0, "aggregate_only": 1, "public": 2}


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{context}: expected an object")
    return value


def _array(value: Any, context: str, *, nonempty: bool = False) -> List[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = "non-empty " if nonempty else ""
        raise ValidationError(f"{context}: expected a {qualifier}array")
    return value


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{context}: expected a non-empty string")
    return value


def _number(
    value: Any,
    context: str,
    *,
    minimum: float = 0.0,
    exclusive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{context}: expected a number")
    result = float(value)
    invalid = result <= minimum if exclusive else result < minimum
    if not math.isfinite(result) or invalid:
        relation = ">" if exclusive else ">="
        raise ValidationError(f"{context}: expected a finite value {relation} {minimum}")
    return result


def _integer(value: Any, context: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValidationError(f"{context}: expected an integer >= {minimum}")
    return value


def _reject_unknown(
    value: Mapping[str, Any], allowed: Iterable[str], context: str
) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ValidationError(f"{context}: unknown fields {unknown}")


def _unique_ids(items: Sequence[Mapping[str, Any]], context: str) -> None:
    seen = set()
    for index, item in enumerate(items):
        identifier = _string(item.get("id"), f"{context}[{index}].id")
        if identifier in seen:
            raise ValidationError(f"{context}[{index}].id: duplicate {identifier!r}")
        seen.add(identifier)


def _matrix_rank(rows: Sequence[Sequence[float]], tolerance: float = 1e-10) -> int:
    matrix = [list(map(float, row)) for row in rows]
    if not matrix:
        return 0
    row_count = len(matrix)
    column_count = len(matrix[0])
    rank = 0
    for column in range(column_count):
        pivot = max(range(rank, row_count), key=lambda row: abs(matrix[row][column]))
        if abs(matrix[pivot][column]) <= tolerance:
            continue
        matrix[rank], matrix[pivot] = matrix[pivot], matrix[rank]
        scale = matrix[rank][column]
        matrix[rank] = [value / scale for value in matrix[rank]]
        for row in range(row_count):
            if row == rank:
                continue
            factor = matrix[row][column]
            if abs(factor) <= tolerance:
                continue
            matrix[row] = [
                current - factor * pivot_value
                for current, pivot_value in zip(matrix[row], matrix[rank])
            ]
        rank += 1
        if rank == row_count:
            break
    return rank


def _nnls_coordinate_descent(
    rows: Sequence[Sequence[float]],
    values: Sequence[float],
    *,
    iterations: int = 20_000,
    tolerance: float = 1e-12,
) -> List[float]:
    """Solve a small non-negative least-squares problem without dependencies."""

    if not rows or len(rows) != len(values):
        raise ValidationError("delay fit: observations and values must align")
    columns = len(rows[0])
    if columns == 0 or any(len(row) != columns for row in rows):
        raise ValidationError("delay fit: inconsistent feature matrix")
    coefficients = [0.0] * columns
    predictions = [0.0] * len(rows)
    for _ in range(iterations):
        maximum_change = 0.0
        for column in range(columns):
            denominator = sum(row[column] ** 2 for row in rows)
            if denominator <= tolerance:
                continue
            numerator = 0.0
            for index, row in enumerate(rows):
                without_column = (
                    predictions[index] - row[column] * coefficients[column]
                )
                numerator += row[column] * (values[index] - without_column)
            updated = max(0.0, numerator / denominator)
            delta = updated - coefficients[column]
            if delta:
                for index, row in enumerate(rows):
                    predictions[index] += row[column] * delta
            maximum_change = max(maximum_change, abs(delta))
            coefficients[column] = updated
        if maximum_change <= tolerance:
            break
    return coefficients


def validate_calibration_template(value: Mapping[str, Any]) -> Dict[str, Any]:
    root = _mapping(value, "template")
    _reject_unknown(
        root,
        {"schema", "model", "device", "link", "configurations", "acceptance"},
        "template",
    )
    if root.get("schema") != TEMPLATE_SCHEMA:
        raise ValidationError(f"template.schema: expected {TEMPLATE_SCHEMA!r}")

    model = _mapping(root.get("model"), "template.model")
    _reject_unknown(
        model,
        {
            "name",
            "description",
            "reference_alias",
            "source_class",
            "authorization_id",
            "publication_scope",
        },
        "template.model",
    )
    name = _string(model.get("name"), "template.model.name")
    reference_alias = _string(
        model.get("reference_alias"), "template.model.reference_alias"
    )
    source_class = _string(model.get("source_class"), "template.model.source_class")
    if source_class not in _SOURCE_CLASSES:
        raise ValidationError(
            f"template.model.source_class: expected one of {sorted(_SOURCE_CLASSES)}"
        )
    authorization_id = _string(
        model.get("authorization_id"), "template.model.authorization_id"
    )
    publication_scope = _string(
        model.get("publication_scope"), "template.model.publication_scope"
    )
    if publication_scope not in _PUBLICATION_SCOPES:
        raise ValidationError(
            "template.model.publication_scope: expected one of "
            f"{sorted(_PUBLICATION_SCOPES)}"
        )

    device = _mapping(root.get("device"), "template.device")
    _reject_unknown(device, {"part", "utilization_limit"}, "template.device")
    part = _string(device.get("part"), "template.device.part")
    utilization_limit = _number(
        device.get("utilization_limit"),
        "template.device.utilization_limit",
        exclusive=True,
    )
    if utilization_limit > 1.0:
        raise ValidationError("template.device.utilization_limit: expected <= 1")

    link = _mapping(root.get("link"), "template.link")
    _reject_unknown(
        link,
        {"direction", "capacity_sharing", "fabric_clock_mhz"},
        "template.link",
    )
    direction = _string(link.get("direction"), "template.link.direction")
    if direction not in {"full_duplex", "half_duplex", "unidirectional"}:
        raise ValidationError("template.link.direction: unsupported direction")
    capacity_sharing = _string(
        link.get("capacity_sharing"), "template.link.capacity_sharing"
    )
    if capacity_sharing not in {"per_direction", "shared_bidirectional"}:
        raise ValidationError("template.link.capacity_sharing: unsupported policy")
    if capacity_sharing == "shared_bidirectional" and direction != "full_duplex":
        raise ValidationError(
            "template.link.capacity_sharing: shared_bidirectional requires full_duplex"
        )
    fabric_clock_mhz = _number(
        link.get("fabric_clock_mhz"),
        "template.link.fabric_clock_mhz",
        exclusive=True,
    )

    raw_configurations = _array(
        root.get("configurations"), "template.configurations", nonempty=True
    )
    configurations: List[Dict[str, Any]] = []
    for index, raw in enumerate(raw_configurations):
        item = _mapping(raw, f"template.configurations[{index}]")
        _reject_unknown(item, {"id", "fpgas", "links"}, f"configurations[{index}]")
        config_id = _string(item.get("id"), f"configurations[{index}].id")
        fpgas = [
            _string(fpga, f"configurations[{index}].fpgas[{fpga_index}]")
            for fpga_index, fpga in enumerate(
                _array(item.get("fpgas"), f"configurations[{index}].fpgas", nonempty=True)
            )
        ]
        if len(fpgas) != len(set(fpgas)):
            raise ValidationError(f"configurations[{index}].fpgas: duplicate IDs")
        raw_links = _array(item.get("links"), f"configurations[{index}].links")
        links: List[Dict[str, Any]] = []
        endpoint_pairs = set()
        for link_index, raw_link in enumerate(raw_links):
            link_item = _mapping(
                raw_link, f"configurations[{index}].links[{link_index}]"
            )
            _reject_unknown(
                link_item,
                {"id", "endpoints"},
                f"configurations[{index}].links[{link_index}]",
            )
            link_id = _string(
                link_item.get("id"),
                f"configurations[{index}].links[{link_index}].id",
            )
            endpoints = _array(
                link_item.get("endpoints"),
                f"configurations[{index}].links[{link_index}].endpoints",
            )
            if len(endpoints) != 2:
                raise ValidationError(
                    f"configurations[{index}].links[{link_index}].endpoints: "
                    "expected exactly two FPGA IDs"
                )
            endpoints = [
                _string(endpoint, "configuration link endpoint")
                for endpoint in endpoints
            ]
            if endpoints[0] == endpoints[1] or not set(endpoints) <= set(fpgas):
                raise ValidationError(
                    f"configurations[{index}].links[{link_index}].endpoints: "
                    "must name two distinct configuration FPGAs"
                )
            pair = tuple(sorted(endpoints))
            if pair in endpoint_pairs:
                raise ValidationError(
                    f"configurations[{index}].links[{link_index}]: duplicate FPGA pair"
                )
            endpoint_pairs.add(pair)
            links.append({"id": link_id, "endpoints": endpoints})
        _unique_ids(links, f"configurations[{index}].links")
        if len(fpgas) > 1:
            adjacency = {fpga: set() for fpga in fpgas}
            for topology_link in links:
                source, sink = topology_link["endpoints"]
                adjacency[source].add(sink)
                adjacency[sink].add(source)
            reached = {fpgas[0]}
            pending = [fpgas[0]]
            while pending:
                current = pending.pop()
                for neighbor in adjacency[current] - reached:
                    reached.add(neighbor)
                    pending.append(neighbor)
            if reached != set(fpgas):
                raise ValidationError(
                    f"configurations[{index}]: topology must connect every FPGA"
                )
        configurations.append({"id": config_id, "fpgas": fpgas, "links": links})
    _unique_ids(configurations, "template.configurations")

    acceptance = _mapping(root.get("acceptance"), "template.acceptance")
    _reject_unknown(
        acceptance,
        {
            "capacity_outcome_accuracy_min",
            "link_outcome_accuracy_min",
            "delay_mean_relative_error_max",
            "delay_max_relative_error_max",
        },
        "template.acceptance",
    )
    normalized_acceptance = {
        "capacity_outcome_accuracy_min": _number(
            acceptance.get("capacity_outcome_accuracy_min"),
            "template.acceptance.capacity_outcome_accuracy_min",
        ),
        "link_outcome_accuracy_min": _number(
            acceptance.get("link_outcome_accuracy_min"),
            "template.acceptance.link_outcome_accuracy_min",
        ),
        "delay_mean_relative_error_max": _number(
            acceptance.get("delay_mean_relative_error_max"),
            "template.acceptance.delay_mean_relative_error_max",
        ),
        "delay_max_relative_error_max": _number(
            acceptance.get("delay_max_relative_error_max"),
            "template.acceptance.delay_max_relative_error_max",
        ),
    }
    if any(
        normalized_acceptance[key] > 1.0
        for key in (
            "capacity_outcome_accuracy_min",
            "link_outcome_accuracy_min",
        )
    ):
        raise ValidationError("template.acceptance accuracy thresholds must be <= 1")

    return {
        "schema": TEMPLATE_SCHEMA,
        "model": {
            "name": name,
            "description": str(model.get("description", "")),
            "reference_alias": reference_alias,
            "source_class": source_class,
            "authorization_id": authorization_id,
            "publication_scope": publication_scope,
        },
        "device": {"part": part, "utilization_limit": utilization_limit},
        "link": {
            "direction": direction,
            "capacity_sharing": capacity_sharing,
            "fabric_clock_mhz": fabric_clock_mhz,
        },
        "configurations": configurations,
        "acceptance": normalized_acceptance,
    }


def validate_calibration_observations(
    value: Mapping[str, Any], *, expected_role: Optional[str] = None
) -> Dict[str, Any]:
    root = _mapping(value, "observations")
    _reject_unknown(
        root,
        {
            "schema",
            "dataset",
            "capacity_boundaries",
            "link_capacity_boundaries",
            "link_delay_measurements",
        },
        "observations",
    )
    if root.get("schema") != OBSERVATIONS_SCHEMA:
        raise ValidationError(f"observations.schema: expected {OBSERVATIONS_SCHEMA!r}")
    dataset = _mapping(root.get("dataset"), "observations.dataset")
    _reject_unknown(
        dataset,
        {
            "id",
            "role",
            "reference_alias",
            "source_class",
            "authorization_id",
            "publication_scope",
        },
        "observations.dataset",
    )
    role = _string(dataset.get("role"), "observations.dataset.role")
    if role not in _ROLES or (expected_role is not None and role != expected_role):
        expected = expected_role or f"one of {sorted(_ROLES)}"
        raise ValidationError(f"observations.dataset.role: expected {expected}")
    source_class = _string(
        dataset.get("source_class"), "observations.dataset.source_class"
    )
    if source_class not in _SOURCE_CLASSES:
        raise ValidationError("observations.dataset.source_class: unsupported class")
    publication_scope = _string(
        dataset.get("publication_scope"), "observations.dataset.publication_scope"
    )
    if publication_scope not in _PUBLICATION_SCOPES:
        raise ValidationError("observations.dataset.publication_scope: unsupported scope")
    normalized_dataset = {
        "id": _string(dataset.get("id"), "observations.dataset.id"),
        "role": role,
        "reference_alias": _string(
            dataset.get("reference_alias"), "observations.dataset.reference_alias"
        ),
        "source_class": source_class,
        "authorization_id": _string(
            dataset.get("authorization_id"), "observations.dataset.authorization_id"
        ),
        "publication_scope": publication_scope,
    }

    capacity_boundaries = []
    for index, raw in enumerate(_array(root.get("capacity_boundaries", []), "capacity_boundaries")):
        item = _mapping(raw, f"capacity_boundaries[{index}]")
        _reject_unknown(
            item,
            {"id", "configuration", "resource", "demand_per_fpga", "outcome", "assignment_control"},
            f"capacity_boundaries[{index}]",
        )
        resource = _string(item.get("resource"), f"capacity_boundaries[{index}].resource")
        if resource not in RESOURCE_FIELDS:
            raise ValidationError(f"capacity_boundaries[{index}].resource: unsupported resource")
        assignment_control = _string(
            item.get("assignment_control"),
            f"capacity_boundaries[{index}].assignment_control",
        )
        if assignment_control != "fixed":
            raise ValidationError(
                f"capacity_boundaries[{index}].assignment_control: hardware fitting requires fixed"
            )
        outcome = _string(item.get("outcome"), f"capacity_boundaries[{index}].outcome")
        if outcome not in _OUTCOMES:
            raise ValidationError(f"capacity_boundaries[{index}].outcome: unsupported outcome")
        capacity_boundaries.append(
            {
                "id": _string(item.get("id"), f"capacity_boundaries[{index}].id"),
                "configuration": _string(
                    item.get("configuration"), f"capacity_boundaries[{index}].configuration"
                ),
                "resource": resource,
                "demand_per_fpga": _integer(
                    item.get("demand_per_fpga"),
                    f"capacity_boundaries[{index}].demand_per_fpga",
                    minimum=1,
                ),
                "outcome": outcome,
                "assignment_control": "fixed",
            }
        )

    link_capacity_boundaries = []
    for index, raw in enumerate(
        _array(root.get("link_capacity_boundaries", []), "link_capacity_boundaries")
    ):
        item = _mapping(raw, f"link_capacity_boundaries[{index}]")
        _reject_unknown(
            item,
            {"id", "configuration", "hop_count", "offered_bits_per_cycle", "outcome", "assignment_control", "route_control"},
            f"link_capacity_boundaries[{index}]",
        )
        if item.get("assignment_control") != "fixed" or item.get("route_control") != "fixed":
            raise ValidationError(
                f"link_capacity_boundaries[{index}]: hardware fitting requires fixed assignment and route"
            )
        if item.get("hop_count") != 1:
            raise ValidationError(
                f"link_capacity_boundaries[{index}].hop_count: "
                "link-class capacity fitting requires a controlled single-hop route"
            )
        outcome = _string(item.get("outcome"), f"link_capacity_boundaries[{index}].outcome")
        if outcome not in _OUTCOMES:
            raise ValidationError(f"link_capacity_boundaries[{index}].outcome: unsupported outcome")
        link_capacity_boundaries.append(
            {
                "id": _string(item.get("id"), f"link_capacity_boundaries[{index}].id"),
                "configuration": _string(
                    item.get("configuration"),
                    f"link_capacity_boundaries[{index}].configuration",
                ),
                "offered_bits_per_cycle": _integer(
                    item.get("offered_bits_per_cycle"),
                    f"link_capacity_boundaries[{index}].offered_bits_per_cycle",
                    minimum=1,
                ),
                "hop_count": 1,
                "outcome": outcome,
                "assignment_control": "fixed",
                "route_control": "fixed",
            }
        )

    delay_measurements = []
    for index, raw in enumerate(
        _array(root.get("link_delay_measurements", []), "link_delay_measurements")
    ):
        item = _mapping(raw, f"link_delay_measurements[{index}]")
        _reject_unknown(
            item,
            {
                "id",
                "configuration",
                "hop_count",
                "payload_bits",
                "tdm_wait_slots",
                "contention_units",
                "observed_delay_ns",
                "assignment_control",
                "route_control",
            },
            f"link_delay_measurements[{index}]",
        )
        if item.get("assignment_control") != "fixed" or item.get("route_control") != "fixed":
            raise ValidationError(
                f"link_delay_measurements[{index}]: hardware fitting requires fixed assignment and route"
            )
        delay_measurements.append(
            {
                "id": _string(item.get("id"), f"link_delay_measurements[{index}].id"),
                "configuration": _string(
                    item.get("configuration"),
                    f"link_delay_measurements[{index}].configuration",
                ),
                "hop_count": _integer(
                    item.get("hop_count"), f"link_delay_measurements[{index}].hop_count", minimum=1
                ),
                "payload_bits": _integer(
                    item.get("payload_bits"), f"link_delay_measurements[{index}].payload_bits", minimum=1
                ),
                "tdm_wait_slots": _integer(
                    item.get("tdm_wait_slots"),
                    f"link_delay_measurements[{index}].tdm_wait_slots",
                ),
                "contention_units": _number(
                    item.get("contention_units"),
                    f"link_delay_measurements[{index}].contention_units",
                ),
                "observed_delay_ns": _number(
                    item.get("observed_delay_ns"),
                    f"link_delay_measurements[{index}].observed_delay_ns",
                    exclusive=True,
                ),
                "assignment_control": "fixed",
                "route_control": "fixed",
            }
        )

    all_items = capacity_boundaries + link_capacity_boundaries + delay_measurements
    _unique_ids(all_items, "observations")
    if not all_items:
        raise ValidationError("observations: expected at least one measurement")
    return {
        "schema": OBSERVATIONS_SCHEMA,
        "dataset": normalized_dataset,
        "capacity_boundaries": capacity_boundaries,
        "link_capacity_boundaries": link_capacity_boundaries,
        "link_delay_measurements": delay_measurements,
    }


def _boundary_interval(
    observations: Sequence[Mapping[str, Any]], value_key: str, context: str
) -> Tuple[int, int]:
    passed = [int(item[value_key]) for item in observations if item["outcome"] == "pass"]
    failed = [
        int(item[value_key])
        for item in observations
        if item["outcome"] == "capacity_fail"
    ]
    if not passed or not failed:
        raise ValidationError(f"{context}: requires both pass and capacity_fail observations")
    lower = max(passed)
    upper = min(failed)
    if lower >= upper:
        raise ValidationError(
            f"{context}: inconsistent boundary; pass {lower} is not below fail {upper}"
        )
    return lower, upper


def _profile_values(lower: int, upper_exclusive: int) -> Dict[str, int]:
    return {
        "conservative": lower,
        "nominal": (lower + upper_exclusive - 1) // 2,
        "aggressive": upper_exclusive - 1,
    }


def _delay_features(
    item: Mapping[str, Any], payload_capacity: int, slot_ns: float
) -> Tuple[List[float], float]:
    serialization_cycles = max(
        0, math.ceil(int(item["payload_bits"]) / payload_capacity) - 1
    )
    fixed_slot_delay = (
        serialization_cycles + int(item["tdm_wait_slots"])
    ) * slot_ns
    features = [1.0, float(item["hop_count"]), float(item["contention_units"])]
    adjusted = float(item["observed_delay_ns"]) - fixed_slot_delay
    if adjusted < 0.0:
        raise ValidationError(
            f"link_delay_measurements {item['id']!r}: observed delay is below "
            "the declared serialization/TDM wait"
        )
    return features, adjusted


def fit_calibrated_platform(
    template_value: Mapping[str, Any], observations_value: Mapping[str, Any]
) -> Dict[str, Any]:
    template = validate_calibration_template(template_value)
    observations = validate_calibration_observations(
        observations_value, expected_role="fit"
    )
    for field in ("reference_alias", "source_class", "authorization_id"):
        if template["model"][field] != observations["dataset"][field]:
            raise ValidationError(f"fit dataset {field} does not match template")
    if (
        _PUBLICATION_RANK[template["model"]["publication_scope"]]
        > _PUBLICATION_RANK[observations["dataset"]["publication_scope"]]
    ):
        raise ValidationError(
            "fit dataset publication_scope does not authorize the model publication_scope"
        )
    configuration_ids = {item["id"] for item in template["configurations"]}
    for category in (
        "capacity_boundaries",
        "link_capacity_boundaries",
        "link_delay_measurements",
    ):
        for item in observations[category]:
            if item["configuration"] not in configuration_ids:
                raise ValidationError(
                    f"{category} {item['id']!r}: unknown configuration "
                    f"{item['configuration']!r}"
                )

    resource_intervals: Dict[str, Dict[str, int]] = {}
    resource_profiles: Dict[str, Dict[str, int]] = {profile: {} for profile in _PROFILES}
    resources = sorted({item["resource"] for item in observations["capacity_boundaries"]})
    if not resources:
        raise ValidationError("fit dataset: capacity boundaries are required")
    utilization_limit = float(template["device"]["utilization_limit"])
    for resource in resources:
        relevant = [
            item for item in observations["capacity_boundaries"] if item["resource"] == resource
        ]
        lower, upper = _boundary_interval(relevant, "demand_per_fpga", f"resource {resource}")
        resource_intervals[resource] = {
            "effective_lower": lower,
            "effective_upper_exclusive": upper,
        }
        for profile, effective in _profile_values(lower, upper).items():
            resource_profiles[profile][resource] = math.ceil(effective / utilization_limit)

    link_lower, link_upper = _boundary_interval(
        observations["link_capacity_boundaries"],
        "offered_bits_per_cycle",
        "link payload capacity",
    )
    link_profile_values = _profile_values(link_lower, link_upper)
    nominal_payload = link_profile_values["nominal"]
    clock_mhz = float(template["link"]["fabric_clock_mhz"])
    slot_ns = 1000.0 / clock_mhz
    delay_measurements = observations["link_delay_measurements"]
    if len(delay_measurements) < 3:
        raise ValidationError("delay fit requires at least three controlled measurements")
    feature_rows: List[List[float]] = []
    adjusted_values: List[float] = []
    for item in delay_measurements:
        features, adjusted = _delay_features(item, nominal_payload, slot_ns)
        feature_rows.append(features)
        adjusted_values.append(adjusted)
    if _matrix_rank(feature_rows) < 3:
        raise ValidationError(
            "delay fit is not identifiable: vary hop count and contention independently"
        )
    coefficients = _nnls_coordinate_descent(feature_rows, adjusted_values)
    predicted_adjusted = [
        sum(coefficient * feature for coefficient, feature in zip(coefficients, row))
        for row in feature_rows
    ]
    residuals = [
        observed - predicted
        for observed, predicted in zip(adjusted_values, predicted_adjusted)
    ]
    maximum_absolute_residual = max(abs(value) for value in residuals)
    mean_absolute_residual = sum(abs(value) for value in residuals) / len(residuals)
    endpoint_ns, per_hop_ns, contention_ns = coefficients

    profiles = {}
    for profile in _PROFILES:
        if profile == "conservative":
            base_margin = maximum_absolute_residual
        elif profile == "aggressive":
            base_margin = -maximum_absolute_residual
        else:
            base_margin = 0.0
        base_one_hop_ns = max(0.0, endpoint_ns + per_hop_ns + base_margin)
        profiles[profile] = {
            "device_capacity": dict(sorted(resource_profiles[profile].items())),
            "link_payload_bits_per_cycle_per_direction": link_profile_values[profile],
            "base_one_hop_latency_ns": base_one_hop_ns,
            "base_one_hop_latency_cycles": math.ceil(base_one_hop_ns / slot_ns),
        }

    fit_ids = sorted(
        item["id"]
        for category in (
            "capacity_boundaries",
            "link_capacity_boundaries",
            "link_delay_measurements",
        )
        for item in observations[category]
    )
    model = {
        "schema": MODEL_SCHEMA,
        "model": {
            **template["model"],
            "qualification": "behaviorally_calibrated_academic_model",
            "not_a_hardware_clone": True,
        },
        "device": template["device"],
        "link": template["link"],
        "configurations": template["configurations"],
        "profiles": profiles,
        "calibration": {
            "fit_dataset_id": observations["dataset"]["id"],
            "fit_observation_ids": fit_ids,
            "resource_effective_capacity_intervals": resource_intervals,
            "link_payload_capacity_interval": {
                "lower": link_lower,
                "upper_exclusive": link_upper,
            },
            "link_delay_model": {
                "equation": (
                    "endpoint_ns + hop_count * per_hop_ns + "
                    "(serialization_cycles + tdm_wait_slots) * slot_ns + "
                    "contention_units * contention_ns"
                ),
                "endpoint_ns": endpoint_ns,
                "per_hop_ns": per_hop_ns,
                "contention_ns": contention_ns,
                "slot_ns": slot_ns,
                "fit_mean_absolute_residual_ns": mean_absolute_residual,
                "fit_max_absolute_residual_ns": maximum_absolute_residual,
            },
            "parameter_provenance": {
                "device_capacity": "controlled fixed-assignment pass/fail boundaries",
                "link_capacity": "controlled fixed-route pass/fail boundaries",
                "link_delay": "non-negative fit over controlled fixed-assignment/fixed-route measurements",
            },
            "holdout_status": "pending",
        },
        "acceptance": template["acceptance"],
    }
    validate_calibrated_platform_model(model)
    return model


def validate_calibrated_platform_model(value: Mapping[str, Any]) -> Dict[str, Any]:
    root = _mapping(value, "calibrated model")
    if root.get("schema") != MODEL_SCHEMA:
        raise ValidationError(f"calibrated model.schema: expected {MODEL_SCHEMA!r}")
    required = {
        "schema",
        "model",
        "device",
        "link",
        "configurations",
        "profiles",
        "calibration",
        "acceptance",
    }
    missing = sorted(required - set(root))
    if missing:
        raise ValidationError(f"calibrated model: missing fields {missing}")
    if root["model"].get("qualification") != "behaviorally_calibrated_academic_model":
        raise ValidationError("calibrated model: missing academic-model qualification")
    if root["model"].get("not_a_hardware_clone") is not True:
        raise ValidationError("calibrated model: must explicitly state it is not a hardware clone")
    configurations = _array(
        root["configurations"], "calibrated model.configurations", nonempty=True
    )
    _unique_ids(configurations, "calibrated model.configurations")
    profiles = _mapping(root["profiles"], "calibrated model.profiles")
    if set(profiles) != set(_PROFILES):
        raise ValidationError(f"calibrated model.profiles: expected {list(_PROFILES)}")
    for profile in _PROFILES:
        for configuration in configurations:
            materialize_calibrated_boarddb(root, configuration["id"], profile)
    return dict(root)


def materialize_calibrated_boarddb(
    model_value: Mapping[str, Any], configuration_id: str, profile: str = "nominal"
) -> Dict[str, Any]:
    root = _mapping(model_value, "calibrated model")
    if root.get("schema") != MODEL_SCHEMA:
        raise ValidationError(f"calibrated model.schema: expected {MODEL_SCHEMA!r}")
    if profile not in _PROFILES:
        raise ValidationError(f"profile: expected one of {list(_PROFILES)}")
    configurations = _array(root.get("configurations"), "calibrated model.configurations", nonempty=True)
    configuration = next(
        (item for item in configurations if item.get("id") == configuration_id), None
    )
    if configuration is None:
        raise ValidationError(
            f"configuration: {configuration_id!r} is not an explicitly supported platform"
        )
    profile_value = _mapping(root["profiles"][profile], f"profiles.{profile}")
    device = _mapping(root["device"], "calibrated model.device")
    link = _mapping(root["link"], "calibrated model.link")
    model = _mapping(root["model"], "calibrated model.model")
    capacity = {
        resource: _integer(count, f"profiles.{profile}.device_capacity.{resource}")
        for resource, count in _mapping(
            profile_value.get("device_capacity"), f"profiles.{profile}.device_capacity"
        ).items()
    }
    payload_capacity = _integer(
        profile_value.get("link_payload_bits_per_cycle_per_direction"),
        f"profiles.{profile}.link_payload_bits_per_cycle_per_direction",
        minimum=1,
    )
    latency_cycles = _integer(
        profile_value.get("base_one_hop_latency_cycles"),
        f"profiles.{profile}.base_one_hop_latency_cycles",
    )
    boarddb = {
        "schema": "emuflow.boarddb/v1",
        "platform": {
            "name": f"{model['name']}__{configuration_id}__{profile}",
            "kind": "virtual",
            "description": (
                "Behaviorally calibrated academic platform; not a physical hardware clone"
            ),
        },
        "fpgas": [
            {
                "id": fpga_id,
                "part": device["part"],
                "utilization_limit": device["utilization_limit"],
                "capacity": capacity,
            }
            for fpga_id in configuration["fpgas"]
        ],
        "links": [
            {
                "id": item["id"],
                "endpoints": item["endpoints"],
                "direction": link["direction"],
                "mode": "abstract",
                "data_lanes_per_direction": payload_capacity,
                "fabric_clock_mhz": link["fabric_clock_mhz"],
                "latency_cycles": latency_cycles,
                "capacity_sharing": link["capacity_sharing"],
            }
            for item in configuration["links"]
        ],
    }
    return Platform.from_dict(boarddb).to_dict()


def _predict_delay(model: Mapping[str, Any], item: Mapping[str, Any]) -> float:
    calibration = model["calibration"]
    delay_model = calibration["link_delay_model"]
    payload_capacity = model["profiles"]["nominal"][
        "link_payload_bits_per_cycle_per_direction"
    ]
    serialization_cycles = max(
        0, math.ceil(item["payload_bits"] / payload_capacity) - 1
    )
    return (
        delay_model["endpoint_ns"]
        + item["hop_count"] * delay_model["per_hop_ns"]
        + (serialization_cycles + item["tdm_wait_slots"]) * delay_model["slot_ns"]
        + item["contention_units"] * delay_model["contention_ns"]
    )


def validate_calibrated_platform_holdout(
    model_value: Mapping[str, Any], observations_value: Mapping[str, Any]
) -> Dict[str, Any]:
    model = validate_calibrated_platform_model(model_value)
    observations = validate_calibration_observations(
        observations_value, expected_role="holdout"
    )
    for field in ("reference_alias", "source_class", "authorization_id"):
        if model["model"][field] != observations["dataset"][field]:
            raise ValidationError(f"holdout dataset {field} does not match model")
    if (
        _PUBLICATION_RANK[model["model"]["publication_scope"]]
        > _PUBLICATION_RANK[observations["dataset"]["publication_scope"]]
    ):
        raise ValidationError(
            "holdout dataset publication_scope does not authorize model validation publication"
        )
    configuration_ids = {item["id"] for item in model["configurations"]}
    for category in (
        "capacity_boundaries",
        "link_capacity_boundaries",
        "link_delay_measurements",
    ):
        for item in observations[category]:
            if item["configuration"] not in configuration_ids:
                raise ValidationError(
                    f"holdout {category} {item['id']!r}: unknown configuration"
                )
    fit_ids = set(model["calibration"]["fit_observation_ids"])
    holdout_ids = {
        item["id"]
        for category in (
            "capacity_boundaries",
            "link_capacity_boundaries",
            "link_delay_measurements",
        )
        for item in observations[category]
    }
    overlap = sorted(fit_ids & holdout_ids)
    if overlap:
        raise ValidationError(f"holdout observations overlap fit observations: {overlap}")

    nominal = model["profiles"]["nominal"]
    utilization = model["device"]["utilization_limit"]
    capacity_checks = []
    for item in observations["capacity_boundaries"]:
        raw_capacity = nominal["device_capacity"].get(item["resource"])
        if raw_capacity is None:
            raise ValidationError(
                f"holdout resource {item['resource']!r} was not identified during fitting"
            )
        effective_capacity = math.floor(raw_capacity * utilization)
        predicted = "pass" if item["demand_per_fpga"] <= effective_capacity else "capacity_fail"
        capacity_checks.append({"id": item["id"], "predicted": predicted, "observed": item["outcome"], "match": predicted == item["outcome"]})
    link_checks = []
    link_capacity = nominal["link_payload_bits_per_cycle_per_direction"]
    for item in observations["link_capacity_boundaries"]:
        predicted = "pass" if item["offered_bits_per_cycle"] <= link_capacity else "capacity_fail"
        link_checks.append({"id": item["id"], "predicted": predicted, "observed": item["outcome"], "match": predicted == item["outcome"]})
    delay_checks = []
    for item in observations["link_delay_measurements"]:
        predicted = _predict_delay(model, item)
        observed = item["observed_delay_ns"]
        relative_error = abs(predicted - observed) / observed
        delay_checks.append({"id": item["id"], "predicted_delay_ns": predicted, "observed_delay_ns": observed, "relative_error": relative_error})

    def accuracy(checks: Sequence[Mapping[str, Any]]) -> float:
        return sum(bool(item["match"]) for item in checks) / len(checks) if checks else 1.0

    delay_errors = [item["relative_error"] for item in delay_checks]
    mean_delay_error = sum(delay_errors) / len(delay_errors) if delay_errors else 0.0
    max_delay_error = max(delay_errors, default=0.0)
    acceptance = model["acceptance"]
    gates = {
        "capacity_outcome_accuracy": accuracy(capacity_checks),
        "link_outcome_accuracy": accuracy(link_checks),
        "delay_mean_relative_error": mean_delay_error,
        "delay_max_relative_error": max_delay_error,
    }
    passed = (
        gates["capacity_outcome_accuracy"] >= acceptance["capacity_outcome_accuracy_min"]
        and gates["link_outcome_accuracy"] >= acceptance["link_outcome_accuracy_min"]
        and gates["delay_mean_relative_error"] <= acceptance["delay_mean_relative_error_max"]
        and gates["delay_max_relative_error"] <= acceptance["delay_max_relative_error_max"]
    )
    return {
        "schema": VALIDATION_SCHEMA,
        "model": model["model"]["name"],
        "fit_dataset_id": model["calibration"]["fit_dataset_id"],
        "holdout_dataset_id": observations["dataset"]["id"],
        "status": "pass" if passed else "fail",
        "gates": gates,
        "thresholds": acceptance,
        "capacity_checks": capacity_checks,
        "link_capacity_checks": link_checks,
        "link_delay_checks": delay_checks,
    }


def fit_calibrated_platform_files(
    template_path: Path, observations_path: Path, output_path: Path
) -> Dict[str, Any]:
    model = fit_calibrated_platform(read_json(template_path), read_json(observations_path))
    write_json(output_path, model)
    return {"status": "pass", "model": model["model"]["name"], "output": str(output_path)}


def materialize_calibrated_boarddb_file(
    model_path: Path, configuration_id: str, profile: str, output_path: Path
) -> Dict[str, Any]:
    boarddb = materialize_calibrated_boarddb(read_json(model_path), configuration_id, profile)
    write_json(output_path, boarddb)
    return {"status": "pass", "configuration": configuration_id, "profile": profile, "output": str(output_path)}


def validate_calibrated_platform_holdout_files(
    model_path: Path,
    observations_path: Path,
    output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    report = validate_calibrated_platform_holdout(
        read_json(model_path), read_json(observations_path)
    )
    if output_path is not None:
        write_json(output_path, report)
        report = dict(report)
        report["output"] = str(output_path)
    return report
