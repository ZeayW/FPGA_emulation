from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, Dict, Mapping, Optional

from .cross_layer_timing import (
    build_cross_layer_timing_contract,
    validate_cross_layer_timing_contract,
)
from .errors import TDMScheduleInfeasibleError, ValidationError
from .io import read_json, write_json

from .phase3_storage import read_phase3_assignment_view
from .platform import Platform
from .routing import semantic_contract_for_routes
from .tdm import (
    TDM_EXACT_DEPENDENCY_PROVIDER,
    NativeExactScheduleRequest,
    build_native_exact_schedule_request,
    reconstruct_tdm_schedule_timing,
    validate_tdm_schedule,
)

from .tdm_schedule_storage import (
    read_tdm_schedule,
    tdm_schedule_store_path,
    write_tdm_schedule_storage,
)

from .system_route_storage import read_system_routes

PHASE5_REPORT_SCHEMA = "emuflow.phase5-report/v1"
PHASE5_GENERATION_SCHEMA = "emuflow.phase5-generation/v1"

def _canonical_phase5_report_identity(report: Mapping[str, Any]) -> str:
    value = dict(report)
    generation = report.get("generation")
    if isinstance(generation, Mapping):
        generation_value = dict(generation)
        generation_value.pop("report_identity_sha256", None)
        value["generation"] = generation_value
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

def _phase5_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()

def validate_phase5_generation(
    phase5_report_path: Path,
    *,
    managed_root_sealed: bool = False,
    allow_legacy: bool = False,
) -> Dict[str, Any]:
    """Validate the report-last Phase 5 generation before opening its data."""

    phase5_report_path = Path(phase5_report_path)
    if not phase5_report_path.is_file() or phase5_report_path.is_symlink():
        if allow_legacy:
            return {"status": "legacy", "report": None}
        raise ValidationError("Phase 5 generation commit report is missing")
    try:
        with phase5_report_path.open("r", encoding="utf-8") as stream:
            report = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValidationError("Phase 5 generation commit report is unreadable") from error
    if not isinstance(report, dict):
        raise ValidationError("Phase 5 generation commit report is invalid")
    generation = report.get("generation")
    if generation is None:
        if allow_legacy:
            return {"status": "legacy", "report": report}
        raise ValidationError("Phase 5 generation seal is missing")
    if (
        report.get("schema") != PHASE5_REPORT_SCHEMA
        or report.get("status") != "pass"
        or not isinstance(generation, dict)
        or generation.get("schema") != PHASE5_GENERATION_SCHEMA
        or generation.get("commit_point") != "phase5_report.json"
        or phase5_report_path.name != "phase5_report.json"
    ):
        raise ValidationError("Phase 5 generation seal is invalid")
    identity = generation.get("report_identity_sha256")
    if (
        not isinstance(identity, str)
        or len(identity) != 64
        or any(character not in "0123456789abcdef" for character in identity)
        or identity != _canonical_phase5_report_identity(report)
    ):
        raise ValidationError("Phase 5 report identity is invalid")
    artifacts = report.get("artifacts")
    members = generation.get("members")
    if not isinstance(artifacts, dict) or not isinstance(members, list):
        raise ValidationError("Phase 5 generation member table is invalid")
    artifact_paths = {
        value
        for key, value in artifacts.items()
        if key != "report" and isinstance(value, str)
    }
    if (
        artifacts.get("report") != "phase5_report.json"
        or artifacts.get("schedule") != "schedule.json"
        or artifacts.get("cross_layer_timing")
        != "cross_layer_timing.json"
        or len(artifact_paths) != len(artifacts) - 1
        or len(members) != len(artifact_paths)
    ):
        raise ValidationError("Phase 5 generation artifact inventory is invalid")
    member_by_path: Dict[str, Mapping[str, Any]] = {}
    for member in members:
        if not isinstance(member, dict) or set(member) != {
            "path",
            "bytes",
            "sha256",
        }:
            raise ValidationError("Phase 5 generation member record is invalid")
        relative = member.get("path")
        size = member.get("bytes")
        digest = member.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).name != relative
            or relative in member_by_path
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValidationError("Phase 5 generation member record is invalid")
        member_by_path[relative] = member
    if set(member_by_path) != artifact_paths:
        raise ValidationError("Phase 5 generation member coverage is invalid")
    root = phase5_report_path.parent
    for relative, member in member_by_path.items():
        path = root / relative
        try:
            valid = (
                path.is_file()
                and not path.is_symlink()
                and path.stat().st_size == member["bytes"]
            )
        except OSError:
            valid = False
        if not valid:
            raise ValidationError(
                f"Phase 5 generation member is missing: {relative}"
            )
        if not managed_root_sealed and _phase5_file_sha256(path) != member["sha256"]:
            raise ValidationError(
                f"Phase 5 generation member seal is broken: {relative}"
            )
    schedule_member = member_by_path.get("schedule.json")
    store_name = artifacts.get("schedule_store")
    store_member = member_by_path.get(store_name) if isinstance(store_name, str) else None
    if schedule_member is None or store_member is None:
        raise ValidationError("Phase 5 generation schedule members are missing")
    try:
        with (root / "schedule.json").open("r", encoding="utf-8") as stream:
            schedule_selector = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValidationError("Phase 5 schedule selector is unreadable") from error
    if (
        not isinstance(schedule_selector, dict)
        or schedule_selector.get("artifact") != store_name
        or schedule_selector.get("store_bytes") != store_member["bytes"]
        or schedule_selector.get("store_sha256") != store_member["sha256"]
    ):
        raise ValidationError("Phase 5 schedule selector generation disagrees")
    return {"status": "pass", "report": report}

