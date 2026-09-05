"""Source-bound complete-Phase-7 comparison for routing/TDM providers."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable

from .errors import ValidationError
from .io import read_json, write_json
from .multi_fpga_flow import (
    _phase6_physical_metrics,
    validate_multi_fpga_flow_report,
)
from .phase4 import validate_phase4
from .phase5 import validate_phase5
from .platform import Platform
from .routing import load_route_constraints
from .tdm import TDM_ACADEMIC_SCHEDULE_PROVIDER, TDM_BASELINE_PROVIDER
from .timing_routing import GLOBAL_CANDIDATE_PROVIDER, ROUTE_TDM_PROVIDER


SYSTEM_ROUTE_TDM_AB_SCHEMA = "emuflow.system-route-tdm-ab/v6"
SYSTEM_ROUTE_TDM_SCALE_SCHEMA = "emuflow.system-route-tdm-scale-ab/v2"
_FROZEN_ARTIFACTS = (
    "platform",
    "emuir",
    "partition_constraints",
    "route_constraints",
    "assignment",
    "timing_path_database",
    "partition_net_weights",
)
_LEGACY_FIXED_ARTIFACT_PATHS = {
    "platform": "frontend/phase1/platform.normalized.json",
    "partition_constraints": "partition/constraints.normalized.json",
    "route_constraints": "system-route/route_constraints.normalized.json",
}
_PHYSICAL_METRICS = (
    "total_wirelength",
    "worst_critical_path_ns",
    "worst_wns_ns",
    "total_tns_ns",
    "failing_endpoints",
    "failing_endpoint_constraints",
    "unrouted_nets",
    "drc_violations",
)
_GLOBAL_TIMING_METRICS = (
    "target_global_wns_ns",
    "target_global_tns_ns",
    "target_negative_paths",
    "runtime_global_wns_ns",
    "runtime_global_tns_ns",
    "runtime_negative_paths",
    "original_paths",
    "original_local_paths",
    "original_cross_fpga_paths",
    "compressed_representative_paths",
    "original_path_coverage",
    "physical_logic_exact_paths",
    "physical_logic_cone_bound_paths",
    "physical_logic_fallback_paths",
)
_BASELINE_PHASE6_PROVIDER = "deterministic-cut-shadow-split-v1"
_SCALE_METRIC_FIELDS = (
    "route_tree_edges",
    "route_total_link_bit_hops",
    "route_max_link_utilization",
    "route_overloaded_links",
    "route_worst_slack_ns",
    "route_worst_normalized_slack",
    "route_estimated_worst_tdm_slack_ns",
    "tdm_frame_slots",
    "tdm_completion_slot",
    "tdm_max_domain_utilization",
    "tdm_scheduled_bit_hops",
    "tdm_collisions",
    "tdm_worst_slack_ns",
    "tdm_worst_normalized_slack",
    "tdm_p01_normalized_slack",
    "tdm_median_normalized_slack",
    "tdm_negative_slack_paths",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _normalized_json_sha256(path: Path, root: Path) -> str:
    """Hash deterministic bytes while normalizing only their flow root."""

    root_texts = {root.absolute().as_posix(), root.resolve().as_posix()}
    root_texts.update(
        text[len("/private") :] for text in tuple(root_texts)
        if text.startswith("/private/")
    )

    encoded = path.read_bytes()
    for root_text in sorted(root_texts, key=len, reverse=True):
        encoded = encoded.replace(root_text.encode("utf-8"), b"$FLOW_ROOT")
    # OpenSTA writes the deterministic path table through a private temporary
    # directory and records that staging filename in TimingPathDB provenance.
    # The table contents are already embedded and independently sealed; only
    # the random directory component is non-semantic across otherwise frozen
    # runs.
    encoded = re.sub(
        rb'"/tmp/emuflow-opensta-[^"/]+/paths\.tsv"',
        b'"$OPENSTA_PATH_TABLE"',
        encoded,
    )
    return hashlib.sha256(encoded).hexdigest()


def _checked_artifact(root: Path, report: Dict[str, Any], key: str) -> str:
    artifact = report.get("artifacts", {}).get(key)
    if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
        raise ValidationError(f"routing/TDM A/B artifact {key!r} is missing")
    raw_path = artifact.get("path")
    expected = artifact.get("sha256")
    if (
        not isinstance(raw_path, str)
        or not raw_path
        or Path(raw_path).is_absolute()
        or ".." in Path(raw_path).parts
        or not isinstance(expected, str)
        or len(expected) != 64
    ):
        raise ValidationError(f"routing/TDM A/B artifact {key!r} seal is invalid")
    root = root.resolve()
    path = root / raw_path
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"routing/TDM A/B artifact {key!r} is not a regular file")
    resolved = path.resolve()
    if resolved.parent != root and root not in resolved.parents:
        raise ValidationError(f"routing/TDM A/B artifact {key!r} escapes its flow")
    actual = _sha256(resolved)
    if actual != expected:
        raise ValidationError(f"routing/TDM A/B artifact {key!r} hash disagrees")
    return actual


def _canonical_physical_report(
    root: Path, report: Dict[str, Any]
) -> Dict[str, Any]:
    """Load detailed physical evidence only from its canonical artifact."""

    embedded = report.get("physical")
    if isinstance(embedded, dict) and isinstance(embedded.get("fpgas"), list):
        return embedded
    _checked_artifact(root, report, "physical_flow_report")
    artifact = report["artifacts"]["physical_flow_report"]
    physical = read_json(root / artifact["path"])
    if not isinstance(physical, dict):
        raise ValidationError("routing/TDM A/B physical report is invalid")
    return physical


def _checked_frozen_artifact(
    root: Path, report: Dict[str, Any], key: str
) -> tuple[Path, str]:
    """Check a frozen input, including canonical pre-v5 flow locations.

    Complete flows created before the v5 comparison gate did not repeat the
    platform and normalized constraint seals in the top-level artifact table.
    They did, however, materialize those inputs at fixed checked paths below
    the flow root.  Reading those exact paths keeps already-running physical
    jobs usable while v5 reports emitted by new flows seal them directly.
    """

    artifact = report.get("artifacts", {}).get(key)
    if artifact is not None:
        digest = _checked_artifact(root, report, key)
        return root / artifact["path"], digest
    relative = _LEGACY_FIXED_ARTIFACT_PATHS.get(key)
    if relative is None:
        raise ValidationError(f"routing/TDM A/B artifact {key!r} is missing")
    root = root.resolve()
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"routing/TDM A/B artifact {key!r} is missing")
    resolved = path.resolve()
    if resolved.parent != root and root not in resolved.parents:
        raise ValidationError(f"routing/TDM A/B artifact {key!r} escapes its flow")
    return resolved, _sha256(resolved)


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _tool_executable_seals(physical: Dict[str, Any]) -> Dict[str, str]:
    """Hash every external executable actually recorded by Phase 7."""

    seals: Dict[str, str] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            command = value.get("command")
            if isinstance(command, list) and command:
                raw = command[0]
                if not isinstance(raw, str) or not raw:
                    raise ValidationError(
                        "routing/TDM A/B physical command executable is invalid"
                    )
                path = Path(raw).expanduser()
                if path.is_symlink():
                    path = path.resolve()
                if not path.is_file():
                    raise ValidationError(
                        f"routing/TDM A/B physical executable is missing: {raw}"
                    )
                digest = _sha256(path)
                previous = seals.setdefault(raw, digest)
                if previous != digest:
                    raise ValidationError(
                        "routing/TDM A/B physical executable changed during validation"
                    )
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(physical.get("fpgas"))
    if not seals:
        raise ValidationError(
            "routing/TDM A/B physical flow records no executable provenance"
        )
    return dict(sorted(seals.items()))


def _physical_reproducibility_source(
    physical: Dict[str, Any],
) -> Dict[str, Any]:
    """Retain the minimal Phase-7 records needed for independent replay."""

    records = physical.get("fpgas")
    if not isinstance(records, list):
        raise ValidationError(
            "routing/TDM A/B physical FPGA evidence is missing"
        )
    compact_records = []
    for record in records:
        stages = record.get("stages") if isinstance(record, dict) else None
        if not isinstance(stages, dict):
            raise ValidationError(
                "routing/TDM A/B physical stage evidence is missing"
            )
        retained_stages = {}
        for name in ("vpr_pack_place", "vpr_route"):
            stage = stages.get(name)
            if isinstance(stage, dict):
                retained_stages[name] = {
                    key: json.loads(json.dumps(stage[key]))
                    for key in ("architecture", "configuration", "command")
                    if key in stage
                }
        compact_records.append(
            {"fpga": record.get("fpga"), "stages": retained_stages}
        )
    return {
        key: json.loads(json.dumps(physical[key]))
        for key in (
            "backend",
            "architecture",
            "execution",
            "expected_fpgas",
        )
    } | {"fpgas": compact_records}


def _physical_reproducibility_configuration(
    physical: Dict[str, Any],
) -> Dict[str, Any]:
    """Rebuild the backend settings that must be identical across A/B arms."""

    backend = physical.get("backend")
    expected = physical.get("expected_fpgas")
    execution = physical.get("execution")
    architecture = physical.get("architecture")
    records = physical.get("fpgas")
    if (
        not isinstance(backend, dict)
        or not isinstance(expected, list)
        or not expected
        or any(not isinstance(item, str) or not item for item in expected)
        or not isinstance(execution, dict)
        or set(execution)
        != {
            "requested_workers",
            "effective_workers",
            "ordering",
            "pack_place_resume",
        }
        or not isinstance(records, list)
    ):
        raise ValidationError(
            "routing/TDM A/B physical reproducibility metadata is incomplete"
        )
    requested = execution.get("requested_workers")
    effective = execution.get("effective_workers")
    if (
        isinstance(requested, bool)
        or not isinstance(requested, int)
        or requested <= 0
        or isinstance(effective, bool)
        or not isinstance(effective, int)
        or effective <= 0
        or effective > min(requested, len(expected))
        or execution.get("ordering") != "boarddb-fpga-order"
        or not isinstance(execution.get("pack_place_resume"), bool)
    ):
        raise ValidationError(
            "routing/TDM A/B physical execution settings are invalid"
        )
    by_id = {
        record.get("fpga"): record
        for record in records
        if isinstance(record, dict)
    }
    if (
        list(expected) != [record.get("fpga") for record in records]
        or set(by_id) != set(expected)
    ):
        raise ValidationError(
            "routing/TDM A/B physical FPGA order is not deterministic"
        )
    backend_id = backend.get("id")
    canonical_architecture = (
        {
            key: value
            for key, value in architecture.items()
            if key not in {"path", "output"}
        }
        if isinstance(architecture, dict)
        else architecture
    )
    result: Dict[str, Any] = {
        "backend": backend,
        "expected_fpgas": list(expected),
        "execution": dict(execution),
        "architecture": canonical_architecture,
        "tool_executables": _tool_executable_seals(physical),
    }
    if backend_id == "open":
        architecture_sha = (
            architecture.get("sha256") if isinstance(architecture, dict) else None
        )
        if not _valid_digest(architecture_sha):
            raise ValidationError(
                "routing/TDM A/B open architecture seal is missing"
            )
        seeds: Dict[str, int] = {}
        widths: Dict[str, int] = {}
        for fpga_id in expected:
            stages = by_id[fpga_id].get("stages")
            if not isinstance(stages, dict):
                raise ValidationError(
                    f"routing/TDM A/B physical stages for {fpga_id} are missing"
                )
            pack = stages.get("vpr_pack_place")
            route = stages.get("vpr_route")
            if not isinstance(pack, dict) or not isinstance(route, dict):
                raise ValidationError(
                    f"routing/TDM A/B VPR stages for {fpga_id} are missing"
                )
            seed = pack.get("configuration", {}).get("seed")
            width = route.get("configuration", {}).get("route_channel_width")
            if (
                isinstance(seed, bool)
                or not isinstance(seed, int)
                or seed < 0
                or isinstance(width, bool)
                or not isinstance(width, int)
                or width <= 0
                or width % 2
                or pack.get("architecture", {}).get("sha256") != architecture_sha
                or route.get("architecture", {}).get("sha256") != architecture_sha
            ):
                raise ValidationError(
                    f"routing/TDM A/B VPR configuration for {fpga_id} is invalid"
                )
            seeds[fpga_id] = seed
            widths[fpga_id] = width
        result["vpr_pack_place_seed"] = seeds
        result["vpr_route_channel_width"] = widths
    elif backend_id == "vivado":
        if not isinstance(architecture, dict):
            raise ValidationError(
                "routing/TDM A/B Vivado architecture metadata is missing"
            )
    else:
        raise ValidationError("routing/TDM A/B physical backend is unsupported")
    return result


def _finite_metrics(value: Dict[str, Any], fields: Iterable[str], label: str) -> None:
    for field in fields:
        item = value.get(field)
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
        ):
            raise ValidationError(f"routing/TDM A/B {label} metric {field!r} is invalid")


def _negative_slack_improvement(
    baseline: float, upgrade: float
) -> Dict[str, Any]:
    """Describe slack improvement without dividing signed timing values."""

    delta = upgrade - baseline
    baseline_deficit = max(0.0, -baseline)
    upgrade_deficit = max(0.0, -upgrade)
    return {
        "absolute_improvement_ns": delta,
        "negative_slack_deficit_reduction_percent": (
            (baseline_deficit - upgrade_deficit) * 100.0 / baseline_deficit
            if baseline_deficit > 0.0
            else None
        ),
        "baseline_closed": baseline >= 0.0,
        "upgrade_closed": upgrade >= 0.0,
        "closure_transition": (
            "closed"
            if baseline < 0.0 <= upgrade
            else "regressed"
            if baseline >= 0.0 > upgrade
            else "remained-closed"
            if baseline >= 0.0 and upgrade >= 0.0
            else "remained-open"
        ),
    }


def _route_metrics(stage: Dict[str, Any]) -> Dict[str, Any]:
    validation = stage["validation"]
    return {
        field: validation[field]
        for field in (
            "demands",
            "routed_sinks",
            "tree_edges",
            "total_link_bit_hops",
            "max_link_utilization",
            "overloaded_links",
            "estimated_worst_tdm_slack_ns",
        )
        if field in validation
    }


def _tdm_metrics(stage: Dict[str, Any]) -> Dict[str, Any]:
    validation = stage["validation"]
    return {
        field: validation[field]
        for field in (
            "frame_slots",
            "completion_slot",
            "max_domain_utilization",
            "scheduled_bit_hops",
            "collisions",
        )
        if field in validation
    }


def _global_timing_metrics(
    root: Path, report: Dict[str, Any]
) -> Dict[str, Any]:
    if "qor_report" in report.get("artifacts", {}):
        _checked_artifact(root, report, "qor_report")
        timing = read_json(
            root / report["artifacts"]["qor_report"]["path"]
        ).get("timing")
    else:
        # Explicit reader support for immutable pre-v3 flow reports.  New
        # producers never embed this payload in the orchestration report.
        timing = report.get("runtime", {}).get("system_timing")
    if not isinstance(timing, dict) or timing.get("status") not in {
        "pass", "fail"
    }:
        raise ValidationError("routing/TDM A/B global system timing is missing")
    if timing.get("timing_scope") != "whole-original-design":
        raise ValidationError(
            "routing/TDM A/B requires whole-design timing; a per-FPGA or "
            "cross-FPGA-only subset is not global timing"
        )
    binding = timing.get("source_binding")
    expected_binding = {
        "path_database_sha256": "timing_path_database",
        "original_ir_sha256": "emuir",
        "assignment_sha256": "assignment",
        "routes_sha256": "routes",
    }
    if not isinstance(binding, dict) or set(binding) != {
        *expected_binding,
        "original_paths",
        "original_path_ids_sha256",
    }:
        raise ValidationError(
            "routing/TDM A/B whole-design timing source binding is missing"
        )
    for field, artifact in expected_binding.items():
        if binding[field] != _checked_artifact(root, report, artifact):
            raise ValidationError(
                f"routing/TDM A/B whole-design timing {field!r} disagrees "
                "with its flow artifact"
            )
    paths = timing.get("paths")
    summary = timing.get("summary")
    if not isinstance(paths, list) or not paths or not isinstance(summary, dict):
        raise ValidationError("routing/TDM A/B global timing coverage is missing")
    ids = [path.get("path") for path in paths if isinstance(path, dict)]
    if len(ids) != len(paths) or len(set(ids)) != len(paths):
        raise ValidationError("routing/TDM A/B global timing path IDs are invalid")
    target = [path.get("target_clock_slack_bound_ns") for path in paths]
    runtime = [path.get("runtime_clock_slack_bound_ns") for path in paths]
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in (*target, *runtime)
    ):
        raise ValidationError("routing/TDM A/B global path slack is invalid")
    original = summary.get("original_paths")
    local = summary.get("original_local_paths")
    crossing = summary.get("original_cross_fpga_paths")
    representatives = summary.get("compressed_representative_paths")
    coverage = summary.get("original_path_coverage")
    if (
        isinstance(original, bool)
        or not isinstance(original, int)
        or original != len(paths)
        or isinstance(local, bool)
        or not isinstance(local, int)
        or local < 0
        or isinstance(crossing, bool)
        or not isinstance(crossing, int)
        or crossing <= 0
        or local + crossing != original
        or isinstance(representatives, bool)
        or not isinstance(representatives, int)
        or representatives <= 0
        or representatives > original
        or not isinstance(coverage, (int, float))
        or float(coverage) != 1.0
        or binding["original_paths"] != original
        or binding["original_path_ids_sha256"]
        != summary.get("original_path_ids_sha256")
    ):
        raise ValidationError("routing/TDM A/B global timing coverage is incomplete")
    exact_paths = 0
    cone_bound_paths = 0
    fallback_paths = 0
    exact_models = {
        "routed-selected-path-chain-exact",
        "routed-staging-chain-exact",
    }
    cone_bound_models = {
        "routed-endpoint-longest-path-conservative",
        "routed-staging-chain-cone-upper-bound",
    }
    for path in paths:
        exact = path.get("physical_logic_segments_exact")
        cone_bound = path.get("physical_logic_segments_cone_bound")
        model = path.get("physical_logic_model")
        if (
            not isinstance(exact, bool)
            or not isinstance(cone_bound, bool)
            or exact and cone_bound
            or not isinstance(model, str)
            or exact and model not in exact_models
            or cone_bound and model not in cone_bound_models
            or not exact and not cone_bound and model in exact_models
            or not exact and not cone_bound and model in cone_bound_models
        ):
            raise ValidationError(
                "routing/TDM A/B physical logic exactness is inconsistent"
            )
        if exact:
            exact_paths += 1
        elif cone_bound:
            cone_bound_paths += 1
        else:
            fallback_paths += 1
    exactness = timing.get("path_exactness")
    if (
        not isinstance(exactness, dict)
        or exactness.get("endpoint_exact_logic_paths") != exact_paths
        or exactness.get("cone_bound_logic_paths") != cone_bound_paths
        or exactness.get("fallback_logic_paths") != fallback_paths
        or exact_paths + cone_bound_paths + fallback_paths != original
        or exactness.get("physical_logic_segments")
        != (exact_paths == original)
        or exactness.get("physical_logic_segment_bounds")
        != (exact_paths + cone_bound_paths == original)
    ):
        raise ValidationError(
            "routing/TDM A/B physical logic exactness summary disagrees"
        )
    result = {
        "target_global_wns_ns": min(target),
        "target_global_tns_ns": sum(min(0.0, value) for value in target),
        "target_negative_paths": sum(value < 0.0 for value in target),
        "runtime_global_wns_ns": min(runtime),
        "runtime_global_tns_ns": sum(min(0.0, value) for value in runtime),
        "runtime_negative_paths": sum(value < 0.0 for value in runtime),
        "original_paths": original,
        "original_local_paths": local,
        "original_cross_fpga_paths": crossing,
        "compressed_representative_paths": representatives,
        "original_path_coverage": float(coverage),
        "physical_logic_exact_paths": exact_paths,
        "physical_logic_cone_bound_paths": cone_bound_paths,
        "physical_logic_fallback_paths": fallback_paths,
    }
    for clock, prefix in (("target_clock", "target"), ("runtime_clock", "runtime")):
        clock_report = timing.get(clock)
        if (
            not isinstance(clock_report, dict)
            or not math.isclose(
                float(clock_report.get("worst_slack_bound_ns", math.nan)),
                result[f"{prefix}_global_wns_ns"],
                rel_tol=0.0,
                abs_tol=1.0e-9,
            )
            or not math.isclose(
                float(clock_report.get("tns_bound_ns", math.nan)),
                result[f"{prefix}_global_tns_ns"],
                rel_tol=0.0,
                abs_tol=1.0e-9,
            )
            or clock_report.get("negative_slack_paths")
            != result[f"{prefix}_negative_paths"]
        ):
            raise ValidationError(
                f"routing/TDM A/B {clock} global metrics disagree"
            )
    return result


def validate_system_route_tdm_ab_comparison(report: Dict[str, Any]) -> Dict[str, Any]:
    if (
        report.get("schema") != SYSTEM_ROUTE_TDM_AB_SCHEMA
        or report.get("status") != "pass"
        or report.get("qualification")
        != "complete-phase7-whole-design-timing-source-bound-ab"
    ):
        raise ValidationError("routing/TDM A/B comparison identity is invalid")
    frozen = report.get("frozen_upstream")
    if not isinstance(frozen, dict) or set(frozen) != set(_FROZEN_ARTIFACTS):
        raise ValidationError("routing/TDM A/B frozen upstream is incomplete")
    for digest in frozen.values():
        if not _valid_digest(digest):
            raise ValidationError("routing/TDM A/B frozen digest is invalid")
    configuration = report.get("configuration")
    if configuration != {
        "baseline_route_provider": ROUTE_TDM_PROVIDER,
        "upgrade_route_provider": GLOBAL_CANDIDATE_PROVIDER,
        "baseline_tdm_provider": TDM_BASELINE_PROVIDER,
        "upgrade_tdm_provider": TDM_ACADEMIC_SCHEDULE_PROVIDER,
        "shared_phase6_provider": _BASELINE_PHASE6_PROVIDER,
    }:
        raise ValidationError("routing/TDM A/B provider configuration is invalid")
    arms = report.get("arms")
    if not isinstance(arms, dict) or set(arms) != {"baseline", "upgrade"}:
        raise ValidationError("routing/TDM A/B arms are incomplete")
    for label, arm in arms.items():
        if not isinstance(arm, dict):
            raise ValidationError(f"routing/TDM A/B {label} arm is invalid")
        physical = arm.get("physical")
        if not isinstance(physical, dict) or set(physical) != set(_PHYSICAL_METRICS):
            raise ValidationError(f"routing/TDM A/B {label} physical metrics are incomplete")
        _finite_metrics(physical, _PHYSICAL_METRICS, f"{label} physical")
        global_timing = arm.get("global_timing")
        if (
            not isinstance(global_timing, dict)
            or set(global_timing) != set(_GLOBAL_TIMING_METRICS)
        ):
            raise ValidationError(
                f"routing/TDM A/B {label} global timing is incomplete"
            )
        _finite_metrics(
            global_timing, _GLOBAL_TIMING_METRICS, f"{label} global timing"
        )
        if global_timing["original_path_coverage"] != 1.0:
            raise ValidationError(
                f"routing/TDM A/B {label} global path coverage is incomplete"
            )
        exactness_counts = (
            global_timing["physical_logic_exact_paths"],
            global_timing["physical_logic_cone_bound_paths"],
            global_timing["physical_logic_fallback_paths"],
        )
        if (
            any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for value in exactness_counts
            )
            or sum(exactness_counts) != global_timing["original_paths"]
        ):
            raise ValidationError(
                f"routing/TDM A/B {label} physical logic exactness is invalid"
            )
        if physical["unrouted_nets"] != 0 or physical["drc_violations"] != 0:
            raise ValidationError(f"routing/TDM A/B {label} physical arm did not close")
        source = arm.get("flow_report")
        if (
            not isinstance(source, dict)
            or set(source) != {"sha256"}
            or not isinstance(source["sha256"], str)
            or len(source["sha256"]) != 64
        ):
            raise ValidationError(f"routing/TDM A/B {label} source seal is invalid")
        reproducibility = arm.get("physical_reproducibility")
        reproducibility_source = arm.get("physical_reproducibility_source")
        evidence = arm.get("physical_reproducibility_evidence")
        reproducibility_keys = {
            "backend",
            "expected_fpgas",
            "execution",
            "architecture",
            "tool_executables",
        }
        if isinstance(reproducibility, dict) and reproducibility.get(
            "backend", {}
        ).get("id") == "open":
            reproducibility_keys.update(
                {"vpr_pack_place_seed", "vpr_route_channel_width"}
            )
        if (
            not isinstance(reproducibility, dict)
            or set(reproducibility) != reproducibility_keys
            or not isinstance(reproducibility.get("backend"), dict)
            or not isinstance(reproducibility.get("expected_fpgas"), list)
            or not isinstance(reproducibility.get("execution"), dict)
            or not isinstance(reproducibility.get("architecture"), dict)
            or not isinstance(reproducibility.get("tool_executables"), dict)
            or not reproducibility["tool_executables"]
            or not isinstance(reproducibility_source, dict)
            or _physical_reproducibility_configuration(
                reproducibility_source
            )
            != reproducibility
            or not isinstance(evidence, dict)
            or set(evidence)
            != {"physical_report_sha256", "configuration_sha256"}
            or not _valid_digest(evidence.get("physical_report_sha256"))
            or evidence.get("configuration_sha256")
            != _json_sha256(reproducibility)
            or evidence.get("physical_report_sha256")
            != _json_sha256(reproducibility_source)
            or any(
                not isinstance(path, str)
                or not path
                or not _valid_digest(digest)
                for path, digest in reproducibility["tool_executables"].items()
            )
            or (
                reproducibility.get("backend", {}).get("id") == "open"
                and (
                    set(reproducibility["vpr_pack_place_seed"])
                    != set(reproducibility["expected_fpgas"])
                    or set(reproducibility["vpr_route_channel_width"])
                    != set(reproducibility["expected_fpgas"])
                )
            )
        ):
            raise ValidationError(
                f"routing/TDM A/B {label} reproducibility evidence disagrees"
            )
    if (
        arms["baseline"]["physical_reproducibility"]
        != arms["upgrade"]["physical_reproducibility"]
    ):
        raise ValidationError(
            "routing/TDM A/B physical architecture, tools, seed, workers, "
            "or backend options differ"
        )
    baseline = arms["baseline"]["physical"]
    upgrade = arms["upgrade"]["physical"]
    expected = {
        field: upgrade[field] - baseline[field]
        for field in _PHYSICAL_METRICS
        if field not in {"unrouted_nets", "drc_violations"}
    }
    delta = report.get("physical_delta_upgrade_minus_baseline")
    if delta != expected:
        raise ValidationError("routing/TDM A/B physical delta disagrees")
    expected_global = {
        field: arms["upgrade"]["global_timing"][field]
        - arms["baseline"]["global_timing"][field]
        for field in _GLOBAL_TIMING_METRICS
        if field not in {
            "original_paths",
            "original_local_paths",
            "original_cross_fpga_paths",
            "compressed_representative_paths",
            "original_path_coverage",
            "physical_logic_exact_paths",
            "physical_logic_cone_bound_paths",
            "physical_logic_fallback_paths",
        }
    }
    if report.get("global_timing_delta_upgrade_minus_baseline") != expected_global:
        raise ValidationError("routing/TDM A/B global timing delta disagrees")
    for field in (
        "physical_logic_exact_paths",
        "physical_logic_cone_bound_paths",
        "physical_logic_fallback_paths",
    ):
        if (
            arms["baseline"]["global_timing"][field]
            != arms["upgrade"]["global_timing"][field]
        ):
            raise ValidationError(
                "routing/TDM A/B physical logic exactness populations differ"
            )
    expected_improvements = {
        field: _negative_slack_improvement(
            float(arms["baseline"]["global_timing"][field]),
            float(arms["upgrade"]["global_timing"][field]),
        )
        for field in (
            "target_global_wns_ns",
            "target_global_tns_ns",
            "runtime_global_wns_ns",
            "runtime_global_tns_ns",
        )
    }
    if report.get("global_timing_improvement") != expected_improvements:
        raise ValidationError("routing/TDM A/B global timing improvement disagrees")
    return {
        "status": "pass",
        "target_global_wns_improvement_ns": expected_global[
            "target_global_wns_ns"
        ],
        "target_global_tns_improvement_ns": expected_global[
            "target_global_tns_ns"
        ],
        "runtime_global_wns_improvement_ns": expected_global[
            "runtime_global_wns_ns"
        ],
        "runtime_global_tns_improvement_ns": expected_global[
            "runtime_global_tns_ns"
        ],
        "per_fpga_physical_wns_change_ns": expected["worst_wns_ns"],
        "per_fpga_physical_tns_change_ns": expected["total_tns_ns"],
        "critical_path_change_ns": expected["worst_critical_path_ns"],
        "wirelength_change": expected["total_wirelength"],
        "failing_endpoint_change": expected["failing_endpoints"],
    }


def build_system_route_tdm_ab_comparison(
    baseline_root: Path,
    upgrade_root: Path,
    output_path: Path,
) -> Dict[str, Any]:
    baseline_root = baseline_root.resolve()
    upgrade_root = upgrade_root.resolve()
    if baseline_root == upgrade_root:
        raise ValidationError("routing/TDM A/B flow roots must be different")
    reports: Dict[str, Dict[str, Any]] = {}
    physical_reports: Dict[str, Dict[str, Any]] = {}
    roots = {"baseline": baseline_root, "upgrade": upgrade_root}
    for label, root in roots.items():
        path = root / "multi-fpga-flow-report.json"
        if path.is_symlink() or not path.is_file():
            raise ValidationError(f"routing/TDM A/B {label} flow report is missing")
        report = read_json(path)
        validate_multi_fpga_flow_report(report)
        if report.get("physical") is None:
            raise ValidationError(f"routing/TDM A/B {label} did not complete Phase 7")
        reports[label] = report
        physical_reports[label] = _canonical_physical_report(root, report)

    baseline = reports["baseline"]
    upgrade = reports["upgrade"]
    if (
        baseline["stages"]["frontend"].get("design")
        != upgrade["stages"]["frontend"].get("design")
        or baseline["stages"]["frontend"].get("platform")
        != upgrade["stages"]["frontend"].get("platform")
        or baseline["stages"]["partition"].get("provider")
        != upgrade["stages"]["partition"].get("provider")
    ):
        raise ValidationError("routing/TDM A/B common design/platform/partition differs")
    if (
        baseline["stages"]["system_route"].get("provider") != ROUTE_TDM_PROVIDER
        or upgrade["stages"]["system_route"].get("provider")
        != GLOBAL_CANDIDATE_PROVIDER
        or baseline["stages"]["tdm"].get("provider") != TDM_BASELINE_PROVIDER
        or upgrade["stages"]["tdm"].get("provider")
        != TDM_ACADEMIC_SCHEDULE_PROVIDER
    ):
        raise ValidationError("routing/TDM A/B arm providers are not the frozen pair")
    shared_phase6 = baseline["stages"]["split"].get("provider")
    if (
        shared_phase6 != _BASELINE_PHASE6_PROVIDER
        or upgrade["stages"]["split"].get("provider") != shared_phase6
    ):
        raise ValidationError("routing/TDM A/B Phase 6 provider is not shared baseline")

    frozen: Dict[str, str] = {}
    for key in _FROZEN_ARTIFACTS:
        baseline_path, _ = _checked_frozen_artifact(
            baseline_root, baseline, key
        )
        upgrade_path, _ = _checked_frozen_artifact(
            upgrade_root, upgrade, key
        )
        baseline_digest = _normalized_json_sha256(
            baseline_path, baseline_root
        )
        upgrade_digest = _normalized_json_sha256(upgrade_path, upgrade_root)
        if baseline_digest != upgrade_digest:
            raise ValidationError(f"routing/TDM A/B frozen artifact {key!r} differs")
        frozen[key] = baseline_digest

    arms: Dict[str, Any] = {}
    for label, report in reports.items():
        root = roots[label]
        source_path = root / "multi-fpga-flow-report.json"
        canonical_physical = physical_reports[label]
        physical = _phase6_physical_metrics(canonical_physical)
        reproducibility_source = _physical_reproducibility_source(
            canonical_physical
        )
        reproducibility = _physical_reproducibility_configuration(
            reproducibility_source
        )
        arms[label] = {
            "flow_report": {"sha256": _sha256(source_path)},
            "route_provider": report["stages"]["system_route"]["provider"],
            "tdm_provider": report["stages"]["tdm"]["provider"],
            "route": _route_metrics(report["stages"]["system_route"]),
            "tdm": _tdm_metrics(report["stages"]["tdm"]),
            "physical": physical,
            "global_timing": _global_timing_metrics(root, report),
            "physical_reproducibility": reproducibility,
            "physical_reproducibility_source": reproducibility_source,
            "physical_reproducibility_evidence": {
                "physical_report_sha256": _json_sha256(
                    reproducibility_source
                ),
                "configuration_sha256": _json_sha256(reproducibility),
            },
        }
    baseline_physical = arms["baseline"]["physical"]
    upgrade_physical = arms["upgrade"]["physical"]
    result = {
        "schema": SYSTEM_ROUTE_TDM_AB_SCHEMA,
        "status": "pass",
        "qualification": (
            "complete-phase7-whole-design-timing-source-bound-ab"
        ),
        "design": baseline["stages"]["frontend"]["design"],
        "platform": baseline["stages"]["frontend"]["platform"],
        "frozen_upstream": frozen,
        "configuration": {
            "baseline_route_provider": ROUTE_TDM_PROVIDER,
            "upgrade_route_provider": GLOBAL_CANDIDATE_PROVIDER,
            "baseline_tdm_provider": TDM_BASELINE_PROVIDER,
            "upgrade_tdm_provider": TDM_ACADEMIC_SCHEDULE_PROVIDER,
            "shared_phase6_provider": shared_phase6,
        },
        "arms": arms,
        "physical_delta_upgrade_minus_baseline": {
            field: upgrade_physical[field] - baseline_physical[field]
            for field in _PHYSICAL_METRICS
            if field not in {"unrouted_nets", "drc_violations"}
        },
        "global_timing_delta_upgrade_minus_baseline": {
            field: arms["upgrade"]["global_timing"][field]
            - arms["baseline"]["global_timing"][field]
            for field in _GLOBAL_TIMING_METRICS
            if field not in {
                "original_paths",
                "original_local_paths",
                "original_cross_fpga_paths",
                "compressed_representative_paths",
                "original_path_coverage",
                "physical_logic_exact_paths",
                "physical_logic_cone_bound_paths",
                "physical_logic_fallback_paths",
            }
        },
        "global_timing_improvement": {
            field: _negative_slack_improvement(
                float(arms["baseline"]["global_timing"][field]),
                float(arms["upgrade"]["global_timing"][field]),
            )
            for field in (
                "target_global_wns_ns",
                "target_global_tns_ns",
                "runtime_global_wns_ns",
                "runtime_global_tns_ns",
            )
        },
    }
    result["validation"] = validate_system_route_tdm_ab_comparison(result)
    write_json(output_path, result)
    return result


def validate_system_route_tdm_scale_comparison(
    report: Dict[str, Any],
) -> Dict[str, Any]:
    if (
        report.get("schema") != SYSTEM_ROUTE_TDM_SCALE_SCHEMA
        or report.get("status") != "pass"
        or report.get("qualification") != "independent-phase4-phase5-scale-ab"
    ):
        raise ValidationError("routing/TDM scale comparison identity is invalid")
    sources = report.get("frozen_upstream")
    if not isinstance(sources, dict) or set(sources) != {
        "assignment", "platform", "route_constraints", "timing_paths"
    }:
        raise ValidationError("routing/TDM scale frozen upstream is incomplete")
    for digest in sources.values():
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValidationError("routing/TDM scale source digest is invalid")
    arms = report.get("arms")
    if not isinstance(arms, dict) or set(arms) != {"baseline", "upgrade"}:
        raise ValidationError("routing/TDM scale arms are incomplete")
    expected_providers = {
        "baseline": (ROUTE_TDM_PROVIDER, TDM_BASELINE_PROVIDER),
        "upgrade": (GLOBAL_CANDIDATE_PROVIDER, TDM_ACADEMIC_SCHEDULE_PROVIDER),
    }
    for label, (route_provider, tdm_provider) in expected_providers.items():
        arm = arms.get(label)
        if (
            not isinstance(arm, dict)
            or arm.get("route_provider") != route_provider
            or arm.get("tdm_provider") != tdm_provider
            or arm.get("route_validation", {}).get("status") != "pass"
            or arm.get("tdm_validation", {}).get("status") != "pass"
        ):
            raise ValidationError(f"routing/TDM scale {label} arm is invalid")
        runtime = arm.get("runtime_seconds")
        metrics = arm.get("metrics")
        if (
            isinstance(runtime, bool)
            or not isinstance(runtime, (int, float))
            or not math.isfinite(float(runtime))
            or float(runtime) < 0.0
            or not isinstance(metrics, dict)
            or set(metrics) != set(_SCALE_METRIC_FIELDS)
        ):
            raise ValidationError(
                f"routing/TDM scale {label} metrics are invalid"
            )
        _finite_metrics(metrics, _SCALE_METRIC_FIELDS, f"scale {label}")
        if metrics != _scale_metrics(
            arm["route_validation"], arm["tdm_validation"]
        ):
            raise ValidationError(
                f"routing/TDM scale {label} metrics were not reconstructed"
            )
        seals = arm.get("artifacts")
        if not isinstance(seals, dict) or set(seals) != {
            "routes", "schedule", "ratio_plan"
        }:
            raise ValidationError(f"routing/TDM scale {label} seals are invalid")
        if any(
            value is not None and not _valid_digest(value)
            for value in seals.values()
        ) or (label == "baseline") != (seals["ratio_plan"] is None):
            raise ValidationError(f"routing/TDM scale {label} digest is invalid")
    expected_delta = {
        field: (
            float(arms["upgrade"]["metrics"][field])
            - float(arms["baseline"]["metrics"][field])
        )
        for field in _SCALE_METRIC_FIELDS
    }
    expected_runtime_delta = (
        float(arms["upgrade"]["runtime_seconds"])
        - float(arms["baseline"]["runtime_seconds"])
    )
    if report.get("delta_upgrade_minus_baseline") != expected_delta:
        raise ValidationError("routing/TDM scale metric delta disagrees")
    if report.get("runtime_delta_seconds") != expected_runtime_delta:
        raise ValidationError("routing/TDM scale runtime delta disagrees")
    return {
        "status": "pass",
        "baseline_routed_sinks": arms["baseline"]["route_validation"].get(
            "routed_sinks"
        ),
        "upgrade_routed_sinks": arms["upgrade"]["route_validation"].get(
            "routed_sinks"
        ),
        "baseline_scheduled_bit_hops": arms["baseline"]["tdm_validation"].get(
            "scheduled_bit_hops"
        ),
        "upgrade_scheduled_bit_hops": arms["upgrade"]["tdm_validation"].get(
            "scheduled_bit_hops"
        ),
        "tdm_worst_slack_improvement_ns": expected_delta[
            "tdm_worst_slack_ns"
        ],
        "runtime_delta_seconds": expected_runtime_delta,
    }


def _scale_metrics(
    route_validation: Dict[str, Any], tdm_validation: Dict[str, Any]
) -> Dict[str, Any]:
    timing = tdm_validation.get("timing")
    if not isinstance(timing, dict) or timing.get("status") != "pass":
        raise ValidationError("routing/TDM scale timing reconstruction is missing")
    values = {
        "route_tree_edges": route_validation.get("tree_edges"),
        "route_total_link_bit_hops": route_validation.get(
            "total_link_bit_hops"
        ),
        "route_max_link_utilization": route_validation.get(
            "max_link_utilization"
        ),
        "route_overloaded_links": route_validation.get("overloaded_links"),
        "route_worst_slack_ns": route_validation.get("worst_slack_ns"),
        "route_worst_normalized_slack": route_validation.get(
            "worst_normalized_slack"
        ),
        "route_estimated_worst_tdm_slack_ns": route_validation.get(
            "estimated_worst_tdm_slack_ns"
        ),
        "tdm_frame_slots": tdm_validation.get("frame_slots"),
        "tdm_completion_slot": tdm_validation.get("completion_slot"),
        "tdm_max_domain_utilization": tdm_validation.get(
            "max_domain_utilization"
        ),
        "tdm_scheduled_bit_hops": tdm_validation.get("scheduled_bit_hops"),
        "tdm_collisions": tdm_validation.get("collisions"),
        "tdm_worst_slack_ns": timing.get("worst_slack_ns"),
        "tdm_worst_normalized_slack": timing.get("worst_normalized_slack"),
        "tdm_p01_normalized_slack": timing.get("p01_normalized_slack"),
        "tdm_median_normalized_slack": timing.get(
            "median_normalized_slack"
        ),
        "tdm_negative_slack_paths": timing.get("negative_slack_paths"),
    }
    _finite_metrics(values, _SCALE_METRIC_FIELDS, "scale reconstructed")
    return values


def build_system_route_tdm_scale_comparison(
    assignment_path: Path,
    platform_path: Path,
    route_constraints_path: Path,
    timing_paths_path: Path,
    baseline_route_root: Path,
    baseline_tdm_root: Path,
    upgrade_route_root: Path,
    upgrade_tdm_root: Path,
    output_path: Path,
    *,
    baseline_runtime_seconds: float,
    upgrade_runtime_seconds: float,
) -> Dict[str, Any]:
    """Independently replay and seal Phase 4/5 on a frozen large instance."""

    source_paths = {
        "assignment": assignment_path.resolve(),
        "platform": platform_path.resolve(),
        "route_constraints": route_constraints_path.resolve(),
        "timing_paths": timing_paths_path.resolve(),
    }
    for label, path in source_paths.items():
        if path.is_symlink() or not path.is_file():
            raise ValidationError(f"routing/TDM scale {label} input is missing")
    roots = {
        "baseline": (baseline_route_root.resolve(), baseline_tdm_root.resolve()),
        "upgrade": (upgrade_route_root.resolve(), upgrade_tdm_root.resolve()),
    }
    arms: Dict[str, Any] = {}
    expected = {
        "baseline": (ROUTE_TDM_PROVIDER, TDM_BASELINE_PROVIDER),
        "upgrade": (GLOBAL_CANDIDATE_PROVIDER, TDM_ACADEMIC_SCHEDULE_PROVIDER),
    }
    runtimes = {
        "baseline": baseline_runtime_seconds,
        "upgrade": upgrade_runtime_seconds,
    }
    normalized_constraints = load_route_constraints(
        source_paths["route_constraints"], Platform.load(source_paths["platform"])
    )
    for label, (route_root, tdm_root) in roots.items():
        routes_path = route_root / "routes.json"
        normalized_constraints_path = (
            route_root / "route_constraints.normalized.json"
        )
        schedule_path = tdm_root / "schedule.json"
        ratio_path = tdm_root / "ratio_plan.json"
        for artifact_label, path in (("routes", routes_path), ("schedule", schedule_path)):
            if path.is_symlink() or not path.is_file():
                raise ValidationError(
                    f"routing/TDM scale {label} {artifact_label} is missing"
                )
        if (
            normalized_constraints_path.is_symlink()
            or not normalized_constraints_path.is_file()
            or read_json(normalized_constraints_path) != normalized_constraints
        ):
            raise ValidationError(
                f"routing/TDM scale {label} normalized route constraints differ"
            )
        routes = read_json(routes_path)
        schedule = read_json(schedule_path)
        route_provider, tdm_provider = expected[label]
        if routes.get("provider") != route_provider or schedule.get("provider") != tdm_provider:
            raise ValidationError(f"routing/TDM scale {label} provider differs")
        route_validation = validate_phase4(
            source_paths["assignment"],
            source_paths["platform"],
            routes_path,
            source_paths["timing_paths"],
        )
        tdm_validation = validate_phase5(
            routes_path,
            source_paths["platform"],
            schedule_path,
            ratio_path if ratio_path.is_file() and not ratio_path.is_symlink() else None,
            assignment_path=source_paths["assignment"],
        )
        runtime = runtimes[label]
        if (
            isinstance(runtime, bool)
            or not isinstance(runtime, (int, float))
            or not math.isfinite(float(runtime))
            or float(runtime) < 0.0
        ):
            raise ValidationError(
                f"routing/TDM scale {label} runtime is invalid"
            )
        arms[label] = {
            "route_provider": route_provider,
            "tdm_provider": tdm_provider,
            "route_validation": route_validation,
            "tdm_validation": tdm_validation,
            "metrics": _scale_metrics(route_validation, tdm_validation),
            "runtime_seconds": float(runtime),
            "artifacts": {
                "routes": _sha256(routes_path),
                "schedule": _sha256(schedule_path),
                "ratio_plan": (
                    _sha256(ratio_path)
                    if ratio_path.is_file() and not ratio_path.is_symlink()
                    else None
                ),
            },
        }
    report = {
        "schema": SYSTEM_ROUTE_TDM_SCALE_SCHEMA,
        "status": "pass",
        "qualification": "independent-phase4-phase5-scale-ab",
        "frozen_upstream": {
            label: _sha256(path) for label, path in source_paths.items()
        },
        "arms": arms,
        "delta_upgrade_minus_baseline": {
            field: (
                float(arms["upgrade"]["metrics"][field])
                - float(arms["baseline"]["metrics"][field])
            )
            for field in _SCALE_METRIC_FIELDS
        },
        "runtime_delta_seconds": (
            arms["upgrade"]["runtime_seconds"]
            - arms["baseline"]["runtime_seconds"]
        ),
    }
    report["validation"] = validate_system_route_tdm_scale_comparison(report)
    write_json(output_path, report)
    return report
