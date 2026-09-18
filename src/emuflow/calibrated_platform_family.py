"""Qualified families of behaviorally calibrated academic platforms.

A family is a catalog of independently admitted platform specifications.  It
does not synthesize a topology from an FPGA count.  Selection is deliberately
limited to a resource-capacity prefilter over specifications whose blind
holdout, application holdout, and one-shot Phase 1--7 evidence all pass.
Partition balance and communication feasibility remain authoritative later
gates in the normal flow.
"""

from __future__ import annotations

import hashlib
import math
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .calibrated_platform import (
    APPLICATION_VALIDATION_SCHEMA,
    MODEL_SCHEMA,
    VALIDATION_SCHEMA,
    materialize_calibrated_board_link_timing,
    materialize_calibrated_boarddb,
    validate_calibrated_platform_model,
)
from .errors import ValidationError
from .io import read_json, write_json
from .multi_fpga_flow import validate_multi_fpga_flow_report
from .runtime import QOR_REPORT_SCHEMA


FAMILY_SCHEMA = "emuflow.calibrated-platform-family/v1"
DEMAND_SCHEMA = "emuflow.calibrated-platform-design-demand/v1"
SELECTION_SCHEMA = "emuflow.calibrated-platform-selection/v1"
FULL_FLOW_SCHEMA = "emuflow.calibrated-platform-full-flow-acceptance/v1"
QUALIFICATION_SPEC_SCHEMA = (
    "emuflow.calibrated-platform-family-qualification-spec/v1"
)
_PROFILES = {"conservative", "nominal", "aggressive"}
_RESOURCES = {"lut", "ff", "bram", "dsp"}


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


