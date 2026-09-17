"""Behaviorally calibrated academic multi-FPGA platform models.

The model deliberately captures only externally observable resource, link,
TDM, transport, and timing behavior.  It is not a hardware clone and does not
encode a reference tool's partitioner, router, reports, file paths, or internal
data structures.
"""

from __future__ import annotations

import math
import hashlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .board_link_timing import directed_board_links, validate_board_link_timing
from .errors import ValidationError
from .io import read_json, write_json
from .platform import Platform
from .resources import RESOURCE_FIELDS, ResourceVector, classify_primitive_resources


TEMPLATE_SCHEMA = "emuflow.calibrated-platform-template/v1"
OBSERVATIONS_SCHEMA = "emuflow.platform-calibration-observations/v1"
MODEL_SCHEMA = "emuflow.calibrated-academic-platform/v1"
VALIDATION_SCHEMA = "emuflow.calibrated-academic-platform-validation/v1"
APPLICATION_HOLDOUT_SCHEMA = (
    "emuflow.calibrated-academic-platform-application-holdout/v1"
)
APPLICATION_VALIDATION_SCHEMA = (
    "emuflow.calibrated-academic-platform-application-validation/v1"
)
PARTITION_LOAD_SCHEMA = "emuflow.calibrated-platform-partition-load/v1"
PARTITION_ENVELOPE_VALIDATION_SCHEMA = (
    "emuflow.calibrated-platform-partition-envelope-validation/v1"
)
RESOURCE_UNIT_MEASUREMENT_SCHEMA = (
    "emuflow.calibrated-platform-resource-unit-measurement/v1"
)

_ROLES = {"fit", "holdout"}
_SOURCE_CLASSES = {"authorized_reference_flow", "synthetic_fixture"}
_PUBLICATION_SCOPES = {"internal", "aggregate_only", "public"}
_OUTCOMES = {"pass", "capacity_fail"}
_PROFILES = ("conservative", "nominal", "aggressive")
_PUBLICATION_RANK = {"internal": 0, "aggregate_only": 1, "public": 2}


def _attribute_is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        try:
            return int(value, 2) != 0
        except ValueError:
            return value.strip().lower() in {"yes", "true"}
    return False


def _reachable_yosys_resource_totals(
    design: Mapping[str, Any], top: str
) -> Dict[str, int]:
    """Count mapped leaf resources through retained Yosys hierarchy.

    Calibration RTL intentionally keeps hierarchy so both the reference flow
    and the academic mapper cannot optimize away repeated resource probes.
    Counting only top-level cells would therefore report one opaque module.
    This traversal expands reachable non-blackbox modules and classifies only
    leaf/blackbox primitives; unreachable vendor library modules are ignored.
    """

    modules = _mapping(design.get("modules"), "mapped Yosys design.modules")
    normalized_modules = {
        str(name).lstrip("\\"): _mapping(module, f"mapped Yosys module {name}")
        for name, module in modules.items()
    }
    top_name = top.lstrip("\\")
    if top_name not in normalized_modules:
        raise ValidationError(f"mapped Yosys design: top module {top!r} not found")

    memo: Dict[str, ResourceVector] = {}

    def visit(module_name: str, stack: Tuple[str, ...]) -> ResourceVector:
        if module_name in memo:
            return memo[module_name]
        if module_name in stack:
            raise ValidationError(
                "mapped Yosys design: recursive module hierarchy "
                + " -> ".join((*stack, module_name))
            )
        module = normalized_modules[module_name]
        attributes = _mapping(
            module.get("attributes", {}),
            f"mapped Yosys module {module_name}.attributes",
        )
        if _attribute_is_true(attributes.get("blackbox", False)):
            result = classify_primitive_resources(module_name)
            memo[module_name] = result
            return result

        cells = _mapping(
            module.get("cells", {}), f"mapped Yosys module {module_name}.cells"
        )
        vectors = []
        vtr_memory_groups = set()
        for cell_name, raw_cell in cells.items():
            cell = _mapping(
                raw_cell, f"mapped Yosys module {module_name}.cells[{cell_name}]"
            )
            cell_type = _string(
                cell.get("type"),
                f"mapped Yosys module {module_name}.cells[{cell_name}].type",
            ).lstrip("\\")
            if cell_type.lower() in {"single_port_ram", "dual_port_ram"}:
                # VTR's public memory model emits one architecture atom per
                # data bit.  Yosys preserves the originating memory instance
                # in each flattened atom name, so count each group once rather
                # than treating bit atoms as independent BRAMs.
                marker = ".bits["
                if marker not in cell_name:
                    raise ValidationError(
                        "mapped Yosys design: cannot identify the physical "
                        f"VTR memory group for cell {cell_name!r}"
                    )
                vtr_memory_groups.add(cell_name.rsplit(marker, 1)[0])
                continue
            child = normalized_modules.get(cell_type)
            if child is not None and not _attribute_is_true(
                _mapping(
                    child.get("attributes", {}),
                    f"mapped Yosys module {cell_type}.attributes",
                ).get("blackbox", False)
            ):
                vectors.append(visit(cell_type, (*stack, module_name)))
            else:
                vectors.append(classify_primitive_resources(cell_type))
        vectors.extend(
            ResourceVector(bram=1) for _ in sorted(vtr_memory_groups)
        )
        result = ResourceVector.sum(vectors)
        memo[module_name] = result
        return result

    return visit(top_name, ()).to_dict(include_zeros=False)


def measure_mapped_yosys_resource_units(
    design: Mapping[str, Any],
    *,
    top: str,
    resource: str,
    source_sha256: str,
) -> Dict[str, Any]:
    """Measure one academic resource namespace from a mapped Yosys design."""

    if resource not in {"lut", "ff", "bram", "dsp"}:
        raise ValidationError(f"resource: unsupported mapping resource {resource!r}")
    if len(source_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in source_sha256
    ):
        raise ValidationError("source_sha256: expected lowercase SHA-256")
    totals = _reachable_yosys_resource_totals(design, top)
    if resource == "bram":
        bram18k = totals.get("bram18k", 0)
        if bram18k % 2:
            raise ValidationError(
                "isolated BRAM mapping probe must contain a whole number of "
                "36-Kib academic BRAM units"
            )
        academic_units = totals.get("bram", 0) + bram18k // 2
    elif resource == "dsp":
        academic_units = totals.get("dsp", 0) + totals.get("dsp48", 0)
    else:
        academic_units = totals.get(resource, 0)
    if academic_units <= 0:
        raise ValidationError(
            f"mapped Yosys design contains no academic {resource} units"
        )
    return {
        "schema": RESOURCE_UNIT_MEASUREMENT_SCHEMA,
        "top": top,
        "resource": resource,
        "academic_units": academic_units,
        "mapped_resource_totals": totals,
        "source_sha256": source_sha256,
    }