def _replace_phase5_generation_file(source: Path, destination: Path) -> None:
    """Atomic replacement hook kept separate for failure-injection tests."""

    os.replace(source, destination)

def _fsync_phase5_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def _fsync_phase5_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def _commit_phase5_generation(
    staging: Path,
    output_dir: Path,
    relative_paths: list[str],
) -> None:
    """Publish one Phase 5 generation with complete exception rollback.

    Every member is fully written in ``staging`` first. The report is ordered
    last and is the generation commit point. If any replacement raises after
    one or more members became visible, hard-linked backups restore the prior
    generation byte-for-byte and newly introduced members are removed.
    """

    if not relative_paths or relative_paths[-1] != "phase5_report.json":
        raise ValidationError("Phase 5 report must commit the generation")
    rollback_root = staging / ".rollback"
    changes: list[tuple[Path, Path | None]] = []
    try:
        # A durable report-last commit needs more than rename ordering. Every
        # staged payload must reach stable storage, and the directory entries
        # for all pre-report members must be durable before the report can
        # become durable. A crash before the final directory sync therefore
        # leaves either the old report or a report whose member seal rejects.
        for relative in relative_paths:
            source = staging / relative
            if not source.is_file():
                raise ValidationError(
                    f"Phase 5 staged generation member is missing: {relative}"
                )
            _fsync_phase5_file(source)

        for index, relative in enumerate(relative_paths):
            source = staging / relative
            destination = output_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            backup = None
            if destination.exists():
                if not destination.is_file() or destination.is_symlink():
                    raise ValidationError(
                        f"Phase 5 generation destination is invalid: {relative}"
                    )
                backup = rollback_root / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(destination, backup)
                except OSError:
                    shutil.copy2(destination, backup)
            changes.append((destination, backup))
            _replace_phase5_generation_file(source, destination)
            if index == len(relative_paths) - 2:
                _fsync_phase5_directory(output_dir)
        _fsync_phase5_directory(output_dir)
    except BaseException:
        for destination, backup in reversed(changes):
            if backup is None:
                destination.unlink(missing_ok=True)
            elif backup.exists():
                os.replace(backup, destination)
        _fsync_phase5_directory(output_dir)
        raise