def _integer(value: Any, context: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValidationError(f"{context}: expected an integer >= {minimum}")
    return value


def _number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{context}: expected a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(f"{context}: expected a finite number")
    return result


def _sha256(value: Any, context: str) -> str:
    digest = _string(value, context)
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValidationError(f"{context}: expected lowercase SHA-256")
    return digest


def _reject_unknown(
    value: Mapping[str, Any], allowed: set[str], context: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValidationError(f"{context}: unknown fields {unknown}")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative_file(root: Path, raw: Any, context: str) -> Path:
    text = _string(raw, context)
    candidate = Path(text)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValidationError(f"{context}: expected a contained relative path")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValidationError(f"{context}: path escapes the family directory") from exc
    if not resolved.is_file():
        raise ValidationError(f"{context}: file does not exist: {text}")
    return resolved


def validate_full_flow_acceptance(value: Mapping[str, Any]) -> Dict[str, Any]:
    root = _mapping(value, "full-flow acceptance")
    _reject_unknown(
        root,
        {
            "schema",
            "status",
            "model",
            "model_sha256",
            "flow_report_sha256",
            "qor_report_sha256",
            "configuration",
            "profile",
            "utilization_limit",
            "workload",
            "workload_sha256",
            "physical_seed",
            "completed_phases",
            "checks",
            "timing",
        },
        "full-flow acceptance",
    )
    if root.get("schema") != FULL_FLOW_SCHEMA:
        raise ValidationError(
            f"full-flow acceptance.schema: expected {FULL_FLOW_SCHEMA!r}"
        )
    if root.get("status") != "pass":
        raise ValidationError("full-flow acceptance must have status 'pass'")
    profile = _string(root.get("profile"), "full-flow acceptance.profile")
    if profile not in _PROFILES:
        raise ValidationError("full-flow acceptance.profile: unsupported profile")
    utilization_limit = _number(
        root.get("utilization_limit"), "full-flow acceptance.utilization_limit"
    )
    if utilization_limit <= 0.0 or utilization_limit > 1.0:
        raise ValidationError(
            "full-flow acceptance.utilization_limit: expected 0 < value <= 1"
        )
    phases = [
        _integer(item, "full-flow acceptance.completed_phases", minimum=1)
        for item in _array(
            root.get("completed_phases"),
            "full-flow acceptance.completed_phases",
            nonempty=True,
        )
    ]
    if phases != list(range(1, 8)):
        raise ValidationError("full-flow acceptance must complete phases 1 through 7")
    checks = _mapping(root.get("checks"), "full-flow acceptance.checks")
    _reject_unknown(
        checks,
        {
            "macro_cycle_equivalence",
            "schedule_legality",
            "drc_violations",
            "unrouted_nets",
            "phase7c_path_coverage",
        },
        "full-flow acceptance.checks",
    )
    if checks.get("macro_cycle_equivalence") != "pass":
        raise ValidationError("full-flow acceptance macro-cycle equivalence failed")
    if checks.get("schedule_legality") != "pass":
        raise ValidationError("full-flow acceptance schedule legality failed")
    if _integer(checks.get("drc_violations"), "drc_violations") != 0:
        raise ValidationError("full-flow acceptance has DRC violations")
    if _integer(checks.get("unrouted_nets"), "unrouted_nets") != 0:
        raise ValidationError("full-flow acceptance has unrouted nets")
    coverage = _number(checks.get("phase7c_path_coverage"), "phase7c_path_coverage")
    if not math.isclose(coverage, 1.0, rel_tol=0.0, abs_tol=1.0e-12):
        raise ValidationError("full-flow acceptance requires complete path coverage")
    timing = _mapping(root.get("timing"), "full-flow acceptance.timing")
    _reject_unknown(
        timing,
        {"engine", "scope", "wns_ns", "tns_ns"},
        "full-flow acceptance.timing",
    )
    if timing.get("engine") != "opensta" or timing.get("scope") != "system_global":
        raise ValidationError(
            "full-flow acceptance requires independent system-global OpenSTA timing"
        )
    physical_seed = _integer(
        root.get("physical_seed"), "full-flow acceptance.physical_seed", minimum=1
    )
    if physical_seed != 1:
        raise ValidationError("full-flow acceptance requires physical seed 1")
    return {
        "schema": FULL_FLOW_SCHEMA,
        "status": "pass",
        "model": _string(root.get("model"), "full-flow acceptance.model"),
        "model_sha256": _sha256(
            root.get("model_sha256"), "full-flow acceptance.model_sha256"
        ),
        "flow_report_sha256": _sha256(
            root.get("flow_report_sha256"),
            "full-flow acceptance.flow_report_sha256",
        ),
        "qor_report_sha256": _sha256(
            root.get("qor_report_sha256"),
            "full-flow acceptance.qor_report_sha256",
        ),
        "configuration": _string(
            root.get("configuration"), "full-flow acceptance.configuration"
        ),
        "profile": profile,
        "utilization_limit": utilization_limit,
        "workload": _string(root.get("workload"), "full-flow acceptance.workload"),
        "workload_sha256": _sha256(
            root.get("workload_sha256"), "full-flow acceptance.workload_sha256"
        ),
        "physical_seed": physical_seed,
        "completed_phases": phases,
        "checks": {
            "macro_cycle_equivalence": "pass",
            "schedule_legality": "pass",
            "drc_violations": 0,
            "unrouted_nets": 0,
            "phase7c_path_coverage": coverage,
        },
        "timing": {
            "engine": "opensta",
            "scope": "system_global",
            "wns_ns": _number(timing.get("wns_ns"), "full-flow acceptance.wns_ns"),
            "tns_ns": _number(timing.get("tns_ns"), "full-flow acceptance.tns_ns"),
        },
    }


def build_full_flow_acceptance_files(
    *,
    model_path: Path,
    configuration: str,
    profile: str,
    utilization_limit: float,
    workload: str,
    workload_path: Path,
    flow_root: Path,
    output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Derive admission evidence from one completed physical flow.

    The caller cannot assert closure fields.  They are reconstructed from the
    canonical flow report and its hash-sealed QoR artifact.  This is the only
    producer for real family admission evidence; ``validate_full_flow_acceptance``
    remains the small standalone consumer used after scratch cleanup.
    """

    model_path = model_path.resolve()
    workload_path = workload_path.resolve()
    flow_root = flow_root.resolve()
    if not model_path.is_file():
        raise ValidationError("full-flow acceptance model file is missing")
    if not workload_path.is_file():
        raise ValidationError("full-flow acceptance workload file is missing")
    if not flow_root.is_dir():
        raise ValidationError("full-flow acceptance flow root is missing")

    model = validate_calibrated_platform_model(read_json(model_path))
    if configuration not in {item["id"] for item in model["configurations"]}:
        raise ValidationError("full-flow acceptance model configuration is unknown")
    if profile not in _PROFILES:
        raise ValidationError("full-flow acceptance profile is unsupported")
    declared_limit = float(model["device"]["utilization_limit"])
    if utilization_limit <= 0.0 or utilization_limit > declared_limit:
        raise ValidationError(
            "full-flow acceptance utilization limit exceeds the model"
        )

    flow_report_path = flow_root / "multi-fpga-flow-report.json"
    if flow_report_path.is_symlink() or not flow_report_path.is_file():
        raise ValidationError("full-flow acceptance canonical flow report is missing")
    flow_report = _mapping(read_json(flow_report_path), "multi-FPGA flow report")
    flow_validation = validate_multi_fpga_flow_report(dict(flow_report))
    if flow_validation.get("physical_status") != "pass":
        raise ValidationError("full-flow acceptance requires completed Phase 7")

    expected_boarddb = materialize_calibrated_boarddb(
        model,
        configuration,
        profile,
        utilization_limit=utilization_limit,
    )
    expected_platform = expected_boarddb["platform"]["name"]
    if flow_validation.get("platform") != expected_platform:
        raise ValidationError(
            "full-flow acceptance flow did not use the calibrated platform"
        )

    physical = _mapping(flow_report.get("physical"), "full-flow physical report")
    execution = _mapping(
        physical.get("execution"), "full-flow physical execution"
    )
    seed = _integer(
        execution.get("seed"), "full-flow physical execution.seed", minimum=1
    )
    if seed != 1:
        raise ValidationError("full-flow acceptance requires physical seed 1")

    artifacts = _mapping(flow_report.get("artifacts"), "full-flow artifacts")
    qor_ref = _mapping(artifacts.get("qor_report"), "full-flow QoR artifact")
    relative_qor = Path(_string(qor_ref.get("path"), "full-flow QoR path"))
    if relative_qor.is_absolute() or ".." in relative_qor.parts:
        raise ValidationError("full-flow QoR path is not contained")
    qor_candidate = flow_root / relative_qor
    if qor_candidate.is_symlink() or not qor_candidate.is_file():
        raise ValidationError("full-flow QoR artifact is missing or is a symlink")
    qor_path = qor_candidate.resolve()
    if qor_path.parent != flow_root and flow_root not in qor_path.parents:
        raise ValidationError("full-flow QoR artifact escapes its root")
    qor_digest = _file_sha256(qor_path)
    if qor_digest != _sha256(qor_ref.get("sha256"), "full-flow QoR SHA-256"):
        raise ValidationError("full-flow QoR artifact SHA-256 disagrees")
    qor = _mapping(read_json(qor_path), "full-flow QoR report")
    if (
        qor.get("schema") != QOR_REPORT_SCHEMA
        or qor.get("status") != "pass"
        or qor.get("design") != flow_validation.get("design")
        or qor.get("platform") != expected_platform
    ):
        raise ValidationError("full-flow QoR identity or status is invalid")

    equivalence = _mapping(qor.get("equivalence"), "full-flow equivalence")
    if equivalence.get("mismatches") != 0:
        raise ValidationError("full-flow macro-cycle equivalence failed")
    tdm_validation = _mapping(
        _mapping(flow_report["stages"].get("tdm"), "full-flow Phase 5").get(
            "validation"
        ),
        "full-flow Phase 5 validation",
    )
    if tdm_validation.get("status") != "pass":
        raise ValidationError("full-flow schedule legality failed")

    physical_qor = _mapping(qor.get("physical"), "full-flow physical QoR")
    if (
        physical_qor.get("status") != "pass"
        or physical_qor.get("drc_violations") != 0
        or physical_qor.get("unrouted_nets") != 0
    ):
        raise ValidationError("full-flow physical closure failed")
    timing = _mapping(qor.get("timing"), "full-flow system timing")
    global_opensta = _mapping(
        timing.get("global_opensta"), "full-flow global OpenSTA"
    )
    summary = _mapping(timing.get("summary"), "full-flow timing summary")
    target_clock = _mapping(
        timing.get("target_clock"), "full-flow target clock"
    )
    coverage = _number(
        summary.get("original_path_coverage"),
        "full-flow original path coverage",
    )
    if (
        timing.get("status") != "pass"
        or timing.get("timing_scope") != "whole-original-design"
        or global_opensta.get("authority") != "opensta"
        or global_opensta.get("execution") != "standalone"
        or global_opensta.get("status") != "pass"
        or global_opensta.get("timing_scope") != "whole-original-design"
        or not math.isclose(coverage, 1.0, rel_tol=0.0, abs_tol=1.0e-12)
    ):
        raise ValidationError(
            "full-flow acceptance requires complete standalone global OpenSTA"
        )

    acceptance = validate_full_flow_acceptance(
        {
            "schema": FULL_FLOW_SCHEMA,
            "status": "pass",
            "model": model["model"]["name"],
            "model_sha256": _file_sha256(model_path),
            "flow_report_sha256": _file_sha256(flow_report_path),
            "qor_report_sha256": qor_digest,
            "configuration": configuration,
            "profile": profile,
            "utilization_limit": float(utilization_limit),
            "workload": _string(workload, "full-flow workload"),
            "workload_sha256": _file_sha256(workload_path),
            "physical_seed": seed,
            "completed_phases": list(range(1, 8)),
            "checks": {
                "macro_cycle_equivalence": "pass",
                "schedule_legality": "pass",
                "drc_violations": 0,
                "unrouted_nets": 0,
                "phase7c_path_coverage": coverage,
            },
            "timing": {
                "engine": "opensta",
                "scope": "system_global",
                "wns_ns": _number(
                    target_clock.get("worst_slack_bound_ns"),
                    "full-flow target WNS",
                ),
                "tns_ns": _number(
                    target_clock.get("total_negative_slack_bound_ns"),
                    "full-flow target TNS",
                ),
            },
        }
    )
    if output_path is not None:
        write_json(output_path, acceptance)
    return acceptance


def _validate_evidence(
    family_root: Path,
    raw: Mapping[str, Any],
    *,
    model: Mapping[str, Any],
    model_sha256: str,
    configuration: str,
    profile: str,
    utilization_limit: float,
) -> Dict[str, Any]:
    evidence = _mapping(raw, "family specification.evidence")
    expected = {
        "blind_holdout",
        "application_holdout",
        "full_flow_acceptance",
    }
    _reject_unknown(evidence, expected, "family specification.evidence")
    if set(evidence) != expected:
        raise ValidationError(
            "qualified family specification requires blind, application, and full-flow evidence"
        )
    normalized: Dict[str, Any] = {}
    reports: Dict[str, Mapping[str, Any]] = {}
    for name in sorted(expected):
        reference = _mapping(evidence[name], f"evidence.{name}")
        _reject_unknown(reference, {"file", "sha256"}, f"evidence.{name}")
        path = _relative_file(family_root, reference.get("file"), f"evidence.{name}.file")
        digest = _sha256(reference.get("sha256"), f"evidence.{name}.sha256")
        if _file_sha256(path) != digest:
            raise ValidationError(f"evidence.{name}: SHA-256 mismatch")
        reports[name] = _mapping(read_json(path), f"evidence.{name} report")
        normalized[name] = {"file": str(path.relative_to(family_root)), "sha256": digest}
    model_name = str(model["model"]["name"])
    blind = reports["blind_holdout"]
    if (
        blind.get("schema") != VALIDATION_SCHEMA
        or blind.get("status") != "pass"
        or blind.get("model") != model_name
        or configuration not in blind.get("configurations", [])
    ):
        raise ValidationError(
            "family blind holdout evidence does not pass for the configuration"
        )
    application = reports["application_holdout"]
    application_utilization_limit = _number(
        application.get("utilization_limit"),
        "family application holdout utilization_limit",
    )
    if (
        application.get("schema") != APPLICATION_VALIDATION_SCHEMA
        or application.get("status") != "pass"
        or application.get("model") != model_name
        or application.get("configuration") != configuration
        or not math.isclose(
            application_utilization_limit,
            utilization_limit,
            rel_tol=0.0,
            abs_tol=1.0e-12,
        )
    ):
        raise ValidationError(
            "family application holdout evidence does not pass for the configuration"
        )
    full_flow = validate_full_flow_acceptance(reports["full_flow_acceptance"])
    if (
        full_flow["model"] != model_name
        or full_flow["model_sha256"] != model_sha256
        or full_flow["configuration"] != configuration
        or full_flow["profile"] != profile
        or not math.isclose(
            full_flow["utilization_limit"],
            utilization_limit,
            rel_tol=0.0,
            abs_tol=1.0e-12,
        )
    ):
        raise ValidationError("family full-flow evidence identity does not match")
    return normalized


def load_calibrated_platform_family(
    family_path: Path,
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Load and independently seal every model/evidence reference in a family."""

    family_root = family_path.resolve().parent
    root = _mapping(read_json(family_path), "calibrated platform family")
    _reject_unknown(
        root,
        {"schema", "family", "selection_policy", "specifications"},
        "calibrated platform family",
    )
    if root.get("schema") != FAMILY_SCHEMA:
        raise ValidationError(f"family.schema: expected {FAMILY_SCHEMA!r}")
    family = _mapping(root.get("family"), "family.family")
    _reject_unknown(
        family,
        {"name", "description", "qualification", "not_a_hardware_clone"},
        "family.family",
    )
    if family.get("qualification") != "independently_admitted_calibrated_platforms":
        raise ValidationError("family has an unsupported qualification")
    if family.get("not_a_hardware_clone") is not True:
        raise ValidationError("family must state that it is not a hardware clone")
    policy = _mapping(root.get("selection_policy"), "family.selection_policy")
    _reject_unknown(
        policy,
        {"objective", "resource_prefilter", "post_partition_gate"},
        "family.selection_policy",
    )
    if policy.get("objective") != "lowest_explicit_service_tier":
        raise ValidationError("family selection objective is unsupported")
    if policy.get("resource_prefilter") != "aggregate_effective_capacity":
        raise ValidationError("family resource prefilter is unsupported")
    if policy.get("post_partition_gate") != "calibrated_partition_envelope":
        raise ValidationError("family post-partition gate is unsupported")

    specifications = []
    loaded: Dict[str, Dict[str, Any]] = {}
    seen_ids = set()
    seen_ranks = set()
    seen_qualified_model_digests = set()
    qualified_resource_names: Optional[set[str]] = None
    for index, raw in enumerate(
        _array(root.get("specifications"), "family.specifications", nonempty=True)
    ):
        item = _mapping(raw, f"family.specifications[{index}]")
        _reject_unknown(
            item,
            {
                "id",
                "service_rank",
                "admission",
                "model_file",
                "model_sha256",
                "configuration",
                "profile",
                "utilization_limit",
                "evidence",
            },
            f"family.specifications[{index}]",
        )
        identifier = _string(item.get("id"), f"family.specifications[{index}].id")
        rank = _integer(
            item.get("service_rank"),
            f"family.specifications[{index}].service_rank",
            minimum=1,
        )
        if identifier in seen_ids or rank in seen_ranks:
            raise ValidationError("family specification IDs and service ranks must be unique")
        seen_ids.add(identifier)
        seen_ranks.add(rank)
        admission = _string(
            item.get("admission"), f"family.specifications[{index}].admission"
        )
        if admission not in {"candidate", "qualified"}:
            raise ValidationError("family admission must be candidate or qualified")
        model_path = _relative_file(
            family_root,
            item.get("model_file"),
            f"family.specifications[{index}].model_file",
        )
        model_digest = _sha256(
            item.get("model_sha256"),
            f"family.specifications[{index}].model_sha256",
        )
        if _file_sha256(model_path) != model_digest:
            raise ValidationError(f"family specification {identifier!r}: model SHA mismatch")
        model = validate_calibrated_platform_model(read_json(model_path))
        configuration = _string(
            item.get("configuration"),
            f"family.specifications[{index}].configuration",
        )
        configuration_value = next(
            (entry for entry in model["configurations"] if entry["id"] == configuration),
            None,
        )
        if configuration_value is None:
            raise ValidationError(
                f"family specification {identifier!r}: unknown model configuration"
            )
        profile = _string(item.get("profile"), f"family.specifications[{index}].profile")
        if profile not in _PROFILES:
            raise ValidationError("family specification profile is unsupported")
        utilization_limit = _number(
            item.get("utilization_limit"),
            f"family.specifications[{index}].utilization_limit",
        )
        declared_limit = float(model["device"]["utilization_limit"])
        if utilization_limit <= 0.0 or utilization_limit > declared_limit:
            raise ValidationError(
                "family specification utilization_limit must be positive and may "
                "not exceed the calibrated model limit"
            )
        normalized_evidence = None
        if admission == "qualified":
            if model_digest in seen_qualified_model_digests:
                raise ValidationError(
                    "qualified family specifications must use independently "
                    "calibrated model artifacts"
                )
            seen_qualified_model_digests.add(model_digest)
            normalized_evidence = _validate_evidence(
                family_root,
                item.get("evidence"),
                model=model,
                model_sha256=model_digest,
                configuration=configuration,
                profile=profile,
                utilization_limit=utilization_limit,
            )
            resource_names = set(model["profiles"][profile]["device_capacity"])
            if not resource_names or not resource_names <= _RESOURCES:
                raise ValidationError("family model has unsupported resource dimensions")
            if qualified_resource_names is None:
                qualified_resource_names = resource_names
            elif resource_names != qualified_resource_names:
                raise ValidationError(
                    "qualified family specifications must share resource dimensions"
                )
        elif "evidence" in item:
            raise ValidationError("candidate family specification must not carry pass evidence")
        normalized = {
            "id": identifier,
            "service_rank": rank,
            "admission": admission,
            "model_file": str(model_path.relative_to(family_root)),
            "model_sha256": model_digest,
            "configuration": configuration,
            "profile": profile,
            "utilization_limit": utilization_limit,
            **({"evidence": normalized_evidence} if normalized_evidence else {}),
        }
        specifications.append(normalized)
        loaded[identifier] = {
            "model": model,
            "configuration": configuration_value,
            "model_path": model_path,
        }
    specifications.sort(key=lambda entry: entry["service_rank"])
    qualified = [item for item in specifications if item["admission"] == "qualified"]
    # Recheck monotonicity in semantic service-rank order, independent of JSON order.
    prior = None
    for item in qualified:
        state = loaded[item["id"]]
        model = state["model"]
        config = state["configuration"]
        limit = float(item["utilization_limit"])
        aggregate = {
            resource: len(config["fpgas"])
            * math.floor(int(model["profiles"][item["profile"]]["device_capacity"][resource]) * limit)
            for resource in model["profiles"][item["profile"]]["device_capacity"]
        }
        if prior is not None and any(aggregate[k] < prior[k] for k in aggregate):
            raise ValidationError(
                "qualified service tiers must monotonically dominate aggregate capacity"
            )
        prior = aggregate
    normalized_family = {
        "schema": FAMILY_SCHEMA,
        "family": {
            "name": _string(family.get("name"), "family.family.name"),
            "description": str(family.get("description", "")),
            "qualification": "independently_admitted_calibrated_platforms",
            "not_a_hardware_clone": True,
        },
        "selection_policy": {
            "objective": "lowest_explicit_service_tier",
            "resource_prefilter": "aggregate_effective_capacity",
            "post_partition_gate": "calibrated_partition_envelope",
        },
        "specifications": specifications,
    }
    return normalized_family, loaded


def qualify_calibrated_platform_family_files(
    spec_path: Path,
    output_dir: Path,
) -> Dict[str, Any]:
    """Atomically assemble a qualified family from independently sealed evidence."""

    spec_path = spec_path.expanduser()
    if spec_path.is_symlink() or not spec_path.is_file():
        raise ValidationError("family qualification specification is missing")
    spec_path = spec_path.resolve()
    output_dir = output_dir.expanduser()
    if output_dir.exists() or output_dir.is_symlink():
        raise ValidationError("family qualification output already exists")
    output_dir = output_dir.resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    spec = _mapping(read_json(spec_path), "family qualification specification")
    _reject_unknown(
        spec,
        {"schema", "family", "tiers"},
        "family qualification specification",
    )
    if spec.get("schema") != QUALIFICATION_SPEC_SCHEMA:
        raise ValidationError(
            "family qualification specification has an unsupported schema"
        )
    family_value = _mapping(spec.get("family"), "family qualification family")
    _reject_unknown(
        family_value,
        {"name", "description"},
        "family qualification family",
    )
    family_name = _string(
        family_value.get("name"), "family qualification family.name"
    )
    description = str(family_value.get("description", ""))
    tier_values = _array(
        spec.get("tiers"), "family qualification tiers", nonempty=True
    )

    def source_file(raw: Any, context: str) -> Path:
        value = Path(_string(raw, context)).expanduser()
        candidate = value if value.is_absolute() else spec_path.parent / value
        if candidate.is_symlink() or not candidate.is_file():
            raise ValidationError(f"{context}: source file is missing or is a symlink")
        return candidate.resolve()

    with tempfile.TemporaryDirectory(
        prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent
    ) as temporary:
        temporary_root = Path(temporary)
        specifications = []
        for index, raw in enumerate(tier_values):
            tier = _mapping(raw, f"family qualification tiers[{index}]")
            _reject_unknown(
                tier,
                {
                    "id",
                    "service_rank",
                    "model_file",
                    "configuration",
                    "profile",
                    "utilization_limit",
                    "blind_holdout_file",
                    "application_holdout_file",
                    "full_flow_acceptance_file",
                },
                f"family qualification tiers[{index}]",
            )
            identifier = _string(
                tier.get("id"), f"family qualification tiers[{index}].id"
            )
            if (
                identifier.startswith(".")
                or Path(identifier).name != identifier
                or any(
                    not (character.isalnum() or character in "._-")
                    for character in identifier
                )
            ):
                raise ValidationError(
                    "family qualification tier ID is not a safe directory name"
                )
            destination = temporary_root / identifier
            destination.mkdir()
            inputs = {
                "model": source_file(
                    tier.get("model_file"),
                    f"family qualification tiers[{index}].model_file",
                ),
                "blind_holdout": source_file(
                    tier.get("blind_holdout_file"),
                    f"family qualification tiers[{index}].blind_holdout_file",
                ),
                "application_holdout": source_file(
                    tier.get("application_holdout_file"),
                    f"family qualification tiers[{index}].application_holdout_file",
                ),
                "full_flow_acceptance": source_file(
                    tier.get("full_flow_acceptance_file"),
                    f"family qualification tiers[{index}].full_flow_acceptance_file",
                ),
            }
            copied = {}
            for label, source in inputs.items():
                target = destination / f"{label}.json"
                shutil.copyfile(source, target)
                copied[label] = {
                    "file": str(target.relative_to(temporary_root)),
                    "sha256": _file_sha256(target),
                }
            specifications.append(
                {
                    "id": identifier,
                    "service_rank": _integer(
                        tier.get("service_rank"),
                        f"family qualification tiers[{index}].service_rank",
                        minimum=1,
                    ),
                    "admission": "qualified",
                    "model_file": copied["model"]["file"],
                    "model_sha256": copied["model"]["sha256"],
                    "configuration": _string(
                        tier.get("configuration"),
                        f"family qualification tiers[{index}].configuration",
                    ),
                    "profile": _string(
                        tier.get("profile", "nominal"),
                        f"family qualification tiers[{index}].profile",
                    ),
                    "utilization_limit": _number(
                        tier.get("utilization_limit"),
                        f"family qualification tiers[{index}].utilization_limit",
                    ),
                    "evidence": {
                        label: copied[label]
                        for label in (
                            "blind_holdout",
                            "application_holdout",
                            "full_flow_acceptance",
                        )
                    },
                }
            )
        family = {
            "schema": FAMILY_SCHEMA,
            "family": {
                "name": family_name,
                "description": description,
                "qualification": "independently_admitted_calibrated_platforms",
                "not_a_hardware_clone": True,
            },
            "selection_policy": {
                "objective": "lowest_explicit_service_tier",
                "resource_prefilter": "aggregate_effective_capacity",
                "post_partition_gate": "calibrated_partition_envelope",
            },
            "specifications": specifications,
        }
        family_path = temporary_root / "family.json"
        write_json(family_path, family)
        normalized, _ = load_calibrated_platform_family(family_path)
        write_json(family_path, normalized)
        temporary_root.replace(output_dir)

    final_family = output_dir / "family.json"
    return {
        "status": "pass",
        "family": family_name,
        "qualified_tiers": [
            item["id"] for item in normalized["specifications"]
        ],
        "family_file": str(final_family),
        "family_sha256": _file_sha256(final_family),
    }


def validate_design_demand(
    value: Mapping[str, Any], *, expected_resources: set[str]
) -> Dict[str, Any]:
    root = _mapping(value, "platform design demand")
    _reject_unknown(root, {"schema", "design", "resources"}, "platform design demand")
    if root.get("schema") != DEMAND_SCHEMA:
        raise ValidationError(f"design demand.schema: expected {DEMAND_SCHEMA!r}")
    design = _mapping(root.get("design"), "platform design demand.design")
    _reject_unknown(design, {"id", "source_sha256"}, "platform design demand.design")
    resources = _mapping(root.get("resources"), "platform design demand.resources")
    if set(resources) != expected_resources:
        raise ValidationError(
            "platform design demand resources must exactly match the family models"
        )
    normalized_resources = {
        resource: _integer(resources[resource], f"design demand.resources.{resource}")
        for resource in sorted(resources)
    }
    if not any(normalized_resources.values()):
        raise ValidationError("platform design demand must request at least one resource")
    return {
        "schema": DEMAND_SCHEMA,
        "design": {
            "id": _string(design.get("id"), "platform design demand.design.id"),
            "source_sha256": _sha256(
                design.get("source_sha256"),
                "platform design demand.design.source_sha256",
            ),
        },
        "resources": normalized_resources,
    }


def select_calibrated_platform(
    family_path: Path, demand_value: Mapping[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    family, loaded = load_calibrated_platform_family(family_path)
    qualified = [
        item for item in family["specifications"] if item["admission"] == "qualified"
    ]
    if not qualified:
        raise ValidationError("calibrated platform family has no qualified specifications")
    first_model = loaded[qualified[0]["id"]]["model"]
    resources = set(first_model["profiles"][qualified[0]["profile"]]["device_capacity"])
    demand = validate_design_demand(demand_value, expected_resources=resources)
    candidates = []
    selected = None
    for specification in qualified:
        state = loaded[specification["id"]]
        model = state["model"]
        configuration = state["configuration"]
        profile = model["profiles"][specification["profile"]]
        fpga_count = len(configuration["fpgas"])
        limit = float(specification["utilization_limit"])
        effective_per_fpga = {
            resource: math.floor(int(profile["device_capacity"][resource]) * limit)
            for resource in sorted(resources)
        }
        required = {
            resource: (
                math.ceil(demand["resources"][resource] / effective_per_fpga[resource])
                if demand["resources"][resource]
                else 0
            )
            for resource in sorted(resources)
        }
        feasible = max(required.values(), default=0) <= fpga_count
        candidate = {
            "specification": specification["id"],
            "service_rank": specification["service_rank"],
            "fpga_count": fpga_count,
            "effective_capacity_per_fpga": effective_per_fpga,
            "required_fpgas_by_resource": required,
            "resource_prefilter_pass": feasible,
        }
        candidates.append(candidate)
        if feasible and selected is None:
            selected = specification
    status = "pass" if selected is not None else "fail"
    report = {
        "schema": SELECTION_SCHEMA,
        "status": status,
        "family": family["family"]["name"],
        "design": demand["design"],
        "resources": demand["resources"],
        "selection_policy": family["selection_policy"],
        "selected_specification": selected["id"] if selected else None,
        "selected_configuration": selected["configuration"] if selected else None,
        "selected_profile": selected["profile"] if selected else None,
        "selected_utilization_limit": (
            selected["utilization_limit"] if selected else None
        ),
        "candidates": candidates,
        "qualification_boundary": (
            "Selection is an aggregate resource-capacity prefilter over independently "
            "qualified named platforms. Phase 3 balance and the calibrated post-partition "
            "communication envelope remain mandatory."
        ),
    }
    if selected is None:
        return report, {}, {}
    state = loaded[selected["id"]]
    boarddb = materialize_calibrated_boarddb(
        state["model"],
        selected["configuration"],
        selected["profile"],
        utilization_limit=selected["utilization_limit"],
    )
    timing = materialize_calibrated_board_link_timing(
        state["model"],
        selected["configuration"],
        selected["profile"],
        utilization_limit=selected["utilization_limit"],
    )
    return report, boarddb, timing


def select_calibrated_platform_files(
    family_path: Path,
    demand_path: Path,
    output_path: Optional[Path] = None,
    boarddb_output_path: Optional[Path] = None,
    timing_output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    report, boarddb, timing = select_calibrated_platform(
        family_path, read_json(demand_path)
    )
    if output_path is not None:
        write_json(output_path, report)
    if report["status"] == "pass":
        if boarddb_output_path is not None:
            write_json(boarddb_output_path, boarddb)
        if timing_output_path is not None:
            write_json(timing_output_path, timing)
    elif boarddb_output_path is not None or timing_output_path is not None:
        raise ValidationError("cannot materialize a platform when no specification fits")
    return report
