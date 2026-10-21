"""End-to-end orchestration for the source-qualified Chimew Phase 6 path."""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterator, Mapping, Optional

from .chimew_bank_channel import (
    evaluate_chimew_bank_channel_assignment,
    validate_chimew_bank_channel_input,
)
from .chimew_binding_storage import read_chimew_binding
from .chimew_grouping import build_chimew_initial_groups
from .chimew_phase6 import (
    CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA,
    CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA_V1,
    CHIMEW_PHASE6_BINDING_PROVIDER,
    chimew_adapter_report_generation_path,
    validate_chimew_adapter_generation,
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
from .pin_plan_storage import read_pin_plan
from .pin_planning import validate_pin_plan
from .platform import Platform
from .position_hints_storage import read_position_hints
from .tdm_schedule_storage import (
    copy_tdm_schedule_storage,
    read_tdm_schedule,
    tdm_schedule_store_path,
)


CHIMEW_PIPELINE_REPORT_SCHEMA = "emuflow.chimew-phase6-pipeline-report/v1"
CHIMEW_PIPELINE_PROVIDER = "source-qualified-chimew-phase6-pipeline-v1"
CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER = "byte-bound-chimew-phase6-pipeline-v2"
CHIMEW_PIPELINE_GENERATION_SCHEMA = "emuflow.chimew-pipeline-generation/v1"

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


@contextmanager
def _pipeline_lock(output_dir: Path, *, exclusive: bool) -> Iterator[None]:
    """Serialize one output's writers and lease its generations to readers."""

    output_dir = Path(output_dir).resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir.parent / f".{output_dir.name}.pipeline.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(
            descriptor,
            fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
        )
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


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


def _committed_pipeline_report(output_dir: Path) -> Mapping[str, Any] | None:
    """Read the report that currently commits the outer bundle."""

    report_path = Path(output_dir) / "pipeline_report.json"
    if not report_path.is_file():
        return None
    try:
        report = read_json(report_path)
    except (OSError, ValueError):
        return None
    return report


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_generation(path: Path) -> None:
    """Durably seal a validated staging tree before its atomic install."""

    directories = [Path(path)]
    for candidate in Path(path).rglob("*"):
        if candidate.is_file():
            with candidate.open("rb") as stream:
                os.fsync(stream.fileno())
        elif candidate.is_dir():
            directories.append(candidate)
    for directory in sorted(
        directories, key=lambda item: len(item.parts), reverse=True
    ):
        _fsync_directory(directory)


def _expected_generation_labels(
    artifacts: Mapping[str, Any], *, source_bound: bool
) -> set[str]:
    expected = set(_BASE_ARTIFACTS)
    if "schedule_store" in artifacts:
        expected.add("schedule_store")
    for label in (
        "adapter_electrical_binding_store",
        "adapter_pin_plan_store",
        "adapter_position_hints_store",
    ):
        if label in artifacts:
            expected.add(label)
    if source_bound:
        expected.update(f"source_{label}" for label in _SOURCE_LABELS)
        if "source_timing_paths" in artifacts:
            expected.add("source_timing_paths")
    return expected


