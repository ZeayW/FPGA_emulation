from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .phase5_timing_native import project_native_schedule_timing
from .errors import ValidationError
from .io import read_json

from .phase3_storage import read_phase3_assignment_view
from .platform import Platform
from .routing import semantic_contract_for_routes
from .tdm import (
    TDM_EXACT_DEPENDENCY_PROVIDER,
    build_native_exact_schedule_request,
    validate_tdm_schedule,
)

from .tdm_schedule_storage import (
    read_tdm_schedule,
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
) -> Dict[str, Any]:
    """Validate the report-last Phase 5 generation before opening its data."""

    phase5_report_path = Path(phase5_report_path)
    if not phase5_report_path.is_file() or phase5_report_path.is_symlink():
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
    from .native_tools import require_native_schema

    require_native_schema("phase5-stage-request-v1")
    persisted_routes = read_system_routes(routes_path)
    routes, semantic_contract = _load_phase3_semantics(persisted_routes, assignment_path)
    if semantic_contract is None:
        raise ValidationError("Phase 5 requires the Static Exact dependency contract")
    platform = Platform.load(platform_path)
    bind_sources = getattr(persisted_routes, "bound_store_sources", None)
    if not callable(bind_sources):
        raise ValidationError("Phase 5 requires the current typed RouteStore owner")
    schedule = build_native_exact_schedule_request(
        routes, platform, source_bindings=bind_sources()
    )
    if schedule is None:
        raise ValidationError("Phase 5 requires the current typed RouteStore and indexed Static Exact contract")
    from .phase5_native import run_native_phase5

    return run_native_phase5(schedule, output_dir)


def validate_phase5(
    routes_path: Path, platform_path: Path, schedule_path: Path,
    assignment_path: Optional[Path] = None, *, managed_root_sealed: bool = False,
) -> Dict[str, Any]:
    generation = validate_phase5_generation(
        schedule_path.parent / "phase5_report.json",
        managed_root_sealed=managed_root_sealed,
    )
    report = generation["report"]
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
    contract = read_json(schedule_path.parent / "cross_layer_timing.json")
    if contract.get("schema") != "emuflow.cross-layer-timing/v3":
        raise ValidationError(
            "current Phase 5 generation requires indexed cross-layer timing v3"
        )
    projected = project_native_schedule_timing(
        persisted_routes,
        schedule,
        platform,
        bind=True,
        analyze=isinstance(routes.get("timing"), dict),
    )
    if contract != projected["contract"]:
        raise ValidationError(
            "cross-layer timing contract does not match canonical inputs"
        )
    if projected["timing_validation"] is not None:
        validation["timing"] = projected["timing_validation"]
    validation["cross_layer_timing"] = {"status": "pass", **contract["metrics"]}
    return validation
