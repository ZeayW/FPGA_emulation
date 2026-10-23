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
from .managed_json_storage import pack_managed_json
from .phase3_storage import read_phase3_assignment_view
from .platform import Platform
from .routing import semantic_contract_for_routes
from .tdm import (
    TDM_BASELINE_PROVIDER,
    TDM_EXACT_DEPENDENCY_PROVIDER,
    build_tdm_schedule,
    build_transport_manifest,
    reconstruct_tdm_schedule_timing,
    schedule_to_systemverilog_testbench,
    write_schedule_tsv,
    simulate_tdm_schedule,
    validate_tdm_schedule,
)
from .tdm_ratio import (
    DEFAULT_EXACT_DOMAIN_LIMIT,
    TDM_RATIO_PROVIDER,
    TDM_TIMING_DAG_RATIO_PROVIDER,
    _prepare_model,
    build_tdm_ratio_plan,
    validate_tdm_ratio_plan,
)
from .tdm_slot import refine_tdm_schedule_native
from .tdm_schedule_storage import (
    FULLY_TYPED_TDM_SCHEDULE_STORAGE_SCHEMA,
    TYPED_TDM_SCHEDULE_STORAGE_SCHEMA,
    read_tdm_schedule,
    tdm_schedule_store_path,
    write_tdm_schedule_storage,
)
from .tdm_timing_dag import build_timing_dag_ratio_plan
from .tdm_feedback import build_tdm_feedback, validate_tdm_feedback
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
    routes_path: Path,
    platform_path: Path,
    output_dir: Path,
    assignment_path: Optional[Path] = None,
    simulation_frames: int = 16,
    provider: Optional[str] = None,
    ratio_optimizer: Optional[str] = None,
    timing_dag_optimizer: Optional[str] = None,
    slot_optimizer: Optional[str] = None,
    ratio_max_iterations: int = 500,
    max_ratio: Optional[int] = None,
    ratio_quantum: Optional[int] = None,
    post_refinement_iterations: int = 200,
    slot_refinement_iterations: int = 0,
    convergence: float = 1.0e-9,
    managed_storage: bool = False,
) -> Dict[str, Any]:
    if (
        isinstance(slot_refinement_iterations, bool)
        or not isinstance(slot_refinement_iterations, int)
        or slot_refinement_iterations < 0
    ):
        raise ValueError(
            "slot_refinement_iterations must be a non-negative integer"
        )
    persisted_routes = read_system_routes(routes_path)
    routes, semantic_contract = _load_phase3_semantics(
        persisted_routes, assignment_path
    )
    platform = Platform.load(platform_path)
    exact_mode = semantic_contract is not None
    if provider is None:
        provider = (
            TDM_EXACT_DEPENDENCY_PROVIDER
            if exact_mode
            else (
                TDM_TIMING_DAG_RATIO_PROVIDER
                if isinstance(routes.get("timing"), dict)
                else TDM_BASELINE_PROVIDER
            )
        )
    if exact_mode and provider == TDM_BASELINE_PROVIDER:
        raise ValueError(
            f"{TDM_BASELINE_PROVIDER} is the explicit registered-boundary "
            "legacy provider and is incompatible with Static Exact; use "
            f"{TDM_EXACT_DEPENDENCY_PROVIDER}"
        )
    ratio_plan = None
    ratio_validation = None
    schedule = None
    validation = None
    timing_validation = None
    candidate_selection = None
    prepared_ratio_model = None
    if provider in {
        TDM_BASELINE_PROVIDER,
        TDM_EXACT_DEPENDENCY_PROVIDER,
    }:
        if provider == TDM_EXACT_DEPENDENCY_PROVIDER and not exact_mode:
            raise ValueError(
                "dependency TDM scheduling requires a Static Exact contract"
            )
        if (
            ratio_optimizer is not None
            or timing_dag_optimizer is not None
            or slot_optimizer is not None
        ):
            raise ValueError(
                "native TDM optimizers require the academic Phase 5 provider"
            )
    elif provider in {TDM_RATIO_PROVIDER, TDM_TIMING_DAG_RATIO_PROVIDER}:
        prepared_ratio_model = _prepare_model(routes, platform)
        if (
            provider == TDM_RATIO_PROVIDER
            and timing_dag_optimizer is not None
        ):
            raise ValueError(
                "timing_dag_optimizer requires the timing-DAG Phase 5 "
                "provider"
            )
        candidates = []
        rejected_candidates = []
        for strategy, exact_domain_limit in (
            ("exact-displacement-dp", DEFAULT_EXACT_DOMAIN_LIMIT),
            ("scalable-minimum-wire", 0),
        ):
            if provider == TDM_RATIO_PROVIDER:
                candidate_plan = build_tdm_ratio_plan(
                    routes,
                    platform,
                    executable=ratio_optimizer,
                    max_iterations=ratio_max_iterations,
                    max_ratio=max_ratio,
                    ratio_quantum=ratio_quantum,
                    post_refinement_iterations=post_refinement_iterations,
                    exact_domain_limit=exact_domain_limit,
                    convergence=convergence,
                    prepared_model=prepared_ratio_model,
                )
            else:
                candidate_plan = build_timing_dag_ratio_plan(
                    routes,
                    platform,
                    dag_executable=timing_dag_optimizer,
                    legalization_executable=ratio_optimizer,
                    max_iterations=ratio_max_iterations,
                    max_ratio=max_ratio,
                    ratio_quantum=ratio_quantum,
                    post_refinement_iterations=(
                        post_refinement_iterations
                    ),
                    exact_domain_limit=exact_domain_limit,
                    convergence=convergence,
                    prepared_model=prepared_ratio_model,
                )
            candidate_ratio_validation = validate_tdm_ratio_plan(
                routes,
                platform,
                candidate_plan,
                prepared_model=prepared_ratio_model,
            )
            try:
                candidate_schedule = build_tdm_schedule(
                    routes,
                    platform,
                    candidate_plan,
                    prepared_ratio_model=prepared_ratio_model,
                )
            except TDMScheduleInfeasibleError as error:
                rejected_candidates.append(
                    {
                        "strategy": strategy,
                        "status": "infeasible",
                        "reason": str(error),
                    }
                )
                continue
            if slot_refinement_iterations > 0:
                candidate_schedule = refine_tdm_schedule_native(
                    routes,
                    platform,
                    candidate_plan,
                    candidate_schedule,
                    executable=slot_optimizer,
                    max_iterations=slot_refinement_iterations,
                    prepared_ratio_model=prepared_ratio_model,
                )
            candidate_validation = validate_tdm_schedule(
                routes,
                platform,
                candidate_schedule,
                candidate_plan,
                prepared_ratio_model=prepared_ratio_model,
            )
            candidate_timing = reconstruct_tdm_schedule_timing(
                routes,
                platform,
                candidate_schedule,
                model=prepared_ratio_model,
            )
            score = (
                candidate_timing["worst_normalized_slack"],
                candidate_timing[
                    "total_negative_normalized_slack"
                ],
                -candidate_timing["negative_slack_paths"],
                candidate_timing["p01_normalized_slack"],
                candidate_timing["median_normalized_slack"],
                -candidate_schedule["metrics"]["completion_slot"],
                candidate_plan["metrics"][
                    "discrete_worst_normalized_slack"
                ],
                1 if strategy == "exact-displacement-dp" else 0,
            )
            candidates.append(
                {
                    "strategy": strategy,
                    "score": score,
                    "ratio_plan": candidate_plan,
                    "ratio_validation": candidate_ratio_validation,
                    "schedule": candidate_schedule,
                    "validation": candidate_validation,
                    "timing_validation": candidate_timing,
                }
            )
        if not candidates:
            reasons = "; ".join(
                f"{candidate['strategy']}: {candidate['reason']}"
                for candidate in rejected_candidates
            )
            raise TDMScheduleInfeasibleError(
                "all academic Phase 5 candidates are infeasible: "
                f"{reasons}"
            )
        selected = max(candidates, key=lambda candidate: candidate["score"])
        ratio_plan = selected["ratio_plan"]
        ratio_validation = selected["ratio_validation"]
        schedule = selected["schedule"]
        validation = selected["validation"]
        timing_validation = selected["timing_validation"]
        candidate_selection = {
            "objective": (
                "lexicographic realized worst and total negative normalized "
                "slack; negative path count; p01 and median normalized "
                "slack; completion slot; analytical discrete slack"
            ),
            "selected": selected["strategy"],
            **(
                {"rejected_candidates": rejected_candidates}
                if rejected_candidates
                else {}
            ),
            "candidates": [
                {
                    "strategy": candidate["strategy"],
                    "realized_worst_normalized_slack": candidate[
                        "timing_validation"
                    ]["worst_normalized_slack"],
                    "realized_total_negative_normalized_slack": candidate[
                        "timing_validation"
                    ]["total_negative_normalized_slack"],
                    "realized_negative_slack_paths": candidate[
                        "timing_validation"
                    ]["negative_slack_paths"],
                    "realized_p01_normalized_slack": candidate[
                        "timing_validation"
                    ]["p01_normalized_slack"],
                    "realized_median_normalized_slack": candidate[
                        "timing_validation"
                    ]["median_normalized_slack"],
                    "completion_slot": candidate["schedule"]["metrics"][
                        "completion_slot"
                    ],
                    "analytical_worst_normalized_slack": candidate[
                        "ratio_plan"
                    ]["metrics"]["discrete_worst_normalized_slack"],
                    "dp_legalized_domains": candidate["ratio_plan"][
                        "metrics"
                    ]["dp_legalized_domains"],
                    "greedy_legalized_domains": candidate["ratio_plan"][
                        "metrics"
                    ]["greedy_legalized_domains"],
                    **(
                        {
                            "slot_accepted_moves": candidate["schedule"][
                                "slot_optimization"
                            ]["metrics"]["accepted_moves"],
                            "slot_evaluated_moves": candidate["schedule"][
                                "slot_optimization"
                            ]["metrics"]["evaluated_moves"],
                        }
                        if "slot_optimization" in candidate["schedule"]
                        else {}
                    ),
                }
                for candidate in candidates
            ],
        }
    else:
        raise ValueError(f"unsupported Phase 5 provider {provider!r}")
    if schedule is None:
        schedule = build_tdm_schedule(routes, platform, ratio_plan)
        validation = (
            None if getattr(schedule["entries"], "native_store_publication_source", None)
            is not None else validate_tdm_schedule(routes, platform, schedule, ratio_plan)
        )
        timing_validation = (
            reconstruct_tdm_schedule_timing(
                routes,
                platform,
                schedule,
                model=prepared_ratio_model,
            )
            if isinstance(routes.get("timing"), dict)
            else None
        )
    return _finish_phase5_generation(
        persisted_routes=persisted_routes,
        routes=routes,
        semantic_contract=semantic_contract,
        schedule=schedule,
        validation=validation,
        timing_validation=timing_validation,
        candidate_selection=candidate_selection,
        ratio_plan=ratio_plan,
        ratio_validation=ratio_validation,
        exact_mode=exact_mode,
        platform=platform,
        output_dir=output_dir,
        managed_storage=managed_storage,
        simulation_frames=simulation_frames,
        prepared_ratio_model=prepared_ratio_model,
    )