def _validate_pipeline_generation_directory(
    path: Path,
) -> tuple[set[Path], Dict[str, Any]]:
    """Validate one immutable whole-pipeline generation and its adapter group."""

    path = Path(path).resolve()
    if (
        len(path.name) != 64
        or any(character not in "0123456789abcdef" for character in path.name)
        or not path.is_dir()
    ):
        raise ValidationError("Chimew pipeline generation name is invalid")
    manifest_path = path / "generation_manifest.json"
    if (
        not manifest_path.is_file()
        or manifest_path.is_symlink()
        or _sha256(manifest_path) != path.name
    ):
        raise ValidationError("Chimew pipeline generation seal differs")
    manifest = read_json(manifest_path)
    artifacts = manifest.get("artifacts")
    provider = manifest.get("provider")
    source_bound = provider == CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER
    if (
        manifest.get("schema") != CHIMEW_PIPELINE_GENERATION_SCHEMA
        or provider
        not in {CHIMEW_PIPELINE_PROVIDER, CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER}
        or not isinstance(manifest.get("design"), str)
        or not manifest["design"]
        or not isinstance(manifest.get("platform"), str)
        or not manifest["platform"]
        or not isinstance(artifacts, dict)
        or set(artifacts)
        != _expected_generation_labels(artifacts, source_bound=source_bound)
    ):
        raise ValidationError("Chimew pipeline generation manifest is invalid")
    files: set[Path] = {manifest_path.resolve()}
    relative_files = {"generation_manifest.json"}
    artifact_paths: dict[str, Path] = {}
    for label, record in artifacts.items():
        if not isinstance(record, dict):
            raise ValidationError("Chimew pipeline generation artifact is invalid")
        raw_relative = record.get("path")
        supplied_sha = record.get("sha256")
        if not isinstance(raw_relative, str) or not isinstance(supplied_sha, str):
            raise ValidationError("Chimew pipeline generation artifact is invalid")
        relative = PurePosixPath(raw_relative)
        unresolved = path / Path(*relative.parts)
        candidate = unresolved.resolve()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or candidate == path
            or path not in candidate.parents
            or any(
                parent.is_symlink()
                for parent in (unresolved, *unresolved.parents)
                if parent == path or path in parent.parents
            )
            or not candidate.is_file()
            or _sha256(candidate) != supplied_sha
        ):
            raise ValidationError("Chimew pipeline generation artifact seal differs")
        artifact_paths[label] = candidate
        files.add(candidate)
        relative_files.add(relative.as_posix())
    actual_relative = {
        candidate.relative_to(path).as_posix()
        for candidate in path.rglob("*")
        if candidate.is_file() or candidate.is_symlink()
    }
    if actual_relative != relative_files:
        raise ValidationError("Chimew pipeline generation coverage is not exact")
    if len(set(artifact_paths.values())) != len(artifact_paths):
        raise ValidationError("Chimew pipeline generation artifacts alias one another")
    adapter_group = validate_chimew_adapter_generation(
        artifact_paths["adapter_report"]
    )
    inventoried_adapter = {
        candidate
        for label, candidate in artifact_paths.items()
        if label == "adapter_report" or label.startswith("adapter_")
    }
    if adapter_group != inventoried_adapter:
        raise ValidationError("Chimew adapter generation inventory differs")
    semantic = _validate_pipeline_artifact_semantics(
        artifact_paths,
        provider=provider,
    )
    if (
        manifest["design"] != semantic["schedule"].get("design")
        or manifest["platform"] != semantic["platform"].name
    ):
        raise ValidationError("Chimew pipeline generation identity differs")
    return files, semantic


def _garbage_collect_pipeline_generations(
    output_dir: Path,
    current_generation: str,
    previous_report: Mapping[str, Any] | None,
) -> None:
    retire: list[Path] = []
    generations_dir = Path(output_dir) / "generations"
    if generations_dir.is_dir():
        for candidate in generations_dir.iterdir():
            if candidate.name == current_generation or not candidate.is_dir():
                continue
            try:
                _validate_pipeline_generation_directory(candidate)
            except (OSError, ValueError, ValidationError):
                continue
            retire.append(candidate)
    if previous_report is not None:
        artifacts = previous_report.get("artifacts")
        if isinstance(artifacts, dict):
            legacy_roots: set[str] = set()
            for record in artifacts.values():
                raw_path = record.get("path") if isinstance(record, dict) else None
                if not isinstance(raw_path, str):
                    continue
                relative = PurePosixPath(raw_path)
                if relative.parts and relative.parts[0] in {
                    "inputs",
                    "kernels",
                    "sources",
                    "phase6-adapter",
                }:
                    legacy_roots.add(relative.parts[0])
            retire.extend(
                Path(output_dir) / name
                for name in sorted(legacy_roots)
                if (Path(output_dir) / name).is_dir()
            )

    # Move retired trees out of the committed inventory before deleting them.
    # A kill during recursive removal can then leave only sibling scratch; it
    # cannot leave a half-deleted generation that breaks the new report.
    if not retire:
        return
    try:
        quarantine = Path(
            tempfile.mkdtemp(
                prefix=f".{Path(output_dir).name}.gc-",
                suffix=".tmp",
                dir=Path(output_dir).parent,
            )
        )
    except OSError:
        return
    try:
        for index, source in enumerate(retire):
            destination = quarantine / f"{index:08d}-{source.name}"
            try:
                os.replace(source, destination)
            except OSError:
                continue
        if generations_dir.is_dir():
            _fsync_directory(generations_dir)
        _fsync_directory(Path(output_dir))
    finally:
        shutil.rmtree(quarantine, ignore_errors=True)


