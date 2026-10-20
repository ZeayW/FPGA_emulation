"""End-to-end orchestration for the source-qualified Chimew Phase 6 path."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping, Optional

from .chimew_bank_channel import (
    evaluate_chimew_bank_channel_assignment,
    validate_chimew_bank_channel_input,
)
from .chimew_grouping import build_chimew_initial_groups
from .chimew_phase6 import (
    CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA,
    CHIMEW_PHASE6_BINDING_PROVIDER,
    run_chimew_phase6_adapter,
    validate_chimew_electrical_map,
    validate_chimew_phase6_binding,
)
from .chimew_qualification import (
    build_chimew_phase6_qualification,
    canonical_sha256,
    validate_chimew_phase6_qualification,
)
from .chimew_refinement import refine_chimew_groups
from .chimew_rudy import evaluate_chimew_rudy
from .errors import ValidationError
from .io import read_json, write_json
from .pin_planning import validate_pin_plan
from .platform import Platform
from .tdm_schedule_storage import (
    copy_tdm_schedule_storage,
    tdm_schedule_store_path,
)


CHIMEW_PIPELINE_REPORT_SCHEMA = "emuflow.chimew-phase6-pipeline-report/v1"
CHIMEW_PIPELINE_PROVIDER = "source-qualified-chimew-phase6-pipeline-v1"
CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER = "byte-bound-chimew-phase6-pipeline-v2"

_BASE_ARTIFACTS = {
    "schedule",
    "platform",
    "crossings",
    "positions",
    "rudy_input",
    "bank_channel_input",
    "electrical_map",
    "initial_grouping",
    "refined_grouping",
    "rudy_report",
    "bank_channel_report",
    "qualification",
    "adapter_report",
    "adapter_bank_channel_report",
    "adapter_electrical_binding",
    "adapter_pin_plan",
    "adapter_position_hints",
    "adapter_qualification_certificate",
}
_SOURCE_LABELS = (
    "routing",
    "placement",
    "netlist",
    "architecture",
    "package_pins",
)
_OPTIONAL_SOURCE_LABELS = ("timing_paths",)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_digests(
    crossings: Mapping[str, Any],
    positions: Mapping[str, Any],
    rudy_input: Mapping[str, Any],
    electrical_map: Mapping[str, Any],
    timing_paths_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    crossing_provenance = crossings.get("provenance")
    position_provenance = positions.get("provenance")
    rudy_provenance = rudy_input.get("provenance")
    electrical_provenance = electrical_map.get("provenance")
    if not isinstance(crossing_provenance, Mapping):
        crossing_provenance = {}
    if not isinstance(position_provenance, Mapping):
        position_provenance = {}
    if not isinstance(rudy_provenance, Mapping):
        rudy_provenance = {}
    if not isinstance(electrical_provenance, Mapping):
        electrical_provenance = {}
    result = {
        "routing": crossing_provenance.get("routing_sha256"),
        "placement": position_provenance.get("placement_sha256"),
        "netlist": rudy_provenance.get("netlist_sha256"),
        "architecture": rudy_provenance.get("architecture_sha256"),
        "package_pins": electrical_provenance.get(
            "package_pin_inventory_sha256"
        ),
    }
    if timing_paths_sha256 is not None:
        result["timing_paths"] = timing_paths_sha256
    return result


def _artifact_path(output_dir: Path, label: str, record: Any) -> Path:
    if not isinstance(record, dict):
        raise ValidationError(f"Chimew pipeline artifact {label!r} is invalid")
    raw_path = record.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValidationError(f"Chimew pipeline artifact {label!r} path is invalid")
    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError(f"Chimew pipeline artifact {label!r} path is unsafe")
    root = output_dir.resolve()
    path = (root / Path(*relative.parts)).resolve()
    if path == root or root not in path.parents or not path.is_file():
        raise ValidationError(f"Chimew pipeline artifact {label!r} is missing")
    supplied_sha = record.get("sha256")
    if not isinstance(supplied_sha, str) or _sha256(path) != supplied_sha:
        raise ValidationError(f"Chimew pipeline artifact {label!r} hash differs")
    return path


def run_chimew_phase6_pipeline(
    schedule_path: Path,
    platform_path: Path,
    crossings_path: Path,
    positions_path: Path,
    rudy_input_path: Path,
    bank_channel_input_path: Path,
    electrical_map_path: Path,
    output_dir: Path,
    *,
    source_paths: Optional[Mapping[str, Path]] = None,
    grouper: Optional[str] = None,
    refiner: Optional[str] = None,
    rudy: Optional[str] = None,
    assigner: Optional[str] = None,
    region_count: int = 31,
) -> Dict[str, Any]:
    """Run, certify, and electrically bind every Chimew Phase 6 kernel."""

    schedule = read_json(schedule_path)
    crossings = read_json(crossings_path)
    positions = read_json(positions_path)
    rudy_input = read_json(rudy_input_path)
    bank_input = read_json(bank_channel_input_path)
    electrical_map = read_json(electrical_map_path)

    initial = build_chimew_initial_groups(
        schedule, crossings, executable=grouper
    )
    refined = refine_chimew_groups(
        schedule,
        crossings,
        initial,
        positions,
        executable=refiner,
    )
    provenance = bank_input.get("provenance")
    if (
        not isinstance(provenance, dict)
        or provenance.get("grouping_sha256") != canonical_sha256(refined)
    ):
        raise ValidationError(
            "Chimew bank/channel input does not bind the refined grouping"
        )
    rudy_report = evaluate_chimew_rudy(rudy_input, executable=rudy)
    if rudy_report.get("gate_status") != "pass":
        raise ValidationError("Chimew RUDY qualification gate did not pass")
    bank_report = evaluate_chimew_bank_channel_assignment(
        bank_input, executable=assigner
    )
    source_binding = None
    if source_paths is not None:
        required_labels = set(_SOURCE_LABELS)
        if not required_labels <= set(source_paths) or set(source_paths) - (
            required_labels | set(_OPTIONAL_SOURCE_LABELS)
        ):
            raise ValidationError(
                "Chimew source bundle must provide routing, placement, netlist, "
                "architecture, package pins, and only recognized optional sources"
            )
        expected_digests = _source_digests(
            crossings,
            positions,
            rudy_input,
            electrical_map,
            (
                _sha256(Path(source_paths["timing_paths"]))
                if "timing_paths" in source_paths
                else None
            ),
        )
        for label in sorted(source_paths):
            source = Path(source_paths[label])
            if not source.is_file() or _sha256(source) != expected_digests[label]:
                raise ValidationError(
                    f"Chimew {label} source does not match declared provenance"
                )
        source_binding = {
            "scope": "byte-bound-source-artifacts",
            "digests": expected_digests,
        }

    qualification = build_chimew_phase6_qualification(
        schedule,
        crossings,
        initial,
        positions,
        refined,
        rudy_input,
        rudy_report,
        bank_input,
        bank_report,
        source_binding=source_binding,
    )

    inputs_dir = output_dir / "inputs"
    kernels_dir = output_dir / "kernels"
    adapter_dir = output_dir / "phase6-adapter"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    kernels_dir.mkdir(parents=True, exist_ok=True)
    input_sources = {
        "schedule": schedule_path,
        "platform": platform_path,
        "crossings": crossings_path,
        "positions": positions_path,
        "rudy_input": rudy_input_path,
        "bank_channel_input": bank_channel_input_path,
        "electrical_map": electrical_map_path,
    }
    input_names = {
        "schedule": "schedule.json",
        "platform": "platform.json",
        "crossings": "crossings.json",
        "positions": "positions.json",
        "rudy_input": "rudy_input.json",
        "bank_channel_input": "bank_channel_input.json",
        "electrical_map": "electrical_map.json",
    }
    artifact_paths: Dict[str, Path] = {}
    for label, source in input_sources.items():
        destination = inputs_dir / input_names[label]
        if label == "schedule":
            copy_tdm_schedule_storage(source, destination)
        else:
            shutil.copy2(source, destination)
        artifact_paths[label] = destination
        if label == "schedule":
            schedule_store = tdm_schedule_store_path(destination)
            if schedule_store is not None:
                artifact_paths["schedule_store"] = schedule_store
    if source_paths is not None:
        sources_dir = output_dir / "sources"
        sources_dir.mkdir(parents=True, exist_ok=True)
        for label in sorted(source_paths):
            source = Path(source_paths[label])
            destination = sources_dir / f"{label}.source"
            shutil.copy2(source, destination)
            artifact_paths[f"source_{label}"] = destination
    kernel_documents = {
        "initial_grouping": initial,
        "refined_grouping": refined,
        "rudy_report": rudy_report,
        "bank_channel_report": bank_report,
        "qualification": qualification,
    }
    for label, document in kernel_documents.items():
        path = kernels_dir / f"{label}.json"
        write_json(path, document)
        artifact_paths[label] = path

    adapter_report = run_chimew_phase6_adapter(
        artifact_paths["schedule"],
        artifact_paths["platform"],
        artifact_paths["bank_channel_input"],
        artifact_paths["electrical_map"],
        adapter_dir,
        qualification_path=artifact_paths["qualification"],
        bank_channel_report_path=artifact_paths["bank_channel_report"],
        executable=assigner,
        region_count=region_count,
    )
    if adapter_report.get("lookahead_qualification") != "complete-artifact-chain":
        raise ValidationError("Chimew pipeline did not produce a complete binding")
    artifact_paths["adapter_report"] = adapter_dir / "adapter_report.json"
    for label, name in adapter_report["artifacts"].items():
        artifact_paths[f"adapter_{label}"] = adapter_dir / name

    report = {
        "schema": CHIMEW_PIPELINE_REPORT_SCHEMA,
        "status": "pass",
        "design": schedule["design"],
        "platform": schedule["platform"],
        "provider": (
            CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER
            if source_paths is not None
            else CHIMEW_PIPELINE_PROVIDER
        ),
        "qualification_scope": (
            "byte-bound-source-artifacts"
            if source_paths is not None
            else "declared-digest-artifact-chain"
        ),
        "qualification_sha256": qualification["qualification_sha256"],
        "metrics": {
            "signals": qualification["metrics"]["signals"],
            "groups": qualification["metrics"]["groups"],
            "rudy_peak_utilization": qualification["metrics"][
                "rudy_peak_utilization"
            ],
            "rudy_overloaded_bins": 0,
            "artifact_chain_disagreements": 0,
        },
        "artifacts": {
            label: {
                "path": str(path.relative_to(output_dir)),
                "sha256": _sha256(path),
            }
            for label, path in sorted(artifact_paths.items())
        },
    }
    write_json(output_dir / "pipeline_report.json", report)
    return report


def validate_chimew_phase6_pipeline(output_dir: Path) -> Dict[str, Any]:
    """Independently validate a frozen pipeline bundle without reoptimizing it."""

    output_dir = Path(output_dir)
    report = read_json(output_dir / "pipeline_report.json")
    if (
        report.get("schema") != CHIMEW_PIPELINE_REPORT_SCHEMA
        or report.get("status") != "pass"
        or report.get("provider")
        not in {CHIMEW_PIPELINE_PROVIDER, CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER}
    ):
        raise ValidationError("Chimew pipeline report identity is invalid")
    source_bound = report.get("provider") == CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER
    expected_scope = (
        "byte-bound-source-artifacts"
        if source_bound
        else "declared-digest-artifact-chain"
    )
    supplied_scope = report.get("qualification_scope")
    legacy_pre_scope = not source_bound and supplied_scope is None
    if supplied_scope != expected_scope and not (
        not source_bound and supplied_scope is None
    ):
        raise ValidationError("Chimew pipeline qualification scope is invalid")
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValidationError("Chimew pipeline artifact inventory is invalid")
    expected_labels = set(_BASE_ARTIFACTS)
    if "schedule_store" in artifacts:
        expected_labels.add("schedule_store")
    if source_bound:
        expected_labels.update(f"source_{label}" for label in _SOURCE_LABELS)
        if "source_timing_paths" in artifacts:
            expected_labels.add("source_timing_paths")
    if set(artifacts) != expected_labels:
        raise ValidationError("Chimew pipeline artifact inventory is invalid")
    paths = {
        label: _artifact_path(output_dir, label, record)
        for label, record in artifacts.items()
    }
    if len(set(paths.values())) != len(paths):
        raise ValidationError("Chimew pipeline artifacts alias one another")
    actual_files = {
        path.resolve()
        for path in output_dir.rglob("*")
        if path.is_file() and path.name != "pipeline_report.json"
    }
    if actual_files != set(paths.values()):
        raise ValidationError("Chimew pipeline artifact coverage is not exact")

    schedule = read_json(paths["schedule"])
    platform = Platform.load(paths["platform"])
    crossings = read_json(paths["crossings"])
    positions = read_json(paths["positions"])
    initial = read_json(paths["initial_grouping"])
    refined = read_json(paths["refined_grouping"])
    rudy_input = read_json(paths["rudy_input"])
    rudy_report = read_json(paths["rudy_report"])
    bank_input = read_json(paths["bank_channel_input"])
    bank_report = read_json(paths["bank_channel_report"])
    electrical_map = read_json(paths["electrical_map"])
    electrical_provenance = electrical_map.get("provenance")
    if (
        not isinstance(electrical_provenance, dict)
        or electrical_provenance.get("boarddb_sha256")
        != _sha256(paths["platform"])
    ):
        raise ValidationError("Chimew electrical map BoardDB hash differs")
    validate_chimew_electrical_map(
        electrical_map,
        platform,
        validate_chimew_bank_channel_input(bank_input),
    )
    source_binding = None
    if source_bound:
        expected_digests = _source_digests(
            crossings,
            positions,
            rudy_input,
            electrical_map,
            (
                _sha256(paths["source_timing_paths"])
                if "source_timing_paths" in paths
                else None
            ),
        )
        for label in sorted(expected_digests):
            if _sha256(paths[f"source_{label}"]) != expected_digests[label]:
                raise ValidationError(
                    f"Chimew {label} source provenance does not agree"
                )
        source_binding = {
            "scope": "byte-bound-source-artifacts",
            "digests": expected_digests,
        }

    qualification = read_json(paths["qualification"])
    qualification_validation = validate_chimew_phase6_qualification(
        qualification,
        schedule,
        crossings,
        initial,
        positions,
        refined,
        rudy_input,
        rudy_report,
        bank_input,
        bank_report,
        source_binding=source_binding,
    )
    if read_json(paths["adapter_bank_channel_report"]) != bank_report:
        raise ValidationError("Chimew adapter bank/channel report differs")
    if read_json(paths["adapter_qualification_certificate"]) != qualification:
        raise ValidationError("Chimew adapter qualification certificate differs")
    pin_plan = read_json(paths["adapter_pin_plan"])
    position_hints = read_json(paths["adapter_position_hints"])
    electrical_binding = read_json(paths["adapter_electrical_binding"])
    pin_validation = validate_pin_plan(
        schedule, platform, position_hints, pin_plan
    )
    binding_validation = validate_chimew_phase6_binding(
        schedule, platform, pin_plan, electrical_binding
    )
    adapter_report = read_json(paths["adapter_report"])
    expected_adapter_report = {
        "schema": CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA,
        "status": "pass",
        "provider": CHIMEW_PHASE6_BINDING_PROVIDER,
        "design": schedule["design"],
        "platform": platform.name,
        "paper_provider": bank_report["provider"],
        "validation": pin_validation,
        "electrical_metrics": electrical_binding["metrics"],
        "lookahead_qualification": "complete-artifact-chain",
        "qualification_validation": (
            {
                key: value
                for key, value in qualification_validation.items()
                if key != "qualification_scope"
            }
            if legacy_pre_scope
            else qualification_validation
        ),
        "artifacts": {
            "bank_channel_report": "bank_channel_report.json",
            "position_hints": "position_hints.json",
            "pin_plan": "pin_plan.json",
            "electrical_binding": "electrical_binding.json",
            "qualification_certificate": "qualification_certificate.json",
        },
    }
    if adapter_report != expected_adapter_report:
        raise ValidationError("Chimew pipeline adapter report does not agree")
    metrics = report.get("metrics")
    expected_metrics = {
        "signals": qualification_validation["signals"],
        "groups": qualification_validation["groups"],
        "rudy_peak_utilization": qualification["metrics"][
            "rudy_peak_utilization"
        ],
        "rudy_overloaded_bins": 0,
        "artifact_chain_disagreements": 0,
    }
    if (
        report.get("design") != schedule.get("design")
        or report.get("platform") != platform.name
        or report.get("qualification_sha256")
        != qualification_validation["qualification_sha256"]
        or metrics != expected_metrics
    ):
        raise ValidationError("Chimew pipeline summary does not agree")
    return {
        "status": "pass",
        "provider": report["provider"],
        "qualification_scope": expected_scope,
        "signals": qualification_validation["signals"],
        "groups": qualification_validation["groups"],
        "qualification_sha256": qualification_validation[
            "qualification_sha256"
        ],
        "pin_plan": pin_validation,
        "electrical_binding": binding_validation,
        "artifact_hashes_verified": len(paths),
    }