def _load_phase3_semantics(
    routes: Dict[str, Any], assignment_path: Optional[Path]
) -> tuple[Dict[str, Any], Optional[Mapping[str, Any]]]:
    """Hydrate Phase-3-owned semantics without persisting a second copy."""

    if "semantic_contract" in routes:
        raise ValidationError(
            "Phase 4 routes may not duplicate the Phase 3 semantic contract"
        )
    schema = routes.get("semantic_contract_schema")
    digest = routes.get("semantic_contract_sha256")
    if schema is None and digest is None:
        return routes, None
    if not isinstance(schema, str) or not isinstance(digest, str):
        raise ValidationError("Phase 4 semantic contract binding is incomplete")
    if assignment_path is None:
        raise ValidationError(
            "sampled virtual-wire Phase 5 requires the Phase 3 assignment"
        )
    # Phase 4 already binds every routed cut to the Phase 3 semantic digest
    # and carries the complete cut-dependency DAG.  Keep the graph-sized
    # logic/capture relation in the Phase 3 SQLite data plane instead of
    # inflating the compressed (multi-GB) contract into Python objects.
    assignment = read_phase3_assignment_view(
        assignment_path,
        include_semantic_contract=False,
    )
    contract = semantic_contract_for_routes(assignment, routes)
    if contract is None:
        raise ValidationError("Phase 3 semantic contract is missing")
    return {**routes, "semantic_contract": contract}, contract

def run_phase5(
    routes_path: Path, platform_path: Path, output_dir: Path,
    assignment_path: Optional[Path] = None,
    provider: str = TDM_EXACT_DEPENDENCY_PROVIDER,
) -> Dict[str, Any]:
    if provider not in {None, TDM_EXACT_DEPENDENCY_PROVIDER}:
        raise ValueError(f"unsupported Phase 5 provider {provider!r}")
    persisted_routes = read_system_routes(routes_path)
    routes, semantic_contract = _load_phase3_semantics(persisted_routes, assignment_path)
    if semantic_contract is None:
        raise ValidationError("Phase 5 requires the Static Exact dependency contract")
    platform = Platform.load(platform_path)
    schedule = build_native_exact_schedule_request(routes, platform)
    if schedule is None:
        raise ValidationError("Phase 5 requires the current typed RouteStore and indexed Static Exact contract")
    return _finish_phase5_generation(
        persisted_routes=persisted_routes, routes=routes,
        semantic_contract=semantic_contract, schedule=schedule,
        platform=platform, output_dir=output_dir,
    )