def _validate_pipeline_artifact_semantics(
    paths: Mapping[str, Path],
    *,
    provider: str,
    legacy_pre_scope: bool = False,
) -> Dict[str, Any]:
    """Validate every logical fact in one sealed pipeline generation."""

    source_bound = provider == CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER
    expected_scope = (
        "byte-bound-source-artifacts"
        if source_bound
        else "declared-digest-artifact-chain"
    )
    schedule = read_tdm_schedule(paths["schedule"])
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
    pin_plan = read_pin_plan(paths["adapter_pin_plan"])
    position_hints = read_position_hints(paths["adapter_position_hints"])
    electrical_binding = read_chimew_binding(
        paths["adapter_electrical_binding"]
    )
    pin_validation = validate_pin_plan(
        schedule, platform, position_hints, pin_plan
    )
    binding_validation = validate_chimew_phase6_binding(
        schedule, platform, pin_plan, electrical_binding
    )
    adapter_report = read_json(paths["adapter_report"])
    if adapter_report.get("schema") not in {
        CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA_V1,
        CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA,
    }:
        raise ValidationError("Chimew adapter report schema is invalid")
    if adapter_report.get("schema") == CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA:
        adapter_artifacts = adapter_report.get("artifacts")
        if not isinstance(adapter_artifacts, dict) or any(
            not isinstance(name, str)
            or Path(name).name != name
            or f"adapter_{label}" not in paths
            or _sha256(paths[f"adapter_{label}"]) not in name.split(".")
            for label, name in adapter_artifacts.items()
        ):
            raise ValidationError(
                "Chimew adapter content-addressed artifact inventory is invalid"
            )
    expected_adapter_labels = {
        "bank_channel_report",
        "position_hints",
        "pin_plan",
        "electrical_binding",
        "qualification_certificate",
    }
    if hasattr(electrical_binding, "manifest"):
        expected_adapter_labels.add("electrical_binding_store")
    if hasattr(pin_plan, "manifest"):
        expected_adapter_labels.add("pin_plan_store")
    if hasattr(position_hints, "manifest"):
        expected_adapter_labels.add("position_hints_store")
    expected_adapter_artifacts = {
        label: paths[f"adapter_{label}"].name
        for label in expected_adapter_labels
    }
    expected_adapter_report = {
        "schema": adapter_report["schema"],
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
        "artifacts": expected_adapter_artifacts,
    }
    if adapter_report != expected_adapter_report:
        raise ValidationError("Chimew pipeline adapter report does not agree")
    return {
        "expected_scope": expected_scope,
        "schedule": schedule,
        "platform": platform,
        "qualification": qualification,
        "qualification_validation": qualification_validation,
        "pin_validation": pin_validation,
        "binding_validation": binding_validation,
    }


def _run_chimew_phase6_pipeline_locked(
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

    output_dir = Path(output_dir)
    schedule = read_tdm_schedule(schedule_path)
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

    provider = (
        CHIMEW_SOURCE_BOUND_PIPELINE_PROVIDER
        if source_paths is not None
        else CHIMEW_PIPELINE_PROVIDER
    )
    previous_report = _committed_pipeline_report(output_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output_dir.name}.generation-",
        suffix=".tmp",
        dir=output_dir.parent,
    ) as temporary:
        staging_container = Path(temporary)
        generation_root = staging_container / "payload"
        generation_root.mkdir()
        inputs_dir = generation_root / "inputs"
        kernels_dir = generation_root / "kernels"
        adapter_dir = generation_root / "phase6-adapter"
        inputs_dir.mkdir(parents=True)
        kernels_dir.mkdir(parents=True)
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
            sources_dir = generation_root / "sources"
            sources_dir.mkdir(parents=True)
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
            defer_generation_gc=True,
        )
        if adapter_report.get("lookahead_qualification") != "complete-artifact-chain":
            raise ValidationError("Chimew pipeline did not produce a complete binding")
        artifact_paths["adapter_report"] = chimew_adapter_report_generation_path(
            adapter_dir, adapter_report
        )
        for label, name in adapter_report["artifacts"].items():
            artifact_paths[f"adapter_{label}"] = adapter_dir / name

        manifest = {
            "schema": CHIMEW_PIPELINE_GENERATION_SCHEMA,
            "design": schedule["design"],
            "platform": schedule["platform"],
            "provider": provider,
            "artifacts": {
                label: {
                    "path": path.relative_to(generation_root).as_posix(),
                    "sha256": _sha256(path),
                }
                for label, path in sorted(artifact_paths.items())
            },
        }
        manifest_path = generation_root / "generation_manifest.json"
        write_json(manifest_path, manifest)
        generation_id = _sha256(manifest_path)
        staged_generation = staging_container / generation_id
        os.replace(generation_root, staged_generation)
        _fsync_directory(staging_container)
        _validate_pipeline_generation_directory(staged_generation)
        _fsync_generation(staged_generation)
        generations_dir = output_dir / "generations"
        generations_dir.mkdir(parents=True, exist_ok=True)
        final_generation = generations_dir / generation_id
        if final_generation.exists():
            _validate_pipeline_generation_directory(final_generation)
            if (
                (final_generation / "generation_manifest.json").read_bytes()
                != (staged_generation / "generation_manifest.json").read_bytes()
            ):
                raise ValidationError("Chimew pipeline generation collision")
        else:
            os.replace(staged_generation, final_generation)
            _fsync_directory(generations_dir)

        final_artifact_paths = {
            label: final_generation / path.relative_to(generation_root)
            for label, path in artifact_paths.items()
        }
        final_artifact_paths["generation_manifest"] = (
            final_generation / "generation_manifest.json"
        )
        report = {
            "schema": CHIMEW_PIPELINE_REPORT_SCHEMA,
            "status": "pass",
            "design": schedule["design"],
            "platform": schedule["platform"],
            "provider": provider,
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
                    "path": path.relative_to(output_dir).as_posix(),
                    "sha256": _sha256(path),
                }
                for label, path in sorted(final_artifact_paths.items())
            },
        }
        write_json(output_dir / "pipeline_report.json", report)
        _fsync_directory(output_dir)
        _garbage_collect_pipeline_generations(
            output_dir, generation_id, previous_report
        )
        return report