def _finish_phase5_generation(
    *,
    persisted_routes: Mapping[str, Any],
    routes: Mapping[str, Any],
    semantic_contract: Optional[Mapping[str, Any]],
    schedule: Mapping[str, Any],
    validation: Optional[Mapping[str, Any]],
    timing_validation: Optional[Mapping[str, Any]],
    candidate_selection: Optional[Mapping[str, Any]],
    ratio_plan: Optional[Mapping[str, Any]],
    ratio_validation: Optional[Mapping[str, Any]],
    exact_mode: bool,
    platform: Platform,
    output_dir: Path,
    managed_storage: bool,
    simulation_frames: int,
    prepared_ratio_model: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Build every Phase 5 artifact in staging and commit the report last."""

    native_production = getattr(schedule["entries"], "native_store_publication_source", None) is not None
    emit_compatibility_exports = not managed_storage and not native_production
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
        persisted_schedule = read_tdm_schedule(staged_schedule_path)
        if validation is None:
            if not exact_mode or ratio_plan is not None or getattr(
                schedule["entries"], "native_store_publication_source", None
            ) is None:
                raise ValidationError("Phase 5 validation is missing")
            # Independently reconstruct from typed cursor/join relations before
            # committing the report; never inflate a private native result into
            # the generic validator's full hop dictionaries.
            validation = validate_tdm_schedule(routes, platform, persisted_schedule)
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
        simulation = None
        manifest = None
        feedback = None
        feedback_validation = None
        if emit_compatibility_exports:
            simulation = simulate_tdm_schedule(
                routes,
                schedule,
                frames=simulation_frames,
            )
            manifest = build_transport_manifest(
                routes, schedule, platform
            )
            feedback = build_tdm_feedback(
                routes,
                platform,
                schedule,
                ratio_plan,
                prepared_ratio_model=prepared_ratio_model,
            )
            feedback_validation = validate_tdm_feedback(
                routes,
                platform,
                schedule,
                feedback,
                ratio_plan,
                prepared_ratio_model=prepared_ratio_model,
            )

        report: Dict[str, Any] = {
            "schema": PHASE5_REPORT_SCHEMA,
            "phase": 5,
            "status": "pass",
            "design": schedule["design"],
            "platform": platform.name,
            "provider": schedule["provider"],
            **(
                {
                    "optimization_provider": ratio_plan["provider"],
                    "ratio_validation": ratio_validation,
                }
                if ratio_plan is not None
                else {}
            ),
            "validation": validation,
            "cross_layer_timing_validation": cross_layer_validation,
            **(
                {"timing_validation": timing_validation}
                if timing_validation is not None
                else {}
            ),
            **(
                {"candidate_selection": candidate_selection}
                if candidate_selection is not None
                else {}
            ),
            "artifacts": {
                "schedule": "schedule.json",
                "cross_layer_timing": "cross_layer_timing.json",
                "report": "phase5_report.json",
            },
        }
        if emit_compatibility_exports:
            report.update(
                {
                    "simulation": simulation,
                    "tdm_feedback_validation": feedback_validation,
                }
            )
            report["artifacts"].update(
                {
                    "schedule_tsv": "schedule.tsv",
                    "transport_manifest": "transport_manifest.json",
                    "rtl_testbench": "transport_schedule_tb.sv",
                    "tdm_feedback": "tdm_feedback.json",
                }
            )
        if exact_mode:
            report["cut_mode"] = "static-exact-combinational"
            report["qualification"] = "dependency-schedule-readiness-pass"
        if ratio_plan is not None:
            report["artifacts"]["ratio_plan"] = "ratio_plan.json"
            write_json(
                staging / "ratio_plan.json",
                pack_managed_json(ratio_plan)
                if managed_storage
                else dict(ratio_plan),
                compact=True,
            )

        report["artifacts"]["schedule_store"] = schedule_storage["artifact"]
        report["schedule_storage"] = {
            "schema": schedule_storage["schema"],
            "implementation_id": (
                "native-exact-schedule-store-v1"
                if getattr(schedule["entries"], "native_store_publication_source", None)
                is not None
                else "python-reference-schedule-store-v1"
            ),
            "dataflow": (
                "typed-sqlite-route-entry-completion-relations-v1"
                if schedule_storage["schema"]
                == FULLY_TYPED_TDM_SCHEDULE_STORAGE_SCHEMA
                else (
                    "typed-sqlite-entry-relation-v1"
                    if schedule_storage["schema"]
                    == TYPED_TDM_SCHEDULE_STORAGE_SCHEMA
                    else "indexed-sqlite-ordered-records-v1"
                )
            ),
            "wall_seconds": schedule_storage_wall_seconds,
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
        if emit_compatibility_exports:
            assert manifest is not None and feedback is not None
            write_schedule_tsv(staging / "schedule.tsv", schedule)
            write_json(
                staging / "transport_manifest.json",
                manifest,
                compact=True,
            )
            write_json(
                staging / "tdm_feedback.json",
                feedback,
                compact=True,
            )
            (staging / "transport_schedule_tb.sv").write_text(
                schedule_to_systemverilog_testbench(
                    routes,
                    schedule,
                    platform,
                    frames=simulation_frames,
                ),
                encoding="utf-8",
            )

        generation_member_paths = [schedule_storage["artifact"]]
        if ratio_plan is not None:
            generation_member_paths.append("ratio_plan.json")
        if emit_compatibility_exports:
            generation_member_paths.extend(
                (
                    "schedule.tsv",
                    "transport_manifest.json",
                    "tdm_feedback.json",
                    "transport_schedule_tb.sv",
                )
            )
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
    routes_path: Path,
    platform_path: Path,
    schedule_path: Path,
    ratio_plan_path: Optional[Path] = None,
    assignment_path: Optional[Path] = None,
    *,
    managed_root_sealed: bool = False,
    allow_legacy_generation: bool = False,
) -> Dict[str, Any]:
    generation_validation = validate_phase5_generation(
        schedule_path.parent / "phase5_report.json",
        managed_root_sealed=managed_root_sealed,
        allow_legacy=allow_legacy_generation,
    )
    generation_report = generation_validation.get("report")
    if isinstance(generation_report, dict):
        expected_schedule = schedule_path.parent / "schedule.json"
        if Path(os.path.abspath(schedule_path)) != Path(
            os.path.abspath(expected_schedule)
        ):
            raise ValidationError(
                "Phase 5 schedule path disagrees with generation report"
            )
        artifacts = generation_report["artifacts"]
        sealed_ratio_name = artifacts.get("ratio_plan")
        if sealed_ratio_name is not None:
            sealed_ratio_path = schedule_path.parent / sealed_ratio_name
            if ratio_plan_path is None:
                ratio_plan_path = sealed_ratio_path
            elif Path(os.path.abspath(ratio_plan_path)) != Path(
                os.path.abspath(sealed_ratio_path)
            ):
                raise ValidationError(
                    "Phase 5 ratio-plan path disagrees with generation report"
                )
        elif ratio_plan_path is not None:
            raise ValidationError(
                "Phase 5 generation does not contain a ratio plan"
            )
    persisted_routes = read_system_routes(routes_path)
    routes, semantic_contract = _load_phase3_semantics(
        persisted_routes, assignment_path
    )
    platform = Platform.load(platform_path)
    schedule = read_tdm_schedule(schedule_path)
    ratio_plan = (
        read_json(ratio_plan_path)
        if ratio_plan_path is not None
        else None
    )
    # Academic schedules are constructed and certified against the canonical
    # dense ratio model.  Rebuild that model once for standalone validation as
    # well; otherwise the schedule validator compares the native certificate
    # with the sparse baseline reconstruction, which is intentionally used
    # only for ratio-free baseline scale runs and may use a different path
    # representation.  Sharing one model also avoids rebuilding a large
    # TimingPathDB for the subsequent timing report.
    prepared_ratio_model = (
        _prepare_model(routes, platform) if ratio_plan is not None else None
    )
    validation = validate_tdm_schedule(
        routes,
        platform,
        schedule,
        ratio_plan,
        prepared_ratio_model=prepared_ratio_model,
    )
    if isinstance(routes.get("timing"), dict):
        validation["timing"] = reconstruct_tdm_schedule_timing(
            routes, platform, schedule, model=prepared_ratio_model
        )
    cross_layer_path = schedule_path.parent / "cross_layer_timing.json"
    if cross_layer_path.is_file():
        validation["cross_layer_timing"] = (
            validate_cross_layer_timing_contract(
                persisted_routes,
                read_json(cross_layer_path),
                schedule,
                semantic_contract,
            )
        )
    return validation