def _finish_phase5_generation(
    *, persisted_routes: Mapping[str, Any], routes: Mapping[str, Any],
    semantic_contract: Mapping[str, Any], schedule: NativeExactScheduleRequest,
    platform: Platform, output_dir: Path,
) -> Dict[str, Any]:
    """Build every Phase 5 artifact in staging and commit the report last."""

    validation = None
    timing_validation = None
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".phase5-generation-", dir=output_dir
    ) as temporary_directory:
        staging = Path(temporary_directory)
        staged_schedule_path = staging / "schedule.json"
        schedule_storage_started = time.monotonic()
        schedule_storage = write_tdm_schedule_storage(
            staged_schedule_path, schedule
        )
        schedule_storage_wall_seconds = (
            time.monotonic() - schedule_storage_started
        )
        persisted_schedule = read_tdm_schedule(staged_schedule_path, verify_store_hash=False)
        # Independently reconstruct from typed cursor/join relations before
        # committing the report; never inflate a private native result into
        # the generic validator's full hop dictionaries.
        validation = validate_tdm_schedule(routes, platform, persisted_schedule)
        schedule = persisted_schedule
        if isinstance(routes.get("timing"), dict):
            timing_validation = reconstruct_tdm_schedule_timing(
                routes, platform, persisted_schedule,
            )
        cross_layer_timing = build_cross_layer_timing_contract(
            persisted_routes, persisted_schedule, semantic_contract
        )
        staged_store_path = tdm_schedule_store_path(staged_schedule_path)
        if staged_store_path is None:
            raise ValidationError("Phase 5 schedule store is missing")

        cross_layer_validation = {
            "status": "pass",
            **cross_layer_timing.get("metrics", {}),
        }

        report: Dict[str, Any] = {
            "schema": PHASE5_REPORT_SCHEMA,
            "phase": 5,
            "status": "pass",
            "design": schedule["design"],
            "platform": platform.name,
            "provider": schedule["provider"],
            "validation": validation,
            "cross_layer_timing_validation": cross_layer_validation,
            **(
                {"timing_validation": timing_validation}
                if timing_validation is not None
                else {}
            ),
            "artifacts": {
                "schedule": "schedule.json",
                "cross_layer_timing": "cross_layer_timing.json",
                "report": "phase5_report.json",
            },
        }
        report["cut_mode"] = "static-exact-combinational"
        report["qualification"] = "dependency-schedule-readiness-pass"

        report["artifacts"]["schedule_store"] = schedule_storage["artifact"]
        report["schedule_storage"] = {
            "schema": schedule_storage["schema"],
            "implementation_id": (
                "native-exact-schedule-store-v1"
            ),
            "producer_implementation_id": (
                "native-exact-phase5-producer-v1"
            ),
            "dataflow": "typed-sqlite-route-entry-completion-relations-v1",
            "wall_seconds": schedule_storage_wall_seconds,
            "wall_scope": (
                "native-model-schedule-certificate-publication-and-store-seal"
            ),
            "bytes": schedule_storage["store_bytes"],
            "logical_sha256": schedule_storage["logical_sha256"],
            "records": {
                section: record["records"]
                for section, record in schedule_storage["sections"].items()
            },
        }
        write_json(
            staging / "cross_layer_timing.json",
            cross_layer_timing,
            compact=True,
        )

        generation_member_paths = [schedule_storage["artifact"]]
        generation_member_paths.extend(
            (
                "schedule.json",
                "cross_layer_timing.json",
            )
        )
        generation_members = []
        for relative in generation_member_paths:
            path = staging / relative
            generation_members.append(
                {
                    "path": relative,
                    "bytes": path.stat().st_size,
                    "sha256": (
                        schedule_storage["store_sha256"]
                        if relative == schedule_storage["artifact"]
                        else _phase5_file_sha256(path)
                    ),
                }
            )
        report["generation"] = {
            "schema": PHASE5_GENERATION_SCHEMA,
            "commit_point": "phase5_report.json",
            "members": generation_members,
        }
        report["generation"]["report_identity_sha256"] = (
            _canonical_phase5_report_identity(report)
        )
        write_json(staging / "phase5_report.json", report)
        validate_phase5_generation(
            staging / "phase5_report.json",
            managed_root_sealed=True,
        )
        _commit_phase5_generation(
            staging,
            output_dir,
            [*generation_member_paths, "phase5_report.json"],
        )
        return report

def validate_phase5(
    routes_path: Path, platform_path: Path, schedule_path: Path,
    assignment_path: Optional[Path] = None, *, managed_root_sealed: bool = False,
    allow_legacy_generation: bool = False,
) -> Dict[str, Any]:
    generation = validate_phase5_generation(
        schedule_path.parent / "phase5_report.json",
        managed_root_sealed=managed_root_sealed, allow_legacy=allow_legacy_generation,
    )
    report = generation.get("report")
    if isinstance(report, dict):
        if report.get("provider") != TDM_EXACT_DEPENDENCY_PROVIDER:
            raise ValidationError("unsupported Phase 5 generation provider")
        if report["artifacts"].get("ratio_plan") is not None:
            raise ValidationError("retired ratio-plan generation is not supported")
        if Path(os.path.abspath(schedule_path)) != Path(os.path.abspath(schedule_path.parent / "schedule.json")):
            raise ValidationError("Phase 5 schedule path disagrees with generation report")
    persisted_routes = read_system_routes(routes_path)
    routes, semantic_contract = _load_phase3_semantics(persisted_routes, assignment_path)
    platform = Platform.load(platform_path)
    schedule = read_tdm_schedule(schedule_path)
    if schedule.get("provider") != TDM_EXACT_DEPENDENCY_PROVIDER or semantic_contract is None:
        raise ValidationError("Phase 5 requires a Static Exact dependency schedule")
    validation = validate_tdm_schedule(routes, platform, schedule)
    if isinstance(routes.get("timing"), dict):
        validation["timing"] = reconstruct_tdm_schedule_timing(routes, platform, schedule)
    cross_layer_path = schedule_path.parent / "cross_layer_timing.json"
    if cross_layer_path.is_file():
        validation["cross_layer_timing"] = validate_cross_layer_timing_contract(
            persisted_routes, read_json(cross_layer_path), schedule, semantic_contract,
        )
    return validation
