"""Source-bound Phase-7 path residuals for iterative Phase-3 refinement."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, Mapping

from .errors import ValidationError


PARTITION_PHYSICAL_FEEDBACK_SCHEMA = (
    "emuflow.partition-physical-feedback/v1"
)
PARTITION_PHYSICAL_FEEDBACK_PROVIDER = (
    "phase7-endpoint-pair-positive-residual-v1"
)


def _digest(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _finite(value: Any, context: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValidationError(f"{context} must be finite")
    return float(value)


def _sha256(value: Any, context: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValidationError(f"{context} must be a lowercase SHA-256")
    return value


def _reconstruct_partition_physical_feedback(
    model: Mapping[str, Any],
    observed_assignment: Mapping[str, Any],
    system_timing: Mapping[str, Any],
) -> Dict[str, Any]:
    if model.get("schema") != "emuflow.partition-pressure-model/v6":
        raise ValidationError("physical feedback pressure model is invalid")
    if observed_assignment.get("schema") != "emuflow.partition-assignment/v1":
        raise ValidationError("physical feedback assignment is invalid")
    if system_timing.get("schema") != "emuflow.system-timing/v2":
        raise ValidationError("physical feedback system timing is invalid")
    design = model.get("design")
    platform = model.get("platform")
    if (
        observed_assignment.get("design") != design
        or observed_assignment.get("platform") != platform
        or system_timing.get("design") != design
        or system_timing.get("platform") != platform
    ):
        raise ValidationError("physical feedback source identities disagree")

    clusters = {item["cluster"] for item in model.get("clusters", [])}
    fpgas = {item["fpga"] for item in model.get("fpgas", [])}
    assignment = observed_assignment.get("cluster_assignment")
    if (
        not isinstance(assignment, Mapping)
        or set(assignment) != clusters
        or any(part not in fpgas for part in assignment.values())
    ):
        raise ValidationError("physical feedback assignment coverage is invalid")

    model_paths = model.get("paths")
    timing_paths = system_timing.get("paths")
    if not isinstance(model_paths, list) or not isinstance(timing_paths, list):
        raise ValidationError("physical feedback path population is invalid")
    model_by_path = {item.get("path"): item for item in model_paths}
    timing_by_path = {item.get("path"): item for item in timing_paths}
    if (
        None in model_by_path
        or None in timing_by_path
        or len(model_by_path) != len(model_paths)
        or len(timing_by_path) != len(timing_paths)
        or set(model_by_path) != set(timing_by_path)
    ):
        raise ValidationError(
            "physical feedback must cover the complete pressure path population"
        )

    records = []
    cross_paths = 0
    raw_positive_total = 0.0
    maximum_residual = 0.0
    for index, pressure_path in enumerate(model_paths):
        path_id = pressure_path["path"]
        routed = timing_by_path[path_id]
        scope = routed.get("path_scope")
        if scope == "same-fpga-local":
            continue
        if scope != "cross-fpga":
            raise ValidationError(
                f"physical feedback path {path_id!r} has invalid scope"
            )
        cross_paths += 1
        sequence = routed.get("logical_fpga_sequence")
        if (
            not routed.get("partition_chain_exact")
            or not isinstance(sequence, list)
            or len(sequence) < 2
            or any(part not in fpgas for part in sequence)
            or sequence[0] == sequence[-1]
        ):
            raise ValidationError(
                f"physical feedback path {path_id!r} is not endpoint-pair exact"
            )
        start = pressure_path.get("start_cluster")
        end = pressure_path.get("end_cluster")
        if (
            start not in assignment
            or end not in assignment
            or assignment[start] != sequence[0]
            or assignment[end] != sequence[-1]
        ):
            raise ValidationError(
                f"physical feedback path {path_id!r} disagrees with assignment"
            )
        period = _finite(
            pressure_path.get("clock_period_ns"),
            f"physical feedback path {path_id} period",
        )
        routed_period = _finite(
            routed.get("target_period_ns"),
            f"physical feedback path {path_id} routed period",
        )
        if not math.isclose(period, routed_period, rel_tol=0.0, abs_tol=1.0e-8):
            raise ValidationError(
                f"physical feedback path {path_id!r} period disagrees"
            )
        base_slack = _finite(
            pressure_path.get("base_slack_ns"),
            f"physical feedback path {path_id} base slack",
        )
        predicted_logic = period - base_slack
        physical_logic = _finite(
            routed.get("physical_logic_delay_bound_ns"),
            f"physical feedback path {path_id} logic delay",
        )
        physical_interface = _finite(
            routed.get("physical_interface_delay_bound_ns"),
            f"physical feedback path {path_id} interface delay",
        )
        preplacement_fixed = _finite(
            routed.get("preplacement_fixed_delay_ns"),
            f"physical feedback path {path_id} fixed delay",
        )
        values = (predicted_logic, physical_logic, physical_interface,
                  preplacement_fixed)
        if any(value < 0.0 for value in values):
            raise ValidationError(
                f"physical feedback path {path_id!r} has negative delay"
            )
        logic_residual = physical_logic - predicted_logic
        interface_residual = physical_interface - preplacement_fixed
        residual = max(0.0, logic_residual + interface_residual)
        if residual == 0.0:
            continue
        raw_positive_total += residual
        maximum_residual = max(maximum_residual, residual)
        records.append(
            {
                "index": index,
                "path": path_id,
                "observed_source_fpga": sequence[0],
                "observed_sink_fpga": sequence[-1],
                "positive_residual_ns": residual,
                "logic_residual_ns": logic_residual,
                "interface_residual_ns": interface_residual,
            }
        )

    return {
        "schema": PARTITION_PHYSICAL_FEEDBACK_SCHEMA,
        "provider": PARTITION_PHYSICAL_FEEDBACK_PROVIDER,
        "design": design,
        "platform": platform,
        "matching": "exact-observed-endpoint-pair-v1",
        "residual_model": (
            "max(0,physical_logic-predicted_logic+"
            "physical_interface-preplacement_fixed)-v1"
        ),
        "source_model_sha256": _digest(model),
        "source_assignment_sha256": _digest(observed_assignment),
        "source_system_timing_sha256": _digest(system_timing),
        "paths": records,
        "metrics": {
            "source_paths": len(model_paths),
            "cross_fpga_paths": cross_paths,
            "positive_residual_paths": len(records),
            "mean_positive_residual_ns": (
                raw_positive_total / len(records) if records else 0.0
            ),
            "maximum_positive_residual_ns": maximum_residual,
        },
    }


def build_partition_physical_feedback(
    model: Mapping[str, Any],
    observed_assignment: Mapping[str, Any],
    system_timing: Mapping[str, Any],
) -> Dict[str, Any]:
    """Build positive path residuals from one checked physical iteration."""

    return _reconstruct_partition_physical_feedback(
        model, observed_assignment, system_timing
    )


def validate_partition_physical_feedback(
    model: Mapping[str, Any],
    observed_assignment: Mapping[str, Any],
    system_timing: Mapping[str, Any],
    feedback: Mapping[str, Any],
) -> Dict[str, Any]:
    expected = _reconstruct_partition_physical_feedback(
        model, observed_assignment, system_timing
    )
    if feedback != expected:
        raise ValidationError(
            "partition physical feedback differs from independent reconstruction"
        )
    return {"status": "pass", **expected["metrics"]}


def validate_partition_physical_feedback_seal(
    model: Mapping[str, Any],
    feedback: Mapping[str, Any],
) -> Dict[str, Any]:
    """Validate the immutable subset consumed by the native optimizer."""

    if (
        feedback.get("schema") != PARTITION_PHYSICAL_FEEDBACK_SCHEMA
        or feedback.get("provider") != PARTITION_PHYSICAL_FEEDBACK_PROVIDER
        or feedback.get("design") != model.get("design")
        or feedback.get("platform") != model.get("platform")
        or feedback.get("source_model_sha256") != _digest(model)
        or feedback.get("matching") != "exact-observed-endpoint-pair-v1"
        or feedback.get("residual_model")
        != (
            "max(0,physical_logic-predicted_logic+"
            "physical_interface-preplacement_fixed)-v1"
        )
    ):
        raise ValidationError("partition physical feedback seal is invalid")
    _sha256(
        feedback.get("source_assignment_sha256"),
        "partition physical feedback assignment seal",
    )
    _sha256(
        feedback.get("source_system_timing_sha256"),
        "partition physical feedback system timing seal",
    )
    paths = feedback.get("paths")
    if not isinstance(paths, list):
        raise ValidationError("partition physical feedback paths are invalid")
    model_paths = model.get("paths", [])
    parts = {item["fpga"] for item in model.get("fpgas", [])}
    seen = set()
    for record in paths:
        index = record.get("index")
        residual = _finite(
            record.get("positive_residual_ns"),
            "partition physical feedback residual",
        )
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or index >= len(model_paths)
            or index in seen
            or record.get("path") != model_paths[index].get("path")
            or record.get("observed_source_fpga") not in parts
            or record.get("observed_sink_fpga") not in parts
            or record.get("observed_source_fpga")
            == record.get("observed_sink_fpga")
            or residual <= 0.0
        ):
            raise ValidationError(
                "partition physical feedback path seal is invalid"
            )
        seen.add(index)
    return {
        "status": "pass",
        "positive_residual_paths": len(paths),
        "source_system_timing_sha256": feedback.get(
            "source_system_timing_sha256"
        ),
    }
