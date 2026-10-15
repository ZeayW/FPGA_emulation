"""TDM-aware partition feedback and independent reconstruction."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import EmuFlowError, ValidationError
from .io import read_json, write_json
from .native_tools import resolve_native_executable
from .platform import Platform
from .phase3_storage import read_phase3_assignment_view
from .routing import semantic_contract_for_routes
from .tdm_ratio import validate_tdm_ratio_plan
from .tritonpart import PARTITION_NET_WEIGHTS_SCHEMA


PARTITION_FEEDBACK_PROVIDER = "channel-usage-pair-pressure-v1"
DAMPED_PARTITION_FEEDBACK_PROVIDER = "proximal-log-space-damping-v1"


def _routes_with_phase3_semantics(
    routes: Mapping[str, Any],
    assignment: Optional[Mapping[str, Any]],
) -> Mapping[str, Any]:
    """Resolve a compact Phase 4 binding against its Phase 3 owner."""

    if "semantic_contract" in routes:
        return routes
    has_schema = "semantic_contract_schema" in routes
    has_digest = "semantic_contract_sha256" in routes
    if not has_schema and not has_digest:
        return routes
    if has_schema != has_digest:
        raise ValidationError(
            "partition feedback route semantic-contract binding is incomplete"
        )
    if assignment is None:
        raise ValidationError(
            "Static Exact partition feedback requires the Phase 3 assignment"
        )
    contract = semantic_contract_for_routes(assignment, routes)
    if contract is None:
        raise ValidationError(
            "Static Exact partition feedback Phase 3 contract is missing"
        )
    return {**routes, "semantic_contract": contract}


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _model(
    routes: Mapping[str, Any],
    ratio_plan: Mapping[str, Any],
    pair_pressure_weight: float,
) -> Dict[str, Any]:
    route_nets = {
        route["net"]
        for route in routes.get("routes", [])
        if isinstance(route, dict) and isinstance(route.get("net"), str)
    }
    if not route_nets:
        raise ValidationError("partition feedback requires routed cut nets")
    ratio_by_net: Dict[str, int] = defaultdict(lambda: 1)
    for hop in ratio_plan.get("hops", []):
        net = hop.get("net")
        ratio = hop.get("discrete_ratio")
        if net not in route_nets or isinstance(ratio, bool) or not isinstance(
            ratio, int
        ):
            raise ValidationError("partition feedback ratio hop is invalid")
        ratio_by_net[net] = max(ratio_by_net[net], ratio)

    slack_by_net: Dict[str, float] = {}
    for path in ratio_plan.get("timing_paths", []):
        slack = path.get("normalized_slack")
        cut_nets = path.get("cut_nets")
        if (
            isinstance(slack, bool)
            or not isinstance(slack, (int, float))
            or not math.isfinite(float(slack))
            or not isinstance(cut_nets, list)
        ):
            raise ValidationError("partition feedback timing path is invalid")
        for net in cut_nets:
            if net not in route_nets:
                raise ValidationError(
                    f"partition feedback path references unknown net {net!r}"
                )
            slack_by_net[net] = min(
                slack_by_net.get(net, float("inf")), float(slack)
            )
    if not slack_by_net:
        raise ValidationError("partition feedback requires timing paths")
    minimum_slack = min(slack_by_net.values())
    maximum_slack = max(slack_by_net.values())

    configuration = ratio_plan.get("configuration")
    if not isinstance(configuration, dict):
        raise ValidationError("partition feedback ratio configuration missing")
    max_ratio = configuration.get("max_ratio")
    if isinstance(max_ratio, bool) or not isinstance(max_ratio, int) or max_ratio <= 0:
        raise ValidationError("partition feedback max ratio is invalid")
    alpha = 1.0 / max_ratio

    nets = []
    for index, net in enumerate(sorted(route_nets)):
        slack = slack_by_net.get(net, maximum_slack)
        criticality = (
            1.0
            if maximum_slack <= minimum_slack + 1.0e-12
            else (maximum_slack - slack) / (maximum_slack - minimum_slack)
        )
        nets.append(
            {
                "index": index,
                "net": net,
                "normalized_slack": slack,
                "criticality": criticality,
                "discrete_ratio": ratio_by_net[net],
            }
        )
    return {
        "max_ratio": max_ratio,
        "alpha": alpha,
        "pair_pressure_weight": pair_pressure_weight,
        "minimum_normalized_slack": minimum_slack,
        "maximum_normalized_slack": maximum_slack,
        "nets": nets,
    }


def _write_native_input(path: Path, model: Mapping[str, Any]) -> None:
    lines = [
        "EMUFLOW_PARTITION_FEEDBACK_INPUT_V1",
        (
            f"PARAM {model['max_ratio']} {model['alpha']:.17g} "
            f"{model['pair_pressure_weight']:.17g}"
        ),
    ]
    for net in model["nets"]:
        lines.append(
            f"NET {net['index']} {net['criticality']:.17g} "
            f"{net['discrete_ratio']}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_native_output(
    path: Path, model: Mapping[str, Any]
) -> Dict[str, Any]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "EMUFLOW_PARTITION_FEEDBACK_OUTPUT_V1":
        raise EmuFlowError("partition feedback returned an invalid header")
    records = {}
    metrics = {}
    for line in lines[1:]:
        fields = line.split()
        if fields[0] == "NET" and len(fields) == 6:
            index = int(fields[1])
            if index in records:
                raise EmuFlowError("partition feedback returned duplicate NET")
            records[index] = {
                "group_size": int(fields[2]),
                "channel_usage": float(fields[3]),
                "combined_usage": float(fields[4]),
                "weight": float(fields[5]),
            }
        elif fields[0] == "METRIC" and len(fields) == 3:
            if fields[1] in metrics:
                raise EmuFlowError("partition feedback returned duplicate metric")
            metrics[fields[1]] = float(fields[2])
        else:
            raise EmuFlowError(
                f"partition feedback returned malformed record: {line}"
            )
    if set(records) != set(range(len(model["nets"]))):
        raise EmuFlowError("partition feedback NET coverage is not exact")
    expected_metrics = {
        "objective_threshold",
        "minimum_combined_usage",
        "maximum_combined_usage",
        "maximum_feedback_weight",
    }
    if set(metrics) != expected_metrics:
        raise EmuFlowError("partition feedback metric coverage is not exact")
    return {
        "records": [records[index] for index in range(len(records))],
        "metrics": metrics,
    }


def build_partition_feedback(
    routes: Mapping[str, Any],
    ratio_plan: Mapping[str, Any],
    platform: Platform,
    *,
    assignment: Optional[Mapping[str, Any]] = None,
    executable: Optional[str] = None,
    pair_pressure_weight: float = 1.0,
) -> Dict[str, Any]:
    if (
        isinstance(pair_pressure_weight, bool)
        or not isinstance(pair_pressure_weight, (int, float))
        or not math.isfinite(float(pair_pressure_weight))
        or pair_pressure_weight < 0.0
    ):
        raise ValidationError(
            "partition feedback pair pressure weight must be non-negative"
        )
    resolved_routes = _routes_with_phase3_semantics(routes, assignment)
    validate_tdm_ratio_plan(resolved_routes, platform, ratio_plan)
    model = _model(
        resolved_routes, ratio_plan, float(pair_pressure_weight)
    )
    resolved = resolve_native_executable(
        "emuflow_tdm_partition_feedback", executable
    )
    with tempfile.TemporaryDirectory(
        prefix="emuflow-partition-feedback-"
    ) as temporary:
        root = Path(temporary)
        native_input = root / "feedback.in"
        native_output = root / "feedback.out"
        _write_native_input(native_input, model)
        completed = subprocess.run(
            [resolved, str(native_input), str(native_output)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise EmuFlowError(
                "in-tree partition feedback failed with exit code "
                f"{completed.returncode}: {detail}"
            )
        native = _parse_native_output(native_output, model)
    records = [
        {**net, **optimized}
        for net, optimized in zip(model["nets"], native["records"])
    ]
    artifact = {
        "schema": PARTITION_NET_WEIGHTS_SCHEMA,
        "design": resolved_routes.get("design"),
        "platform": platform.name,
        "provider": PARTITION_FEEDBACK_PROVIDER,
        "configuration": {
            "max_ratio": model["max_ratio"],
            "alpha": model["alpha"],
            "pair_pressure_weight": model["pair_pressure_weight"],
        },
        "slack_range": {
            "minimum": model["minimum_normalized_slack"],
            "maximum": model["maximum_normalized_slack"],
        },
        "weights": {
            record["net"]: record["weight"] for record in records
        },
        "records": records,
        "metrics": {
            **native["metrics"],
            "nets": len(records),
            "critical_nets": sum(
                record["criticality"] > 0.0 for record in records
            ),
        },
    }
    validate_partition_feedback(
        resolved_routes, ratio_plan, platform, artifact
    )
    return artifact


def validate_partition_feedback(
    routes: Mapping[str, Any],
    ratio_plan: Mapping[str, Any],
    platform: Platform,
    artifact: Mapping[str, Any],
    *,
    assignment: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    routes = _routes_with_phase3_semantics(routes, assignment)
    validate_tdm_ratio_plan(routes, platform, ratio_plan)
    if artifact.get("schema") != PARTITION_NET_WEIGHTS_SCHEMA:
        raise ValidationError("partition feedback schema is invalid")
    if artifact.get("provider") != PARTITION_FEEDBACK_PROVIDER:
        raise ValidationError("partition feedback provider is invalid")
    if artifact.get("design") != routes.get("design"):
        raise ValidationError("partition feedback design does not match routes")
    if artifact.get("platform") != platform.name:
        raise ValidationError("partition feedback platform does not match")
    configuration = artifact.get("configuration")
    if not isinstance(configuration, dict):
        raise ValidationError("partition feedback configuration is invalid")
    pair_pressure_weight = configuration.get("pair_pressure_weight")
    if (
        isinstance(pair_pressure_weight, bool)
        or not isinstance(pair_pressure_weight, (int, float))
        or not math.isfinite(float(pair_pressure_weight))
        or pair_pressure_weight < 0.0
    ):
        raise ValidationError(
            "partition feedback pair pressure weight is invalid"
        )
    model = _model(
        routes,
        ratio_plan,
        float(pair_pressure_weight),
    )
    if configuration != {
        "max_ratio": model["max_ratio"],
        "alpha": model["alpha"],
        "pair_pressure_weight": model["pair_pressure_weight"],
    }:
        raise ValidationError("partition feedback configuration mismatch")
    expected_slack_range = {
        "minimum": model["minimum_normalized_slack"],
        "maximum": model["maximum_normalized_slack"],
    }
    if artifact.get("slack_range") != expected_slack_range:
        raise ValidationError("partition feedback slack range mismatch")
    records = artifact.get("records")
    weights = artifact.get("weights")
    if (
        not isinstance(records, list)
        or len(records) != len(model["nets"])
        or not isinstance(weights, dict)
    ):
        raise ValidationError("partition feedback coverage is not exact")

    threshold = max(
        net["criticality"] + model["alpha"] * net["discrete_ratio"]
        for net in model["nets"]
    )
    reconstructed = []
    minimum_combined = float("inf")
    maximum_combined = 0.0
    for net in model["nets"]:
        group_size = max(
            1,
            min(
                model["max_ratio"],
                math.floor(
                    (threshold - net["criticality"] + 1.0e-12)
                    / model["alpha"]
                ),
            ),
        )
        usage = 1.0 / group_size
        pressure = net["discrete_ratio"] / model["max_ratio"]
        combined = usage * (
            1.0 + model["pair_pressure_weight"] * pressure
        )
        minimum_combined = min(minimum_combined, combined)
        maximum_combined = max(maximum_combined, combined)
        reconstructed.append((group_size, usage, combined))
    for expected, actual, values in zip(
        model["nets"], records, reconstructed
    ):
        for key, value in expected.items():
            if actual.get(key) != value:
                raise ValidationError(
                    f"partition feedback record {expected['index']}.{key} "
                    "does not match source artifacts"
                )
        group_size, usage, combined = values
        weight = combined / minimum_combined
        for key, value in (
            ("group_size", group_size),
            ("channel_usage", usage),
            ("combined_usage", combined),
            ("weight", weight),
        ):
            actual_value = actual.get(key)
            if (
                isinstance(actual_value, bool)
                or not isinstance(actual_value, (int, float))
                or not math.isfinite(float(actual_value))
                or abs(float(actual_value) - value) > 1.0e-9
            ):
                raise ValidationError(
                    f"partition feedback record {expected['index']}.{key} "
                    "does not match independent reconstruction"
                )
        actual_weight = weights.get(expected["net"])
        if (
            isinstance(actual_weight, bool)
            or not isinstance(actual_weight, (int, float))
            or not math.isfinite(float(actual_weight))
            or abs(float(actual_weight) - weight) > 1.0e-9
        ):
            raise ValidationError(
                f"partition feedback weight {expected['net']!r} mismatch"
            )
    if set(weights) != {net["net"] for net in model["nets"]}:
        raise ValidationError("partition feedback weight coverage is not exact")
    expected_metrics = {
        "objective_threshold": threshold,
        "minimum_combined_usage": minimum_combined,
        "maximum_combined_usage": maximum_combined,
        "maximum_feedback_weight": maximum_combined / minimum_combined,
        "nets": len(records),
        "critical_nets": sum(net["criticality"] > 0.0 for net in model["nets"]),
    }
    metrics = artifact.get("metrics")
    if not isinstance(metrics, dict):
        raise ValidationError("partition feedback metrics are invalid")
    for key, value in expected_metrics.items():
        actual = metrics.get(key)
        if isinstance(value, int):
            if actual != value:
                raise ValidationError(f"partition feedback metric {key} mismatch")
        elif (
            isinstance(actual, bool)
            or not isinstance(actual, (int, float))
            or not math.isfinite(float(actual))
            or abs(float(actual) - value) > 1.0e-9
        ):
            raise ValidationError(f"partition feedback metric {key} mismatch")
    return {
        "status": "pass",
        "provider": PARTITION_FEEDBACK_PROVIDER,
        "nets": len(records),
        "critical_nets": expected_metrics["critical_nets"],
        "maximum_feedback_weight": expected_metrics[
            "maximum_feedback_weight"
        ],
    }


def run_partition_feedback(
    routes_path: Path,
    ratio_plan_path: Path,
    platform_path: Path,
    output_path: Path,
    *,
    assignment_path: Optional[Path] = None,
    executable: Optional[str] = None,
    pair_pressure_weight: float = 1.0,
) -> Dict[str, Any]:
    routes = read_json(routes_path)
    ratio_plan = read_json(ratio_plan_path)
    assignment = (
        read_phase3_assignment_view(assignment_path)
        if assignment_path is not None
        else None
    )
    platform = Platform.load(platform_path)
    artifact = build_partition_feedback(
        routes,
        ratio_plan,
        platform,
        assignment=assignment,
        executable=executable,
        pair_pressure_weight=pair_pressure_weight,
    )
    write_json(output_path, artifact)
    return validate_partition_feedback(
        routes,
        ratio_plan,
        platform,
        artifact,
        assignment=assignment,
    )


def build_damped_partition_feedback(
    raw_feedback: Mapping[str, Any],
    step_size: float,
) -> Dict[str, Any]:
    """Interpolate unit edge costs and raw feedback in log space.

    ``weight = exp(step_size * log(raw_weight))`` is the multiplicative
    mirror-descent/proximal update.  It preserves positivity, is exactly the
    identity at a unit step, and converges continuously to unweighted
    partitioning as the step approaches zero.
    """

    if (
        isinstance(step_size, bool)
        or not isinstance(step_size, (int, float))
        or not math.isfinite(float(step_size))
        or float(step_size) <= 0.0
        or float(step_size) > 1.0
    ):
        raise ValidationError(
            "damped partition feedback step size must be in (0, 1]"
        )
    if (
        raw_feedback.get("schema") != PARTITION_NET_WEIGHTS_SCHEMA
        or raw_feedback.get("provider") != PARTITION_FEEDBACK_PROVIDER
    ):
        raise ValidationError(
            "damped partition feedback requires checked raw feedback"
        )
    raw_records = raw_feedback.get("records")
    raw_weights = raw_feedback.get("weights")
    if (
        not isinstance(raw_records, list)
        or not raw_records
        or not isinstance(raw_weights, dict)
    ):
        raise ValidationError("raw partition feedback coverage is invalid")

    step = float(step_size)
    records = []
    weights = {}
    for index, raw in enumerate(raw_records):
        if not isinstance(raw, dict):
            raise ValidationError(
                f"raw partition feedback records[{index}] is invalid"
            )
        net = raw.get("net")
        raw_weight = raw.get("weight")
        if (
            not isinstance(net, str)
            or not net
            or net in weights
            or isinstance(raw_weight, bool)
            or not isinstance(raw_weight, (int, float))
            or not math.isfinite(float(raw_weight))
            or float(raw_weight) <= 0.0
            or raw_weights.get(net) != raw_weight
        ):
            raise ValidationError(
                f"raw partition feedback records[{index}] is invalid"
            )
        damped = math.exp(step * math.log(float(raw_weight)))
        records.append(
            {
                **raw,
                "raw_weight": float(raw_weight),
                "weight": damped,
            }
        )
        weights[net] = damped
    if set(weights) != set(raw_weights):
        raise ValidationError(
            "raw partition feedback weight coverage is not exact"
        )
    values = list(weights.values())
    artifact = {
        "schema": PARTITION_NET_WEIGHTS_SCHEMA,
        "design": raw_feedback.get("design"),
        "platform": raw_feedback.get("platform"),
        "provider": DAMPED_PARTITION_FEEDBACK_PROVIDER,
        "configuration": {
            "step_size": step,
            "interpolation": "exp(step_size*log(raw_weight))",
            "raw_provider": PARTITION_FEEDBACK_PROVIDER,
            "raw_feedback_sha256": _canonical_digest(raw_feedback),
        },
        "slack_range": raw_feedback.get("slack_range"),
        "weights": weights,
        "records": records,
        "metrics": {
            "nets": len(records),
            "critical_nets": sum(
                float(record["criticality"]) > 0.0
                for record in records
            ),
            "minimum_raw_weight": min(
                float(record["raw_weight"]) for record in records
            ),
            "maximum_raw_weight": max(
                float(record["raw_weight"]) for record in records
            ),
            "minimum_feedback_weight": min(values),
            "maximum_feedback_weight": max(values),
        },
    }
    validate_damped_partition_feedback(raw_feedback, artifact)
    return artifact


def validate_damped_partition_feedback(
    raw_feedback: Mapping[str, Any],
    artifact: Mapping[str, Any],
) -> Dict[str, Any]:
    if artifact.get("schema") != PARTITION_NET_WEIGHTS_SCHEMA:
        raise ValidationError("damped partition feedback schema is invalid")
    if artifact.get("provider") != DAMPED_PARTITION_FEEDBACK_PROVIDER:
        raise ValidationError("damped partition feedback provider is invalid")
    for key in ("design", "platform", "slack_range"):
        if artifact.get(key) != raw_feedback.get(key):
            raise ValidationError(
                f"damped partition feedback {key} mismatch"
            )
    configuration = artifact.get("configuration")
    if not isinstance(configuration, dict):
        raise ValidationError(
            "damped partition feedback configuration is invalid"
        )
    step = configuration.get("step_size")
    if (
        isinstance(step, bool)
        or not isinstance(step, (int, float))
        or not math.isfinite(float(step))
        or float(step) <= 0.0
        or float(step) > 1.0
    ):
        raise ValidationError(
            "damped partition feedback step size is invalid"
        )
    expected_configuration = {
        "step_size": float(step),
        "interpolation": "exp(step_size*log(raw_weight))",
        "raw_provider": PARTITION_FEEDBACK_PROVIDER,
        "raw_feedback_sha256": _canonical_digest(raw_feedback),
    }
    if configuration != expected_configuration:
        raise ValidationError(
            "damped partition feedback configuration mismatch"
        )
    raw_records = raw_feedback.get("records")
    records = artifact.get("records")
    weights = artifact.get("weights")
    if (
        not isinstance(raw_records, list)
        or not isinstance(records, list)
        or len(records) != len(raw_records)
        or not isinstance(weights, dict)
    ):
        raise ValidationError(
            "damped partition feedback coverage is invalid"
        )
    expected_weights = {}
    for index, (raw, record) in enumerate(zip(raw_records, records)):
        net = raw["net"]
        raw_weight = float(raw["weight"])
        damped = math.exp(float(step) * math.log(raw_weight))
        expected = {
            **raw,
            "raw_weight": raw_weight,
            "weight": damped,
        }
        if record != expected or weights.get(net) != damped:
            raise ValidationError(
                f"damped partition feedback record {index} mismatch"
            )
        expected_weights[net] = damped
    if weights != expected_weights:
        raise ValidationError(
            "damped partition feedback weights mismatch"
        )
    values = list(expected_weights.values())
    expected_metrics = {
        "nets": len(records),
        "critical_nets": sum(
            float(record["criticality"]) > 0.0 for record in records
        ),
        "minimum_raw_weight": min(
            float(record["raw_weight"]) for record in records
        ),
        "maximum_raw_weight": max(
            float(record["raw_weight"]) for record in records
        ),
        "minimum_feedback_weight": min(values),
        "maximum_feedback_weight": max(values),
    }
    if artifact.get("metrics") != expected_metrics:
        raise ValidationError(
            "damped partition feedback metrics mismatch"
        )
    return {
        "status": "pass",
        "provider": DAMPED_PARTITION_FEEDBACK_PROVIDER,
        "step_size": float(step),
        "nets": len(records),
        "maximum_feedback_weight": max(values),
    }


def run_damped_partition_feedback(
    raw_feedback_path: Path,
    output_path: Path,
    *,
    step_size: float,
) -> Dict[str, Any]:
    raw = read_json(raw_feedback_path)
    artifact = build_damped_partition_feedback(raw, step_size)
    write_json(output_path, artifact)
    return validate_damped_partition_feedback(raw, artifact)