def _validate_chimew_phase6_pipeline_locked(output_dir: Path) -> Dict[str, Any]:
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
    generation_managed = "generation_manifest" in artifacts
    logical_artifacts = {
        label: record
        for label, record in artifacts.items()
        if label != "generation_manifest"
    }
    expected_labels = _expected_generation_labels(
        logical_artifacts, source_bound=source_bound
    )
    if generation_managed:
        expected_labels.add("generation_manifest")
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
    referenced_files = set(paths.values())
    if generation_managed:
        current_generation = paths["generation_manifest"].parent
        current_generation_files, semantic = (
            _validate_pipeline_generation_directory(current_generation)
        )
        inventoried_generation_files = {
            path
            for label, path in paths.items()
            if label != "generation_manifest"
        } | {paths["generation_manifest"]}
        if current_generation_files != inventoried_generation_files:
            raise ValidationError(
                "Chimew committed generation inventory differs"
            )
    missing_files = referenced_files - actual_files
    unexpected_files = actual_files - referenced_files
    allowed_orphans: set[Path] = set()
    generations_dir = output_dir / "generations"
    if generations_dir.is_dir():
        for candidate in generations_dir.iterdir():
            if not candidate.is_dir():
                continue
            try:
                candidate_files, _candidate_semantic = (
                    _validate_pipeline_generation_directory(candidate)
                )
            except (OSError, ValueError, ValidationError):
                continue
            if candidate_files.isdisjoint(referenced_files):
                allowed_orphans.update(candidate_files)
    if missing_files or unexpected_files - allowed_orphans:
        raise ValidationError("Chimew pipeline artifact coverage is not exact")

    if not generation_managed:
        semantic = _validate_pipeline_artifact_semantics(
            paths,
            provider=report["provider"],
            legacy_pre_scope=legacy_pre_scope,
        )
    schedule = semantic["schedule"]
    platform = semantic["platform"]
    qualification = semantic["qualification"]
    qualification_validation = semantic["qualification_validation"]
    pin_validation = semantic["pin_validation"]
    binding_validation = semantic["binding_validation"]
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
    """Run one writer under the output's exclusive publication lock."""

    with _pipeline_lock(output_dir, exclusive=True):
        return _run_chimew_phase6_pipeline_locked(
            schedule_path,
            platform_path,
            crossings_path,
            positions_path,
            rudy_input_path,
            bank_channel_input_path,
            electrical_map_path,
            output_dir,
            source_paths=source_paths,
            grouper=grouper,
            refiner=refiner,
            rudy=rudy,
            assigner=assigner,
            region_count=region_count,
        )


@contextmanager
def read_chimew_phase6_pipeline(
    output_dir: Path,
    *,
    expected_report_sha256: Optional[str] = None,
) -> Iterator[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Lease a validated report and every referenced generation artifact."""

    with _pipeline_lock(output_dir, exclusive=False):
        report_path = Path(output_dir) / "pipeline_report.json"
        if (
            expected_report_sha256 is not None
            and _sha256(report_path) != expected_report_sha256
        ):
            raise ValidationError("Chimew pipeline report hash differs")
        validation = _validate_chimew_phase6_pipeline_locked(output_dir)
        report = read_json(report_path)
        yield report, validation


def validate_chimew_phase6_pipeline(output_dir: Path) -> Dict[str, Any]:
    """Validate one bundle while holding its shared generation lease."""

    with read_chimew_phase6_pipeline(output_dir) as (_report, validation):
        return dict(validation)