def measure_mapped_yosys_resource_units_file(
    design_path: Path,
    *,
    top: str,
    resource: str,
    source_path: Path,
    output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    digest = hashlib.sha256()
    with source_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    report = measure_mapped_yosys_resource_units(
        read_json(design_path),
        top=top,
        resource=resource,
        source_sha256=digest.hexdigest(),
    )
    if output_path is not None:
        write_json(output_path, report)
    return report


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
            "resource_unit_mapping_max_relative_error",
            "application_tdm_ratio_absolute_error_max",
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
        "resource_unit_mapping_max_relative_error": _number(
            acceptance.get("resource_unit_mapping_max_relative_error"),
            "template.acceptance.resource_unit_mapping_max_relative_error",
        ),
        "application_tdm_ratio_absolute_error_max": _integer(
            acceptance.get("application_tdm_ratio_absolute_error_max"),
            "template.acceptance.application_tdm_ratio_absolute_error_max",
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
            "resource_unit_mappings",
            "link_capacity_boundaries",
            "link_characteristics",
            "link_delay_measurements",
            "collection",
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
    for index, raw in enumerate(
        _array(root.get("capacity_boundaries", []), "capacity_boundaries")
    ):
        item = _mapping(raw, f"capacity_boundaries[{index}]")
        _reject_unknown(
            item,
            {
                "id",
                "configuration",
                "resource",
                "demand_per_fpga",
                "utilization_limit",
                "outcome",
                "assignment_control",
            },
            f"capacity_boundaries[{index}]",
        )
        resource = _string(
            item.get("resource"), f"capacity_boundaries[{index}].resource"
        )
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
        utilization_limit = _number(
            item.get("utilization_limit"),
            f"capacity_boundaries[{index}].utilization_limit",
            exclusive=True,
        )
        if utilization_limit > 1.0:
            raise ValidationError(
                f"capacity_boundaries[{index}].utilization_limit: expected <= 1"
            )
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
                "utilization_limit": utilization_limit,
                "outcome": outcome,
                "assignment_control": "fixed",
            }
        )

    resource_unit_mappings = []
    for index, raw in enumerate(
        _array(root.get("resource_unit_mappings", []), "resource_unit_mappings")
    ):
        item = _mapping(raw, f"resource_unit_mappings[{index}]")
        _reject_unknown(
            item,
            {
                "id",
                "configuration",
                "resource",
                "reference_units",
                "academic_units",
                "source_sha256",
                "mapping_control",
            },
            f"resource_unit_mappings[{index}]",
        )
        resource = _string(
            item.get("resource"), f"resource_unit_mappings[{index}].resource"
        )
        if resource not in RESOURCE_FIELDS:
            raise ValidationError(
                f"resource_unit_mappings[{index}].resource: unsupported resource"
            )
        if item.get("mapping_control") != "isolated_same_rtl":
            raise ValidationError(
                f"resource_unit_mappings[{index}].mapping_control: expected "
                "'isolated_same_rtl'"
            )
        source_sha256 = _string(
            item.get("source_sha256"),
            f"resource_unit_mappings[{index}].source_sha256",
        )
        if len(source_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in source_sha256
        ):
            raise ValidationError(
                f"resource_unit_mappings[{index}].source_sha256: expected SHA-256"
            )
        resource_unit_mappings.append(
            {
                "id": _string(item.get("id"), f"resource_unit_mappings[{index}].id"),
                "configuration": _string(
                    item.get("configuration"),
                    f"resource_unit_mappings[{index}].configuration",
                ),
                "resource": resource,
                "reference_units": _integer(
                    item.get("reference_units"),
                    f"resource_unit_mappings[{index}].reference_units",
                    minimum=1,
                ),
                "academic_units": _integer(
                    item.get("academic_units"),
                    f"resource_unit_mappings[{index}].academic_units",
                    minimum=1,
                ),
                "source_sha256": source_sha256,
                "mapping_control": "isolated_same_rtl",
            }
        )

    link_characteristics = []
    for index, raw in enumerate(
        _array(root.get("link_characteristics", []), "link_characteristics")
    ):
        item = _mapping(raw, f"link_characteristics[{index}]")
        _reject_unknown(
            item,
            {
                "id",
                "configuration",
                "hop_count",
                "line_rate_mbps",
                "phy_width_bits",
                "channels_per_direction",
                "max_tdm_ratio",
                "base_route_delay_ns",
                "assignment_control",
                "route_control",
            },
            f"link_characteristics[{index}]",
        )
        if item.get("assignment_control") != "fixed" or item.get("route_control") != "fixed":
            raise ValidationError(
                f"link_characteristics[{index}]: hardware fitting requires fixed assignment and route"
            )
        if item.get("hop_count") != 1:
            raise ValidationError(
                f"link_characteristics[{index}].hop_count: expected a controlled single-hop route"
            )
        link_characteristics.append(
            {
                "id": _string(item.get("id"), f"link_characteristics[{index}].id"),
                "configuration": _string(
                    item.get("configuration"),
                    f"link_characteristics[{index}].configuration",
                ),
                "hop_count": 1,
                "line_rate_mbps": _number(
                    item.get("line_rate_mbps"),
                    f"link_characteristics[{index}].line_rate_mbps",
                    exclusive=True,
                ),
                "phy_width_bits": _integer(
                    item.get("phy_width_bits"),
                    f"link_characteristics[{index}].phy_width_bits",
                    minimum=1,
                ),
                "channels_per_direction": _integer(
                    item.get("channels_per_direction"),
                    f"link_characteristics[{index}].channels_per_direction",
                    minimum=1,
                ),
                "max_tdm_ratio": _integer(
                    item.get("max_tdm_ratio"),
                    f"link_characteristics[{index}].max_tdm_ratio",
                    minimum=1,
                ),
                "base_route_delay_ns": _number(
                    item.get("base_route_delay_ns"),
                    f"link_characteristics[{index}].base_route_delay_ns",
                    exclusive=True,
                ),
                "assignment_control": "fixed",
                "route_control": "fixed",
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
                "max_tdm_ratio",
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
                "max_tdm_ratio": _integer(
                    item.get("max_tdm_ratio"),
                    f"link_delay_measurements[{index}].max_tdm_ratio",
                    minimum=1,
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

    all_items = (
        capacity_boundaries
        + resource_unit_mappings
        + link_capacity_boundaries
        + link_characteristics
        + delay_measurements
    )
    _unique_ids(all_items, "observations")
    if not all_items:
        raise ValidationError("observations: expected at least one measurement")
    normalized = {
        "schema": OBSERVATIONS_SCHEMA,
        "dataset": normalized_dataset,
        "capacity_boundaries": capacity_boundaries,
        "resource_unit_mappings": resource_unit_mappings,
        "link_capacity_boundaries": link_capacity_boundaries,
        "link_characteristics": link_characteristics,
        "link_delay_measurements": delay_measurements,
    }
    if "collection" in root:
        normalized["collection"] = dict(
            _mapping(root["collection"], "observations.collection")
        )
    return normalized


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


def _raw_capacity_interval(
    observations: Sequence[Mapping[str, Any]], context: str
) -> Tuple[int, int]:
    """Normalize controlled thresholds to the underlying raw device capacity.

    A probe executed with utilization ``u`` passes when
    ``demand <= floor(raw_capacity * u)``.  Running a small probe at a reduced
    utilization therefore identifies the same raw device capacity without
    elaborating millions of synthetic cells merely to reach a 75% boundary.
    """

    passed = [
        math.ceil(int(item["demand_per_fpga"]) / float(item["utilization_limit"]))
        for item in observations
        if item["outcome"] == "pass"
    ]
    failed_upper = [
        math.ceil(int(item["demand_per_fpga"]) / float(item["utilization_limit"]))
        for item in observations
        if item["outcome"] == "capacity_fail"
    ]
    if not passed or not failed_upper:
        raise ValidationError(
            f"{context}: requires both pass and capacity_fail observations"
        )
    lower = max(passed)
    upper = min(failed_upper)
    if lower >= upper:
        raise ValidationError(
            f"{context}: inconsistent normalized raw-capacity boundary; "
            f"pass lower {lower} is not below fail upper {upper}"
        )
    return lower, upper


def _fit_resource_unit_mappings(
    observations: Sequence[Mapping[str, Any]],
    resources: Sequence[str],
    maximum_relative_error: float,
) -> Dict[str, Dict[str, Any]]:
    """Fit a zero-intercept, per-resource reference-to-academic conversion.

    Capacity constraints are independent resource axes.  A dense cross-resource
    regression would turn one device's LUT capacity into another device's FF or
    DSP capacity and would not preserve that feasible region.  Controlled
    same-RTL probes therefore identify one scale per resource.  Application
    workloads remain blind holdouts and cannot contribute to these scales.
    """

    result: Dict[str, Dict[str, Any]] = {}
    for resource in resources:
        relevant = [item for item in observations if item["resource"] == resource]
        distinct = {int(item["reference_units"]) for item in relevant}
        if len(relevant) < 2 or len(distinct) < 2:
            raise ValidationError(
                f"resource unit mapping {resource!r}: requires at least two "
                "distinct isolated same-RTL probes"
            )
        denominator = sum(float(item["reference_units"]) ** 2 for item in relevant)
        scale = sum(
            float(item["reference_units"]) * float(item["academic_units"])
            for item in relevant
        ) / denominator
        if not math.isfinite(scale) or scale <= 0.0:
            raise ValidationError(
                f"resource unit mapping {resource!r}: invalid fitted scale"
            )
        relative_errors = [
            abs(scale * float(item["reference_units"]) - item["academic_units"])
            / float(item["academic_units"])
            for item in relevant
        ]
        maximum = max(relative_errors)
        mean = sum(relative_errors) / len(relative_errors)
        if maximum > maximum_relative_error:
            raise ValidationError(
                f"resource unit mapping {resource!r} does not meet acceptance: "
                f"maximum relative error {maximum:.6f}"
            )
        result[resource] = {
            "academic_units_per_reference_unit": scale,
            "fit_mean_relative_error": mean,
            "fit_max_relative_error": maximum,
            "observation_ids": sorted(item["id"] for item in relevant),
        }
    return result


def _consistent_link_characteristic(
    observations: Sequence[Mapping[str, Any]], field: str
) -> float:
    values = {float(item[field]) for item in observations}
    if len(values) != 1:
        raise ValidationError(
            f"link characteristics: inconsistent {field} observations {sorted(values)}"
        )
    return next(iter(values))


def _delay_features(
    item: Mapping[str, Any], characterized_ratios: Sequence[int]
) -> List[float]:
    """Build the externally observable hop-plus-TDM-knot feature vector.

    A reference flow's reported TDM ratio already includes serialization and
    contention decisions.  Adding independent payload-width or flow-count
    terms double-counts those effects and, for providers with discrete TDM
    implementations, incorrectly assumes that consecutive ratios have a
    linear latency cost.  The first characterized ratio is the zero-penalty
    baseline; every higher observed ratio gets an independently fitted tier.
    """

    ratio = int(item["max_tdm_ratio"])
    return [
        1.0,
        float(item["hop_count"]),
        *(1.0 if ratio == candidate else 0.0 for candidate in characterized_ratios[1:]),
    ]


def _one_hop_only_configurations(
    configurations: Sequence[Mapping[str, Any]],
) -> bool:
    """Return true only when every declared topology admits exactly one hop.

    The narrow two-node/one-link test is intentional. A larger clique may
    still use a multi-hop detour under congestion, so treating it as a
    one-hop-only service would extrapolate beyond the calibration evidence.
    """

    for configuration in configurations:
        fpgas = list(configuration["fpgas"])
        links = list(configuration["links"])
        if len(fpgas) != 2 or len(links) != 1:
            return False
        if set(links[0]["endpoints"]) != set(fpgas):
            return False
    return True


def _tdm_penalty_ns(curve: Sequence[Mapping[str, Any]], ratio: int) -> float:
    """Interpolate inside a monotone characterized TDM penalty curve.

    The provider may accept every integer scheduling ratio while the
    calibration campaign observes only a sparse set of timing knots. Sparse
    knots are interpolation evidence, not permission to extrapolate beyond
    the largest measured ratio.
    """

    if not curve:
        raise ValidationError("link delay model: empty TDM penalty curve")
    points = [
        (
            _integer(item.get("ratio"), "link delay model TDM ratio", minimum=1),
            _number(item.get("penalty_ns"), "link delay model TDM penalty"),
        )
        for item in curve
    ]
    if points != sorted(points) or len({point[0] for point in points}) != len(points):
        raise ValidationError("link delay model: TDM penalty ratios must be unique and sorted")
    if not math.isclose(points[0][1], 0.0, rel_tol=0.0, abs_tol=1.0e-9):
        raise ValidationError("link delay model: baseline TDM penalty must be zero")
    if any(right[1] < left[1] for left, right in zip(points, points[1:])):
        raise ValidationError("link delay model: TDM penalty curve must be monotone")
    if ratio <= points[0][0]:
        return points[0][1]
    for left, right in zip(points, points[1:]):
        if ratio <= right[0]:
            fraction = (ratio - left[0]) / (right[0] - left[0])
            return left[1] + fraction * (right[1] - left[1])
    raise ValidationError(
        "link delay model: requested TDM ratio lies above the characterized "
        "timing domain; collect a controlled delay measurement instead of "
        "extrapolating"
    )


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
    # Capacity, routing, and delay measurements characterize one explicit
    # system configuration. Resource-unit mappings do not: they compare the
    # same isolated RTL under the reference and academic device mappers, so
    # their recorded configuration is provenance for the probe host rather
    # than a topology constraint. Independently fitted platform tiers using
    # the same device/mapping contract may therefore share these probes
    # without relabeling them as if topology changed the mapper.
    for category in (
        "capacity_boundaries",
        "link_capacity_boundaries",
        "link_characteristics",
        "link_delay_measurements",
    ):
        for item in observations[category]:
            if item["configuration"] not in configuration_ids:
                raise ValidationError(
                    f"{category} {item['id']!r}: unknown configuration "
                    f"{item['configuration']!r}"
                )

    resource_intervals: Dict[str, Dict[str, int]] = {}
    reference_resource_profiles: Dict[str, Dict[str, int]] = {
        profile: {} for profile in _PROFILES
    }
    resources = sorted({item["resource"] for item in observations["capacity_boundaries"]})
    if not resources:
        raise ValidationError("fit dataset: capacity boundaries are required")
    for resource in resources:
        relevant = [
            item for item in observations["capacity_boundaries"] if item["resource"] == resource
        ]
        lower, upper = _raw_capacity_interval(relevant, f"resource {resource}")
        resource_intervals[resource] = {
            "raw_lower": lower,
            "raw_upper_exclusive": upper,
        }
        for profile, raw_capacity in _profile_values(lower, upper).items():
            reference_resource_profiles[profile][resource] = raw_capacity

    unit_mappings = _fit_resource_unit_mappings(
        observations["resource_unit_mappings"],
        resources,
        float(
            template["acceptance"]["resource_unit_mapping_max_relative_error"]
        ),
    )
    academic_resource_profiles: Dict[str, Dict[str, int]] = {
        profile: {
            resource: max(
                1,
                math.floor(
                    reference_resource_profiles[profile][resource]
                    * unit_mappings[resource][
                        "academic_units_per_reference_unit"
                    ]
                ),
            )
            for resource in resources
        }
        for profile in _PROFILES
    }

    link_characteristics = observations["link_characteristics"]
    if not link_characteristics:
        raise ValidationError("fit dataset: link characteristics are required")
    line_rate_mbps = _consistent_link_characteristic(
        link_characteristics, "line_rate_mbps"
    )
    phy_width_bits = int(
        _consistent_link_characteristic(link_characteristics, "phy_width_bits")
    )
    channels_per_direction = int(
        _consistent_link_characteristic(
            link_characteristics, "channels_per_direction"
        )
    )
    max_tdm_ratio = int(
        _consistent_link_characteristic(link_characteristics, "max_tdm_ratio")
    )
    base_route_delay_ns = _consistent_link_characteristic(
        link_characteristics, "base_route_delay_ns"
    )
    clock_mhz = line_rate_mbps / phy_width_bits
    declared_clock = float(template["link"]["fabric_clock_mhz"])
    if not math.isclose(clock_mhz, declared_clock, rel_tol=0.0, abs_tol=1.0e-9):
        raise ValidationError(
            "template.link.fabric_clock_mhz disagrees with characterized "
            "line_rate_mbps / phy_width_bits"
        )
    # PPro's PHY width is the serializer width behind each board channel, not
    # the number of independently schedulable logical nets in one TDM slot.
    # The academic BoardDB therefore exposes one logical bit per characterized
    # directional channel.  Multiplying by the PHY width would make Phase 5
    # eight times too optimistic and fails the application-level DLA holdout.
    logical_payload = channels_per_direction
    physical_line_bits = channels_per_direction * phy_width_bits
    slot_ns = 1000.0 / clock_mhz
    delay_measurements = observations["link_delay_measurements"]
    if len(delay_measurements) < 4:
        raise ValidationError("delay fit requires at least four controlled measurements")
    characterized_ratios = sorted(
        {int(item["max_tdm_ratio"]) for item in delay_measurements}
    )
    if len(characterized_ratios) < 2:
        raise ValidationError(
            "delay fit is not identifiable: characterize at least two observed TDM knots"
        )
    one_hop_only = _one_hop_only_configurations(template["configurations"])
    feature_rows: List[List[float]] = []
    for item in delay_measurements:
        full_features = _delay_features(item, characterized_ratios)
        feature_rows.append(
            [full_features[0], *full_features[2:]]
            if one_hop_only
            else full_features
        )
    coefficient_count = (1 if one_hop_only else 2) + len(characterized_ratios) - 1
    if _matrix_rank(feature_rows) < coefficient_count:
        raise ValidationError(
            "delay fit is not identifiable: vary hop count independently at the "
            "baseline (unless the declared platform is strictly one-hop-only) "
            "and characterize every observed TDM knot"
        )
    observed_values = [float(item["observed_delay_ns"]) for item in delay_measurements]
    coefficients = _nnls_coordinate_descent(feature_rows, observed_values)
    predicted_values = [
        sum(coefficient * feature for coefficient, feature in zip(coefficients, row))
        for row in feature_rows
    ]
    residuals = [
        observed - predicted
        for observed, predicted in zip(observed_values, predicted_values)
    ]
    maximum_absolute_residual = max(abs(value) for value in residuals)
    mean_absolute_residual = sum(abs(value) for value in residuals) / len(residuals)
    relative_errors = [
        abs(residual) / float(item["observed_delay_ns"])
        for residual, item in zip(residuals, delay_measurements)
    ]
    mean_relative_error = sum(relative_errors) / len(relative_errors)
    maximum_relative_error = max(relative_errors)
    if (
        mean_relative_error
        > float(template["acceptance"]["delay_mean_relative_error_max"])
        or maximum_relative_error
        > float(template["acceptance"]["delay_max_relative_error_max"])
    ):
        raise ValidationError(
            "delay fit does not meet acceptance thresholds: "
            f"mean relative error {mean_relative_error:.6f}, "
            f"maximum relative error {maximum_relative_error:.6f}"
        )
    if one_hop_only:
        endpoint_ns = 0.0
        per_hop_ns = coefficients[0]
        tdm_penalties = [0.0, *coefficients[1:]]
        hop_model_scope = "declared_one_hop_only"
        delay_equation = (
            "one_hop_base_ns + interpolate(tdm_penalty_curve_ns, max_tdm_ratio)"
        )
    else:
        endpoint_ns, per_hop_ns = coefficients[:2]
        tdm_penalties = [0.0, *coefficients[2:]]
        hop_model_scope = "identified_multi_hop"
        delay_equation = (
            "endpoint_ns + hop_count * per_hop_ns + "
            "interpolate(tdm_penalty_curve_ns, max_tdm_ratio)"
        )
    if any(
        right + 1.0e-9 < left
        for left, right in zip(tdm_penalties, tdm_penalties[1:])
    ):
        raise ValidationError(
            "delay fit produced a non-monotone TDM knot curve; collect more "
            "controlled measurements instead of publishing an unstable model"
        )
    tdm_penalty_curve = [
        {"ratio": ratio, "penalty_ns": penalty}
        for ratio, penalty in zip(characterized_ratios, tdm_penalties)
    ]
    if characterized_ratios[-1] > max_tdm_ratio:
        raise ValidationError(
            "delay fit observed a TDM ratio above the characterized provider maximum"
        )
    # PPro's scheduling ratio is an integer, not a power-of-two service tier.
    # Keep the provider-declared scheduling domain separate from the sparse
    # delay-curve knots. Timing consumers still fail closed above the largest
    # characterized knot in ``_tdm_penalty_ns``.
    supported_tdm_ratios = list(range(1, max_tdm_ratio + 1))

    profiles = {}
    for profile in _PROFILES:
        base_one_hop_ns = base_route_delay_ns
        profiles[profile] = {
            "device_capacity": dict(
                sorted(academic_resource_profiles[profile].items())
            ),
            "reference_device_capacity": dict(
                sorted(reference_resource_profiles[profile].items())
            ),
            "link_channels_per_direction": channels_per_direction,
            "phy_serialization_width_bits": phy_width_bits,
            "payload_bits_per_lane_per_cycle": 1,
            "link_payload_bits_per_cycle_per_direction": logical_payload,
            "max_tdm_ratio": max_tdm_ratio,
            "tdm_supported_ratios": supported_tdm_ratios,
            "base_one_hop_latency_ns": base_one_hop_ns,
            "base_one_hop_latency_cycles": math.ceil(base_one_hop_ns / slot_ns),
        }

    fit_ids = sorted(
        item["id"]
        for category in (
            "capacity_boundaries",
            "resource_unit_mappings",
            "link_capacity_boundaries",
            "link_characteristics",
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
            "reference_resource_raw_capacity_intervals": resource_intervals,
            "resource_unit_mapping": unit_mappings,
            "link_characteristics": {
                "line_rate_mbps": line_rate_mbps,
                "phy_width_bits": phy_width_bits,
                "channels_per_direction": channels_per_direction,
                "logical_tdm_bits_per_slot_per_direction": logical_payload,
                "physical_line_bits_per_fabric_cycle_per_direction": (
                    physical_line_bits
                ),
                "max_tdm_ratio": max_tdm_ratio,
                "base_route_delay_ns": base_route_delay_ns,
            },
            "link_delay_model": {
                "equation": delay_equation,
                "hop_model_scope": hop_model_scope,
                "endpoint_ns": endpoint_ns,
                "per_hop_ns": per_hop_ns,
                "tdm_penalty_curve_ns": tdm_penalty_curve,
                "slot_ns": slot_ns,
                "fit_mean_absolute_residual_ns": mean_absolute_residual,
                "fit_max_absolute_residual_ns": maximum_absolute_residual,
                "fit_mean_relative_error": mean_relative_error,
                "fit_max_relative_error": maximum_relative_error,
            },
            "parameter_provenance": {
                "device_capacity": (
                    "controlled fixed-assignment pass/fail boundaries normalized "
                    "by the applied utilization limit, then converted into the "
                    "academic mapper's units using disjoint isolated same-RTL probes"
                ),
                "link_capacity": (
                    "one independently schedulable logical bit per characterized "
                    "directional channel; PHY serialization width and line rate "
                    "remain separate physical characteristics"
                ),
                "link_delay": (
                    "non-negative hop fit plus a monotone TDM-knot curve "
                    "over controlled fixed-assignment/fixed-route measurements; "
                    "the observed ratio already captures serialization and contention"
                ),
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
        profile_value = _mapping(profiles[profile], f"profiles.{profile}")
        academic_capacity = _mapping(
            profile_value.get("device_capacity"),
            f"profiles.{profile}.device_capacity",
        )
        reference_capacity = _mapping(
            profile_value.get("reference_device_capacity"),
            f"profiles.{profile}.reference_device_capacity",
        )
        if set(academic_capacity) != set(reference_capacity):
            raise ValidationError(
                f"profiles.{profile}: academic and reference resource axes differ"
            )
        maximum_ratio = _integer(
            profile_value.get("max_tdm_ratio"),
            f"profiles.{profile}.max_tdm_ratio",
            minimum=1,
        )
        supported_ratios = [
            _integer(
                item,
                f"profiles.{profile}.tdm_supported_ratios",
                minimum=1,
            )
            for item in _array(
                profile_value.get("tdm_supported_ratios"),
                f"profiles.{profile}.tdm_supported_ratios",
                nonempty=True,
            )
        ]
        if supported_ratios != list(range(1, maximum_ratio + 1)):
            raise ValidationError(
                f"profiles.{profile}.tdm_supported_ratios must describe the "
                "complete integer scheduling-ratio domain"
            )
        for configuration in configurations:
            materialize_calibrated_boarddb(root, configuration["id"], profile)
    mapping = _mapping(
        root["calibration"].get("resource_unit_mapping"),
        "calibrated model.calibration.resource_unit_mapping",
    )
    if set(mapping) != set(profiles["nominal"]["device_capacity"]):
        raise ValidationError(
            "calibrated model resource-unit mapping does not cover device capacity"
        )
    delay_model = _mapping(
        root["calibration"].get("link_delay_model"),
        "calibrated model.calibration.link_delay_model",
    )
    expected_hop_scope = (
        "declared_one_hop_only"
        if _one_hop_only_configurations(configurations)
        else "identified_multi_hop"
    )
    if delay_model.get("hop_model_scope") != expected_hop_scope:
        raise ValidationError(
            "calibrated model link-delay hop scope does not match its declared "
            "platform configurations"
        )
    return dict(root)


def materialize_calibrated_boarddb(
    model_value: Mapping[str, Any],
    configuration_id: str,
    profile: str = "nominal",
    utilization_limit: Optional[float] = None,
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
    declared_utilization_limit = _number(
        device.get("utilization_limit"),
        "calibrated model.device.utilization_limit",
        exclusive=True,
    )
    selected_utilization_limit = (
        declared_utilization_limit
        if utilization_limit is None
        else _number(
            utilization_limit,
            "calibrated materialization utilization_limit",
            exclusive=True,
        )
    )
    if selected_utilization_limit > declared_utilization_limit:
        raise ValidationError(
            "calibrated materialization utilization_limit may reduce the "
            "declared loading policy for a stress qualification, but may not "
            "increase it"
        )
    if selected_utilization_limit > 1.0:
        raise ValidationError(
            "calibrated materialization utilization_limit must be <= 1"
        )
    utilization_suffix = (
        ""
        if math.isclose(
            selected_utilization_limit,
            declared_utilization_limit,
            rel_tol=0.0,
            abs_tol=1.0e-12,
        )
        else f"__util{round(selected_utilization_limit * 10000):04d}bp"
    )
    payload_capacity = _integer(
        profile_value.get("link_payload_bits_per_cycle_per_direction"),
        f"profiles.{profile}.link_payload_bits_per_cycle_per_direction",
        minimum=1,
    )
    channels_per_direction = _integer(
        profile_value.get("link_channels_per_direction"),
        f"profiles.{profile}.link_channels_per_direction",
        minimum=1,
    )
    payload_bits_per_lane = _integer(
        profile_value.get("payload_bits_per_lane_per_cycle"),
        f"profiles.{profile}.payload_bits_per_lane_per_cycle",
        minimum=1,
    )
    if channels_per_direction * payload_bits_per_lane != payload_capacity:
        raise ValidationError(
            f"profiles.{profile}: link channel count and PHY width disagree "
            "with aggregate payload capacity"
        )
    latency_cycles = _integer(
        profile_value.get("base_one_hop_latency_cycles"),
        f"profiles.{profile}.base_one_hop_latency_cycles",
    )
    boarddb = {
        "schema": "emuflow.boarddb/v1",
        "platform": {
            "name": (
                f"{model['name']}__{configuration_id}__{profile}"
                f"{utilization_suffix}"
            ),
            "kind": "virtual",
            "description": (
                "Behaviorally calibrated academic platform; not a physical "
                "hardware clone"
                + (
                    "; explicit reduced-utilization stress qualification"
                    if utilization_suffix
                    else ""
                )
            ),
        },
        "fpgas": [
            {
                "id": fpga_id,
                "part": device["part"],
                "utilization_limit": selected_utilization_limit,
                "capacity": capacity,
            }
            for fpga_id in configuration["fpgas"]
        ],
        "links": [
            {
                "id": item["id"],
                "endpoints": item["endpoints"],
                "direction": link["direction"],
                # A characterized PHY width greater than one means each
                # physical channel transfers multiple payload bits per fabric
                # cycle.  BoardDB represents that contract as a serial link;
                # abstract/parallel links intentionally have unit-width lanes.
                "mode": "serial" if payload_bits_per_lane > 1 else "abstract",
                "data_lanes_per_direction": channels_per_direction,
                "payload_bits_per_lane_per_cycle": payload_bits_per_lane,
                "fabric_clock_mhz": link["fabric_clock_mhz"],
                **(
                    {
                        "max_line_rate_gbps_per_lane": (
                            float(link["fabric_clock_mhz"])
                            * payload_bits_per_lane
                            / 1000.0
                        )
                    }
                    if payload_bits_per_lane > 1
                    else {}
                ),
                "latency_cycles": latency_cycles,
                "capacity_sharing": link["capacity_sharing"],
            }
            for item in configuration["links"]
        ],
    }
    return Platform.from_dict(boarddb).to_dict()


def materialize_calibrated_board_link_timing(
    model_value: Mapping[str, Any],
    configuration_id: str,
    profile: str = "nominal",
    utilization_limit: Optional[float] = None,
) -> Dict[str, Any]:
    """Materialize characterized directed timing beside the BoardDB."""

    model = validate_calibrated_platform_model(model_value)
    platform = Platform.from_dict(
        materialize_calibrated_boarddb(
            model,
            configuration_id,
            profile,
            utilization_limit=utilization_limit,
        )
    )
    calibration = _mapping(model["calibration"], "calibrated model.calibration")
    characteristics = _mapping(
        calibration.get("link_characteristics"),
        "calibrated model.calibration.link_characteristics",
    )
    delay_ns = _number(
        characteristics.get("base_route_delay_ns"),
        "calibrated model.calibration.link_characteristics.base_route_delay_ns",
    )
    reference = _string(
        calibration.get("fit_dataset_id"),
        "calibrated model.calibration.fit_dataset_id",
    )
    records = []
    for (link_id, source, sink), link in sorted(
        directed_board_links(platform).items()
    ):
        records.append(
            {
                "link": link_id,
                "from": source,
                "to": sink,
                "fabric_clock_mhz": link.fabric_clock_mhz,
                "latency_cycles": link.latency_cycles,
                "delay_bound_ns": delay_ns,
                "qualification": "characterized-upper-bound",
                "source": {
                    "kind": "vendor-characterization",
                    "reference": f"authorized aggregate dataset {reference}",
                },
            }
        )
    database = {
        "schema": "emuflow.board-link-timing/v1",
        "status": "pass",
        "platform": platform.name,
        "measurement_scope": "tx-transport-stage-to-rx-transport-stage",
        "final_link_timing_signoff": False,
        "links": records,
    }
    validate_board_link_timing(database, platform)
    return database


def _predict_delay(model: Mapping[str, Any], item: Mapping[str, Any]) -> float:
    calibration = model["calibration"]
    delay_model = calibration["link_delay_model"]
    if (
        delay_model.get("hop_model_scope") == "declared_one_hop_only"
        and int(item["hop_count"]) != 1
    ):
        raise ValidationError(
            "one-hop-only calibrated platform cannot predict a multi-hop path"
        )
    return (
        delay_model["endpoint_ns"]
        + item["hop_count"] * delay_model["per_hop_ns"]
        + _tdm_penalty_ns(
            delay_model["tdm_penalty_curve_ns"], int(item["max_tdm_ratio"])
        )
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
        "link_characteristics",
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
            "link_characteristics",
            "link_delay_measurements",
        )
        for item in observations[category]
    }
    overlap = sorted(fit_ids & holdout_ids)
    if overlap:
        raise ValidationError(f"holdout observations overlap fit observations: {overlap}")

    nominal = model["profiles"]["nominal"]
    capacity_checks = []
    for item in observations["capacity_boundaries"]:
        raw_capacity = nominal["reference_device_capacity"].get(item["resource"])
        if raw_capacity is None:
            raise ValidationError(
                f"holdout resource {item['resource']!r} was not identified during fitting"
            )
        effective_capacity = math.floor(
            raw_capacity * float(item["utilization_limit"])
        )
        predicted = "pass" if item["demand_per_fpga"] <= effective_capacity else "capacity_fail"
        capacity_checks.append({"id": item["id"], "predicted": predicted, "observed": item["outcome"], "match": predicted == item["outcome"]})
    link_checks = []
    link_capacity = (
        nominal["link_payload_bits_per_cycle_per_direction"]
        * nominal["max_tdm_ratio"]
    )
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
        "configurations": sorted(
            {
                item["configuration"]
                for category in (
                    "capacity_boundaries",
                    "link_capacity_boundaries",
                    "link_characteristics",
                    "link_delay_measurements",
                )
                for item in observations[category]
            }
        ),
        "status": "pass" if passed else "fail",
        "gates": gates,
        "thresholds": acceptance,
        "capacity_checks": capacity_checks,
        "link_capacity_checks": link_checks,
        "link_delay_checks": delay_checks,
    }


def validate_calibrated_platform_application_holdout(
    model_value: Mapping[str, Any], observation_value: Mapping[str, Any]
) -> Dict[str, Any]:
    """Validate a free-partition real-application holdout without fitting it."""

    model = validate_calibrated_platform_model(model_value)
    observation = _mapping(observation_value, "application holdout")
    if observation.get("schema") != APPLICATION_HOLDOUT_SCHEMA:
        raise ValidationError(
            f"application holdout.schema: expected {APPLICATION_HOLDOUT_SCHEMA!r}"
        )
    _reject_unknown(
        observation,
        {
            "schema",
            "dataset",
            "workload",
            "configuration",
            "partition_mode",
            "utilization_limit",
            "resource_demand",
            "observed",
        },
        "application holdout",
    )
    dataset = _mapping(observation.get("dataset"), "application holdout.dataset")
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
        "application holdout.dataset",
    )
    if dataset.get("role") != "holdout":
        raise ValidationError("application holdout dataset must have role 'holdout'")
    dataset_id = _string(dataset.get("id"), "application holdout.dataset.id")
    source_class = _string(
        dataset.get("source_class"), "application holdout.dataset.source_class"
    )
    if source_class not in _SOURCE_CLASSES:
        raise ValidationError("application holdout dataset has unsupported source_class")
    publication_scope = _string(
        dataset.get("publication_scope"),
        "application holdout.dataset.publication_scope",
    )
    if publication_scope not in _PUBLICATION_SCOPES:
        raise ValidationError(
            "application holdout dataset has unsupported publication_scope"
        )
    for field in ("reference_alias", "source_class", "authorization_id"):
        if model["model"][field] != dataset.get(field):
            raise ValidationError(f"application holdout {field} does not match model")
    if (
        _PUBLICATION_RANK[model["model"]["publication_scope"]]
        > _PUBLICATION_RANK[publication_scope]
    ):
        raise ValidationError(
            "application holdout publication_scope does not authorize model validation publication"
        )
    if dataset_id == model["calibration"]["fit_dataset_id"]:
        raise ValidationError("application holdout dataset overlaps the fit dataset")
    workload = _mapping(
        observation.get("workload"), "application holdout.workload"
    )
    workload_id = _string(workload.get("id"), "application holdout.workload.id")
    source_sha256 = _string(
        workload.get("source_sha256"),
        "application holdout.workload.source_sha256",
    )
    if len(source_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in source_sha256
    ):
        raise ValidationError("application holdout workload source_sha256 is invalid")
    configuration_id = _string(
        observation.get("configuration"), "application holdout.configuration"
    )
    configuration = next(
        (
            item
            for item in model["configurations"]
            if item["id"] == configuration_id
        ),
        None,
    )
    if configuration is None:
        raise ValidationError("application holdout uses an unknown configuration")
    if observation.get("partition_mode") != "free":
        raise ValidationError("application holdout requires partition_mode 'free'")
    utilization_limit = _number(
        observation.get("utilization_limit"),
        "application holdout.utilization_limit",
        exclusive=True,
    )
    if utilization_limit > 1.0:
        raise ValidationError("application holdout utilization_limit must be <= 1")
    demand_namespaces = _mapping(
        observation.get("resource_demand"),
        "application holdout.resource_demand",
    )
    _reject_unknown(
        demand_namespaces,
        {"reference", "academic"},
        "application holdout.resource_demand",
    )
    reference_demand = {
        resource: _integer(
            value,
            f"application holdout.resource_demand.reference.{resource}",
        )
        for resource, value in _mapping(
            demand_namespaces.get("reference"),
            "application holdout.resource_demand.reference",
        ).items()
    }
    academic_demand = {
        resource: _integer(
            value,
            f"application holdout.resource_demand.academic.{resource}",
        )
        for resource, value in _mapping(
            demand_namespaces.get("academic"),
            "application holdout.resource_demand.academic",
        ).items()
    }
    fitted_resources = set(model["profiles"]["nominal"]["device_capacity"])
    if set(reference_demand) != fitted_resources or set(academic_demand) != fitted_resources:
        raise ValidationError(
            "application holdout resource_demand reference and academic namespaces "
            "must each cover every fitted resource"
        )
    observed = _mapping(observation.get("observed"), "application holdout.observed")
    observed_active = _integer(
        observed.get("active_fpga_count"),
        "application holdout.observed.active_fpga_count",
        minimum=1,
    )
    observed_cut_bits = _integer(
        observed.get("max_direction_cut_bits"),
        "application holdout.observed.max_direction_cut_bits",
        minimum=1,
    )
    observed_ratio = _integer(
        observed.get("max_tdm_ratio"),
        "application holdout.observed.max_tdm_ratio",
        minimum=1,
    )
    observed_delay = _number(
        observed.get("worst_cross_fpga_delay_ns"),
        "application holdout.observed.worst_cross_fpga_delay_ns",
        exclusive=True,
    )
    observed_hops = _integer(
        observed.get("worst_path_hop_count"),
        "application holdout.observed.worst_path_hop_count",
        minimum=1,
    )
    cross_paths = _integer(
        observed.get("cross_fpga_path_count"),
        "application holdout.observed.cross_fpga_path_count",
        minimum=1,
    )

    nominal = model["profiles"]["nominal"]
    reference_required_by_resource = {}
    for resource, amount in reference_demand.items():
        raw_capacity = nominal["reference_device_capacity"].get(resource)
        if raw_capacity is None:
            raise ValidationError(
                f"application holdout resource {resource!r} was not fitted"
            )
        effective = math.floor(raw_capacity * utilization_limit)
        if effective <= 0:
            raise ValidationError("application holdout effective capacity is zero")
        reference_required_by_resource[resource] = math.ceil(amount / effective)
    predicted_active = max(reference_required_by_resource.values(), default=1)
    if predicted_active > len(configuration["fpgas"]):
        raise ValidationError("application holdout does not fit the configuration")

    academic_required_by_resource = {}
    for resource, amount in academic_demand.items():
        raw_capacity = nominal["device_capacity"].get(resource)
        if raw_capacity is None:
            raise ValidationError(
                f"application holdout academic resource {resource!r} was not fitted"
            )
        effective = math.floor(raw_capacity * utilization_limit)
        if effective <= 0:
            raise ValidationError(
                "application holdout academic effective capacity is zero"
            )
        academic_required_by_resource[resource] = math.ceil(amount / effective)
    predicted_academic_active = max(
        academic_required_by_resource.values(), default=1
    )
    if predicted_academic_active > len(configuration["fpgas"]):
        raise ValidationError(
            "application holdout academic mapping does not fit the configuration"
        )

    mapping_errors = {}
    predicted_academic_demand = {}
    for resource in sorted(fitted_resources):
        mapping = model["calibration"]["resource_unit_mapping"][resource]
        predicted = reference_demand[resource] * float(
            mapping["academic_units_per_reference_unit"]
        )
        predicted_academic_demand[resource] = predicted
        observed_academic = academic_demand[resource]
        mapping_errors[resource] = (
            abs(predicted - observed_academic) / observed_academic
            if observed_academic
            else (0.0 if predicted == 0.0 else math.inf)
        )
    maximum_mapping_error = max(mapping_errors.values(), default=0.0)

    logical_capacity = int(nominal["link_payload_bits_per_cycle_per_direction"])
    raw_ratio = max(1, math.ceil(observed_cut_bits / logical_capacity))
    maximum_ratio = int(nominal["max_tdm_ratio"])
    predicted_ratio = raw_ratio if raw_ratio <= maximum_ratio else None
    if predicted_ratio is None:
        raise ValidationError("application holdout exceeds the characterized TDM domain")
    predicted_delay = _predict_delay(
        model,
        {
            "payload_bits": observed_cut_bits,
            "hop_count": observed_hops,
            "max_tdm_ratio": predicted_ratio,
            "contention_units": 0,
        },
    )
    ratio_error = abs(predicted_ratio - observed_ratio)
    delay_error = abs(predicted_delay - observed_delay) / observed_delay
    acceptance = model["acceptance"]
    resource_capacity_decisions_exact = (
        academic_required_by_resource == reference_required_by_resource
    )
    gates = {
        "active_fpga_count_exact": predicted_active == observed_active,
        "resource_capacity_decisions_exact": resource_capacity_decisions_exact,
        "tdm_ratio_absolute_error": ratio_error,
        "delay_relative_error": delay_error,
    }
    passed = (
        gates["active_fpga_count_exact"]
        and gates["resource_capacity_decisions_exact"]
        and ratio_error
        <= acceptance["application_tdm_ratio_absolute_error_max"]
        and delay_error <= acceptance["delay_max_relative_error_max"]
    )
    return {
        "schema": APPLICATION_VALIDATION_SCHEMA,
        "status": "pass" if passed else "fail",
        "model": model["model"]["name"],
        "dataset_id": dataset_id,
        "workload_id": workload_id,
        "source_sha256": source_sha256,
        "configuration": configuration_id,
        "utilization_limit": utilization_limit,
        "cross_fpga_path_count": cross_paths,
        "reference_required_fpgas_by_resource": reference_required_by_resource,
        "academic_required_fpgas_by_resource": academic_required_by_resource,
        "predicted_academic_resource_demand": predicted_academic_demand,
        "observed_academic_resource_demand": academic_demand,
        "predicted_active_fpga_count": predicted_active,
        "predicted_academic_active_fpga_count": predicted_academic_active,
        "observed_active_fpga_count": observed_active,
        "predicted_tdm_ratio": predicted_ratio,
        "observed_tdm_ratio": observed_ratio,
        "predicted_worst_cross_fpga_delay_ns": predicted_delay,
        "observed_worst_cross_fpga_delay_ns": observed_delay,
        "gates": gates,
        "diagnostics": {
            "resource_unit_mapping_relative_error": mapping_errors,
            "resource_unit_mapping_max_relative_error": maximum_mapping_error,
        },
        "thresholds": {
            "tdm_ratio_absolute_error_max": acceptance[
                "application_tdm_ratio_absolute_error_max"
            ],
            "delay_relative_error_max": acceptance[
                "delay_max_relative_error_max"
            ],
        },
    }


def validate_calibrated_partition_envelope(
    model_value: Mapping[str, Any], load_value: Mapping[str, Any]
) -> Dict[str, Any]:
    """Fail closed before routing when a partition exceeds calibrated service.

    This gate evaluates only the academic partition's offered communication
    load against fixed-assignment hardware characterization.  It deliberately
    does not compare the partition with a reference tool's free-partition
    result, because that would conflate hardware capacity with partitioner QoR.
    """

    model = validate_calibrated_platform_model(model_value)
    load = _mapping(load_value, "partition load")
    _reject_unknown(
        load,
        {
            "schema",
            "configuration",
            "partition_sha256",
            "max_direction_cut_bits",
            "worst_path_hop_count",
        },
        "partition load",
    )
    if load.get("schema") != PARTITION_LOAD_SCHEMA:
        raise ValidationError(
            f"partition load.schema: expected {PARTITION_LOAD_SCHEMA!r}"
        )
    configuration_id = _string(
        load.get("configuration"), "partition load.configuration"
    )
    if configuration_id not in {
        item["id"] for item in model["configurations"]
    }:
        raise ValidationError("partition load uses an unknown configuration")
    partition_sha256 = _string(
        load.get("partition_sha256"), "partition load.partition_sha256"
    )
    if len(partition_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in partition_sha256
    ):
        raise ValidationError("partition load.partition_sha256: expected SHA-256")
    cut_bits = _integer(
        load.get("max_direction_cut_bits"),
        "partition load.max_direction_cut_bits",
        minimum=1,
    )
    hops = _integer(
        load.get("worst_path_hop_count"),
        "partition load.worst_path_hop_count",
        minimum=1,
    )
    nominal = model["profiles"]["nominal"]
    logical_capacity = int(nominal["link_payload_bits_per_cycle_per_direction"])
    required_ratio = math.ceil(cut_bits / logical_capacity)
    provider_maximum = int(nominal["max_tdm_ratio"])
    delay_curve = model["calibration"]["link_delay_model"][
        "tdm_penalty_curve_ns"
    ]
    characterized_maximum = int(delay_curve[-1]["ratio"])
    selected = (
        required_ratio
        if required_ratio <= min(provider_maximum, characterized_maximum)
        else None
    )
    hop_supported = not (
        model["calibration"]["link_delay_model"]["hop_model_scope"]
        == "declared_one_hop_only"
        and hops != 1
    )
    status = "pass" if selected is not None and hop_supported else "fail"
    return {
        "schema": PARTITION_ENVELOPE_VALIDATION_SCHEMA,
        "status": status,
        "model": model["model"]["name"],
        "configuration": configuration_id,
        "partition_sha256": partition_sha256,
        "max_direction_cut_bits": cut_bits,
        "worst_path_hop_count": hops,
        "logical_bits_per_slot_per_direction": logical_capacity,
        "required_raw_tdm_ratio": required_ratio,
        "selected_characterized_tdm_ratio": selected,
        "maximum_characterized_tdm_ratio": characterized_maximum,
        "maximum_provider_tdm_ratio": provider_maximum,
        "reason": (
            "partition load lies inside the calibrated communication envelope"
            if status == "pass"
            else (
                "partition path hop count lies outside the one-hop-only "
                "calibrated platform"
                if not hop_supported
                else (
                    "partition load exceeds the provider TDM domain"
                    if required_ratio > provider_maximum
                    else "partition load lies outside the characterized TDM timing domain"
                )
            )
        ),
    }


def fit_calibrated_platform_files(
    template_path: Path, observations_path: Path, output_path: Path
) -> Dict[str, Any]:
    model = fit_calibrated_platform(read_json(template_path), read_json(observations_path))
    write_json(output_path, model)
    return {"status": "pass", "model": model["model"]["name"], "output": str(output_path)}


def materialize_calibrated_boarddb_file(
    model_path: Path,
    configuration_id: str,
    profile: str,
    output_path: Path,
    timing_output_path: Optional[Path] = None,
    utilization_limit: Optional[float] = None,
) -> Dict[str, Any]:
    model = read_json(model_path)
    boarddb = materialize_calibrated_boarddb(
        model,
        configuration_id,
        profile,
        utilization_limit=utilization_limit,
    )
    write_json(output_path, boarddb)
    result = {
        "status": "pass",
        "configuration": configuration_id,
        "profile": profile,
        "utilization_limit": boarddb["fpgas"][0]["utilization_limit"],
        "output": str(output_path),
    }
    if timing_output_path is not None:
        timing = materialize_calibrated_board_link_timing(
            model,
            configuration_id,
            profile,
            utilization_limit=utilization_limit,
        )
        write_json(timing_output_path, timing)
        result["timing_output"] = str(timing_output_path)
    return result


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


def validate_calibrated_platform_application_holdout_files(
    model_path: Path,
    observation_path: Path,
    output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    report = validate_calibrated_platform_application_holdout(
        read_json(model_path), read_json(observation_path)
    )
    if output_path is not None:
        write_json(output_path, report)
        report = {**report, "output": str(output_path)}
    return report


def validate_calibrated_partition_envelope_files(
    model_path: Path, load_path: Path, output_path: Optional[Path] = None
) -> Dict[str, Any]:
    report = validate_calibrated_partition_envelope(
        read_json(model_path), read_json(load_path)
    )
    if output_path is not None:
        write_json(output_path, report)
    return report
