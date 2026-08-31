"""Reusable stage runners for content-addressed Phase 6/7 experiments."""

from __future__ import annotations

import copy
import errno
import hashlib
import math
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from .academic_chimew import materialize_academic_chimew_inputs
from .chimew_pipeline import (
    run_chimew_phase6_pipeline,
    validate_chimew_phase6_pipeline,
)
from .errors import EmuFlowError, ValidationError
from .experiment_storage import validate_experiment_write_path
from .io import read_json, write_json
from .ir import EmuIR
from .multi_fpga_physical_flow import (
    run_multi_fpga_physical_flow,
    validate_multi_fpga_physical_report,
)
from .phase3 import validate_phase3
from .phase4 import validate_phase4
from .phase5 import validate_phase5
from .phase6 import run_phase6, validate_phase6
from .phase7c import run_phase7c
from .pin_planning import (
    SIGNAL_POSITION_HINTS_SCHEMA,
    build_pin_plan,
    validate_pin_plan,
)
from .platform import Platform
from .runtime import QOR_REPORT_SCHEMA
from .vpr import VTR_HARD_BLOCK_PROFILE


EXPERIMENT_LOOKAHEAD_SCHEMA = "emuflow.experiment-physical-lookahead/v1"
EXPERIMENT_PHASE6_SCHEMA = "emuflow.experiment-phase6-checkpoint/v1"
LEGACY_EXPERIMENT_PHASE7_SCHEMA = "emuflow.experiment-phase7-checkpoint/v1"
EXPERIMENT_PHASE7_SCHEMA = "emuflow.experiment-phase7-checkpoint/v2"
PHASE7_QOR_PROJECTION_SCHEMA = "emuflow.phase7-qor-projection/v1"
MANAGED_DAG_VALIDATION_MODE = "managed-independent-publish-v1"
_PROVIDERS = {"baseline", "placement-aware", "chimew"}
_PHASE6_VALIDATION_MODES = {
    "full-replay",
    "producer-self-check",
    "validated-checkpoint-reuse",
}


class _ValidationSession:
    """Deduplicate dependency validation within one stage process.

    Public validator calls create a fresh session, so a later standalone
    validation still observes filesystem changes.  Nested validators in one
    run share the session and therefore do not repeatedly parse the same large
    immutable dependency reports.
    """

    def __init__(self) -> None:
        self.shared: Dict[tuple[str, str], Dict[str, Any]] = {}
        self.phase6: Dict[tuple[str, str, str | None, str], Dict[str, Any]] = {}
        self.lookahead: Dict[tuple[Any, ...], Dict[str, Any]] = {}
        self.physical: Dict[int, tuple[Mapping[str, Any], Dict[str, Any]]] = {}

    def validate_physical(
        self, report: Mapping[str, Any]
    ) -> Dict[str, Any]:
        key = id(report)
        cached = self.physical.get(key)
        if cached is not None and cached[0] is report:
            return copy.deepcopy(cached[1])
        result = validate_multi_fpga_physical_report(report)
        self.physical[key] = (report, copy.deepcopy(result))
        return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _link_tree(source: Path, destination: Path) -> None:
    """Clone an immutable checkpoint tree without duplicating file data."""

    def link_or_copy(source_file: str, destination_file: str) -> str:
        try:
            os.link(source_file, destination_file)
        except OSError as error:
            if error.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES}:
                raise
            shutil.copy2(source_file, destination_file)
        return destination_file

    shutil.copytree(source, destination, copy_function=link_or_copy)


def _managed_checkpoint(
    root: Path,
    *,
    expected_stage: str | Sequence[str],
) -> Dict[str, Any] | None:
    """Return a sealed managed checkpoint for a routine consumer, if present."""

    from .experiment_dag import (
        EXPERIMENT_VALIDATION_SCHEMA,
        validate_experiment_checkpoint,
    )

    root = root.resolve()
    checkpoint_path = root.parent / "checkpoint.json"
    if not checkpoint_path.is_file():
        return None
    checkpoint = validate_experiment_checkpoint(
        checkpoint_path,
        verify_artifact_content=False,
        verify_immutable_tree=False,
    )
    expected_stages = (
        {expected_stage}
        if isinstance(expected_stage, str)
        else set(expected_stage)
    )
    if (
        checkpoint.get("schema") != "emuflow.experiment-checkpoint/v2"
        or checkpoint.get("stage") not in expected_stages
        or checkpoint.get("storage") != "managed"
        or checkpoint.get("output_immutable") is not True
        or Path(checkpoint.get("output_dir", "")).resolve() != root
    ):
        raise ValidationError("managed checkpoint contract is invalid")
    execution_key = checkpoint["execution_key"]
    certificates = []
    validation_root = root.parent / "validations"
    if validation_root.is_dir():
        for path in sorted(validation_root.glob("*.json")):
            value = read_json(path)
            if value != {
                "schema": EXPERIMENT_VALIDATION_SCHEMA,
                "execution_key": execution_key,
                "validation_key": path.stem,
                "status": "pass",
            }:
                raise ValidationError(
                    "managed checkpoint validation certificate is invalid"
                )
            certificates.append(value)
    if not certificates:
        raise ValidationError(
            "managed checkpoint reuse requires an independent validation certificate"
        )
    return checkpoint


def _validate_managed_phase6_checkpoint(root: Path) -> Dict[str, Any]:
    checkpoint = _managed_checkpoint(root, expected_stage="phase6")
    if checkpoint is None:
        raise ValidationError(
            "Phase 6 equivalence reuse requires a managed checkpoint"
        )
    return checkpoint


def _finite_number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValidationError(f"experiment Phase 7 {label} must be finite")
    return float(value)


def _phase7_qor_projection(qor: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the small, replayable subset needed for experiment comparison.

    The complete QoR artifact remains authoritative and hash sealed.  This
    projection prevents every downstream comparison from parsing and embedding
    the complete (potentially hundreds-of-megabytes) timing evidence again.
    """

    if qor.get("schema") != QOR_REPORT_SCHEMA or qor.get("status") != "pass":
        raise ValidationError("experiment Phase 7 QoR report is invalid")
    design = qor.get("design")
    platform = qor.get("platform")
    if (
        not isinstance(design, str)
        or not design
        or not isinstance(platform, str)
        or not platform
    ):
        raise ValidationError("experiment Phase 7 QoR identity is invalid")
    timing = qor.get("timing")
    physical = qor.get("physical")
    if not isinstance(timing, dict) or timing.get("status") != "pass":
        raise ValidationError("experiment Phase 7 timing evidence is invalid")
    if not isinstance(physical, dict) or physical.get("status") != "pass":
        raise ValidationError("experiment Phase 7 physical evidence is invalid")

    clocks: Dict[str, Dict[str, Any]] = {}
    for name in ("target_clock", "runtime_clock"):
        clock = timing.get(name)
        if not isinstance(clock, dict):
            raise ValidationError(f"experiment Phase 7 {name} evidence is missing")
        failing = clock.get("negative_slack_paths")
        if isinstance(failing, bool) or not isinstance(failing, int) or failing < 0:
            raise ValidationError(
                f"experiment Phase 7 {name} negative-slack count is invalid"
            )
        tns = _finite_number(
            clock.get("total_negative_slack_bound_ns"), f"{name} TNS"
        )
        if tns > 1.0e-12:
            raise ValidationError(f"experiment Phase 7 {name} TNS is positive")
        clocks[name] = {
            "worst_slack_bound_ns": _finite_number(
                clock.get("worst_slack_bound_ns"), f"{name} WNS"
            ),
            "total_negative_slack_bound_ns": tns,
            "negative_slack_paths": failing,
        }

    unrouted = physical.get("unrouted_nets")
    drc = physical.get("drc_violations")
    if (
        isinstance(unrouted, bool)
        or not isinstance(unrouted, int)
        or unrouted < 0
        or isinstance(drc, bool)
        or not isinstance(drc, int)
        or drc < 0
    ):
        raise ValidationError("experiment Phase 7 physical violation count is invalid")
    return {
        "schema": PHASE7_QOR_PROJECTION_SCHEMA,
        "status": "pass",
        "design": design,
        "platform": platform,
        "timing": {
            "status": "pass",
            "qualification": copy.deepcopy(timing.get("qualification")),
            "path_exactness": copy.deepcopy(timing.get("path_exactness")),
            **clocks,
        },
        "physical": {
            "status": "pass",
            "worst_wns_ns": _finite_number(
                physical.get("worst_wns_ns"), "per-FPGA worst WNS"
            ),
            "total_tns_ns": _finite_number(
                physical.get("total_tns_ns"), "per-FPGA total TNS"
            ),
            "unrouted_nets": unrouted,
            "drc_violations": drc,
        },
    }


def _require_file(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.is_file():
        raise ValidationError(f"experiment stage artifact is missing: {relative}")
    return path


def _shared_paths(root: Path) -> Dict[str, Path]:
    return {
        "ir": _require_file(root, "frontend/phase1/design.emuir.json"),
        "clusters": _require_file(root, "partition/clusters.json"),
        "assignment": _require_file(root, "partition/assignment.json"),
        "phase3_report": _require_file(root, "partition/phase3_report.json"),
        "routes": _require_file(root, "system-route/routes.json"),
        "phase4_report": _require_file(root, "system-route/phase4_report.json"),
        "schedule": _require_file(root, "tdm/schedule.json"),
        "phase5_report": _require_file(root, "tdm/phase5_report.json"),
    }


def _timing_paths(root: Path) -> Path | None:
    path = root / "timing/cut-timing-paths.json"
    return path if path.is_file() else None


def _sta_path_database(root: Path) -> Path | None:
    path = root / "timing/path-database.json"
    return path if path.is_file() else None


def _physical_timing_databases(root: Path) -> tuple[Path | None, Path | None]:
    """Return the local and routed-member STA databases for physical timing.

    Local intra-FPGA queries always use the complete pre-partition database.
    Cross-FPGA logic segments use the database that produced the sealed
    Phase 4 timing population, because its compressed member IDs define the
    routed paths.  Canonical v3+ checkpoints project the complete database;
    legacy v2 checkpoints projected the through-cut qualification database.
    """

    full = _sta_path_database(root)
    cut_path = root / "timing/cut-path-database.json"
    cut = cut_path if cut_path.is_file() else None
    if full is None and cut is not None:
        raise ValidationError(
            "physical timing has a through-cut STA database without the "
            "complete original STA path database"
        )
    if full is None:
        return None, None
    projected_path = root / "timing/cut-timing-paths.json"
    if not projected_path.is_file():
        raise ValidationError(
            "physical timing requires the sealed Phase 4 timing population"
        )
    projected = read_json(projected_path)
    source = projected.get("source", {})
    if not isinstance(source, dict):
        raise ValidationError("physical timing population provenance is invalid")

    # Canonical v3 projections are portable content-addressed artifacts.  They
    # deliberately avoid recording the producer-local path because a managed
    # checkpoint can be moved into the object store after validation.  Retain
    # the v2 filename form below only for reading existing legacy checkpoints.
    source_digest = source.get("input_sha256")
    if source_digest is not None:
        if not isinstance(source_digest, str) or len(source_digest) != 64:
            raise ValidationError("physical timing population digest is invalid")
        candidates = [(full, full)]
        if cut is not None:
            candidates.append((cut, cut))
        matched = [
            candidate
            for candidate in candidates
            if _sha256(candidate[0]) == source_digest
        ]
        if not matched:
            raise ValidationError(
                "physical timing population digest does not name a known STA database"
            )
        # Identical complete and through-cut databases are semantically
        # interchangeable; prefer the complete database in that degenerate
        # case so local and routed member queries stay on one immutable input.
        selected = matched[0][1]
        source_input = source.get("input")
        if source_input is None:
            return full, selected
        if not isinstance(source_input, str) or not source_input:
            raise ValidationError("physical timing population provenance is invalid")
        source_name = Path(source_input).name
        if source_name != selected.name:
            raise ValidationError(
                "physical timing population path and digest disagree"
            )
        return full, selected

    source_input = source.get("input")
    if not isinstance(source_input, str) or not source_input:
        raise ValidationError("physical timing population provenance is invalid")
    source_name = Path(source_input).name
    if source_name == full.name:
        return full, full
    if cut is not None and source_name == cut.name:
        return full, cut
    raise ValidationError(
        "physical timing population names an unknown STA database"
    )


def _board_link_timing(root: Path) -> Path | None:
    path = root / "timing/board-link-timing.json"
    return path if path.is_file() else None


def _prepare_empty_output(output_dir: Path, label: str) -> Path:
    output_dir = validate_experiment_write_path(output_dir)
    if output_dir.exists() and (
        not output_dir.is_dir() or any(output_dir.iterdir())
    ):
        raise EmuFlowError(f"{label} output must be an empty directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _placement_aware_positions(
    ir_path: Path,
    schedule_path: Path,
    placement_source_path: Path,
    *,
    region_count: int,
) -> Dict[str, Any]:
    ir = read_json(ir_path)
    schedule = read_json(schedule_path)
    placement_source = read_json(placement_source_path)
    locations = {
        fpga["fpga"]: {
            instance["id"]: float(instance["normalised_y"])
            for instance in fpga.get("instances", [])
        }
        for fpga in placement_source.get("fpgas", [])
    }
    net_by_id = {net["id"]: net for net in ir.get("nets", [])}

    def centroid(fpga: str, instances: list[str]) -> tuple[float, bool]:
        values = [
            locations[fpga][instance]
            for instance in instances
            if instance in locations.get(fpga, {})
        ]
        return ((sum(values) / len(values), False) if values else (0.5, True))

    hints = []
    fallbacks = 0
    for entry in sorted(schedule.get("entries", []), key=lambda item: item["id"]):
        net = net_by_id.get(entry.get("net"))
        if net is None:
            raise ValidationError(
                f"placement-aware entry {entry.get('id')!r} references an unknown net"
            )
        drivers = [
            endpoint["instance"]
            for endpoint in net.get("drivers", [])
            if endpoint.get("instance") is not None
        ]
        sinks = [
            endpoint["instance"]
            for endpoint in net.get("sinks", [])
            if endpoint.get("instance") is not None
        ]
        source_y, source_fallback = centroid(entry["from"], drivers)
        sink_y, sink_fallback = centroid(entry["to"], sinks)
        fallbacks += int(source_fallback) + int(sink_fallback)
        hints.append(
            {
                "schedule_entry": entry["id"],
                "source_y": source_y,
                "sink_y": sink_y,
                "source_region": min(region_count - 1, int(source_y * region_count)),
                "sink_region": min(region_count - 1, int(sink_y * region_count)),
                "source_fallback": source_fallback,
                "sink_fallback": sink_fallback,
            }
        )
    return {
        "schema": SIGNAL_POSITION_HINTS_SCHEMA,
        "design": schedule["design"],
        "platform": schedule["platform"],
        "provider": "openparf-lookahead-centroid-v1",
        "region_count": region_count,
        "metrics": {
            "signals": len(hints),
            "endpoint_centroid_fallbacks": fallbacks,
        },
        "entries": hints,
    }


def validate_shared_phase1_5(
    root: Path,
    platform_path: Path,
    *,
    reuse_managed_checkpoint: bool = False,
    _validation_session: _ValidationSession | None = None,
) -> Dict[str, Any]:
    session = _validation_session or _ValidationSession()
    root = root.resolve()
    platform_path = platform_path.resolve()
    key = (str(root), str(platform_path))
    if key in session.shared:
        return copy.deepcopy(session.shared[key])
    if reuse_managed_checkpoint:
        checkpoint = _managed_checkpoint(root, expected_stage="shared")
        if checkpoint is not None:
            report = read_json(_require_file(root, "experiment-shared-report.json"))
            if (
                report.get("schema") != "emuflow.experiment-shared-phase1-5/v1"
                or report.get("status") != "pass"
                or report.get("platform_sha256") != _sha256(platform_path)
            ):
                raise ValidationError("managed shared checkpoint contract is invalid")
            artifacts = report.get("artifacts")
            if not isinstance(artifacts, dict):
                raise ValidationError(
                    "managed shared checkpoint artifact table is invalid"
                )
            required = {
                "ir": "frontend/phase1/design.emuir.json",
                "assignment": "partition/assignment.json",
                "routes": "system-route/routes.json",
                "schedule": "tdm/schedule.json",
            }
            hashes = {}
            for label, relative in required.items():
                record = artifacts.get(relative)
                if (
                    not isinstance(record, dict)
                    or not isinstance(record.get("sha256"), str)
                ):
                    raise ValidationError(
                        "managed shared checkpoint artifact seal is invalid"
                    )
                hashes[label] = record["sha256"]
            result = {
                "status": "pass",
                "platform": Platform.load(platform_path).name,
                "phase1_5_sha256": hashes,
            }
            session.shared[key] = copy.deepcopy(result)
            return result
    paths = _shared_paths(root)
    validate_phase3(paths["ir"], platform_path, paths["clusters"], paths["assignment"])
    validate_phase4(
        paths["assignment"],
        platform_path,
        paths["routes"],
        timing_paths_path=_timing_paths(root),
    )
    ratio_plan = root / "tdm/ratio_plan.json"
    validate_phase5(
        paths["routes"],
        platform_path,
        paths["schedule"],
        ratio_plan_path=ratio_plan if ratio_plan.is_file() else None,
    )
    ir = EmuIR.load(paths["ir"])
    platform = Platform.load(platform_path)
    result = {
        "status": "pass",
        "design": ir.value["design"]["name"],
        "platform": platform.name,
        "phase1_5_sha256": {
            label: _sha256(paths[label])
            for label in ("ir", "assignment", "routes", "schedule")
        },
    }
    session.shared[key] = copy.deepcopy(result)
    return result


def run_physical_lookahead(
    shared_root: Path,
    baseline_phase6_root: Path | None,
    platform_path: Path,
    output_dir: Path,
    *,
    seed: int,
    workers: int,
    region_count: int,
    architecture: Path | None = None,
    architecture_id: str = VTR_HARD_BLOCK_PROFILE,
    yosys: str | None = None,
    vpr: str | None = None,
    architecture_importer: str | None = None,
    packed_importer: str | None = None,
    route_checker: str | None = None,
    openparf_install: Path | None = None,
    openparf_python: Path | None = None,
    route_channel_width: int = 300,
    reuse_validated_phase6_equivalence: bool = False,
    managed_dag_node: bool = False,
) -> Dict[str, Any]:
    session = _ValidationSession()
    shared = validate_shared_phase1_5(
        shared_root,
        platform_path,
        reuse_managed_checkpoint=True,
        _validation_session=session,
    )
    paths = _shared_paths(shared_root)
    split_root = (
        baseline_phase6_root / "split"
        if baseline_phase6_root is not None
        else shared_root / "split"
    )
    if baseline_phase6_root is not None:
        baseline = validate_phase6_checkpoint(
            baseline_phase6_root,
            shared_root,
            None,
            platform_path,
            validation_mode=(
                "validated-checkpoint-reuse"
                if reuse_validated_phase6_equivalence
                else "full-replay"
            ),
            _validation_session=session,
        )
        if baseline["provider"] != "baseline":
            raise ValidationError("physical lookahead requires baseline Phase 6")
    path_database, logic_path_database = _physical_timing_databases(shared_root)
    output_dir = _prepare_empty_output(output_dir, "physical-lookahead")
    physical = run_multi_fpga_physical_flow(
        split_root,
        platform_path,
        paths["schedule"],
        output_dir / "physical",
        backend="open",
        architecture=architecture,
        architecture_id=architecture_id,
        yosys=yosys,
        vpr=vpr,
        architecture_importer=architecture_importer,
        packed_importer=packed_importer,
        route_checker=route_checker,
        openparf_install=openparf_install,
        openparf_python=openparf_python,
        seed=seed,
        route_channel_width=route_channel_width,
        workers=workers,
        original_ir_path=(paths["ir"] if path_database else None),
        assignment_path=(paths["assignment"] if path_database else None),
        routes_path=(paths["routes"] if path_database else None),
        path_database_path=path_database,
        logic_path_database_path=logic_path_database,
    )
    return _finish_physical_lookahead(
        shared_root,
        baseline_phase6_root,
        platform_path,
        output_dir,
        physical,
        seed=seed,
        workers=workers,
        region_count=region_count,
        architecture=architecture,
        architecture_id=architecture_id,
        route_channel_width=route_channel_width,
        reuse_validated_phase6_equivalence=reuse_validated_phase6_equivalence,
        managed_dag_node=managed_dag_node,
        _validation_session=session,
    )


def resume_physical_lookahead(
    shared_root: Path,
    baseline_phase6_root: Path | None,
    platform_path: Path,
    output_dir: Path,
    *,
    seed: int,
    workers: int,
    region_count: int,
    architecture: Path | None = None,
    architecture_id: str = VTR_HARD_BLOCK_PROFILE,
    route_channel_width: int = 300,
    reuse_validated_phase6_equivalence: bool = False,
    managed_dag_node: bool = False,
) -> Dict[str, Any]:
    """Finish a lookahead checkpoint around an independently resumed physical run."""

    output_dir = validate_experiment_write_path(output_dir)
    if not output_dir.is_dir() or {path.name for path in output_dir.iterdir()} != {
        "physical"
    }:
        raise ValidationError(
            "resumed physical-lookahead root must contain only physical/"
        )
    physical_root = output_dir / "physical"
    if not physical_root.is_dir():
        raise ValidationError("resumed physical-lookahead physical/ is missing")
    physical = read_json(
        _require_file(physical_root, "multi-fpga-physical-flow-report.json")
    )
    physical = _rebase_resumed_physical_paths(physical, physical_root)
    write_json(
        physical_root / "multi-fpga-physical-flow-report.json", physical
    )
    return _finish_physical_lookahead(
        shared_root,
        baseline_phase6_root,
        platform_path,
        output_dir,
        physical,
        seed=seed,
        workers=workers,
        region_count=region_count,
        architecture=architecture,
        architecture_id=architecture_id,
        route_channel_width=route_channel_width,
        reuse_validated_phase6_equivalence=reuse_validated_phase6_equivalence,
        managed_dag_node=managed_dag_node,
        _validation_session=_ValidationSession(),
    )


def _rebase_resumed_physical_paths(
    physical: Dict[str, Any], physical_root: Path
) -> Dict[str, Any]:
    """Relocate report-internal paths after an immutable attempt is moved.

    A failed attempt is sealed below ``checkpoints/failures`` before it can be
    resumed.  The physical report consequently still names its original
    staging directory.  Infer that old ``physical/`` root only from the
    per-FPGA placement-IR outputs, require one unambiguous root, and rewrite
    only descendants of it into the caller-provided physical tree.
    """

    roots = set()
    marker = "/physical/"
    for record in physical.get("fpgas", []):
        if not isinstance(record, dict):
            continue
        output = record.get("stages", {}).get("placement_ir", {}).get("output")
        if not isinstance(output, str) or not Path(output).is_absolute():
            continue
        prefix, separator, _ = output.rpartition(marker)
        if separator:
            roots.add(prefix + "/physical")
    if not roots:
        return copy.deepcopy(physical)
    if len(roots) != 1:
        raise ValidationError(
            "resumed physical report contains ambiguous staging roots"
        )
    old_root = next(iter(roots))
    new_root = physical_root.resolve()
    if Path(old_root).resolve() == new_root:
        return copy.deepcopy(physical)

    def relocate(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: relocate(item) for key, item in value.items()}
        if isinstance(value, list):
            return [relocate(item) for item in value]
        if not isinstance(value, str):
            return value
        if value == old_root:
            return str(new_root)
        prefix = old_root + "/"
        if not value.startswith(prefix):
            return value
        relative = Path(value[len(prefix) :])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValidationError(
                "resumed physical report contains an unsafe internal path"
            )
        candidate = (new_root / relative).resolve()
        try:
            candidate.relative_to(new_root)
        except ValueError as error:
            raise ValidationError(
                "resumed physical report path escapes the physical tree"
            ) from error
        return str(candidate)

    return relocate(copy.deepcopy(physical))


def _finish_physical_lookahead(
    shared_root: Path,
    baseline_phase6_root: Path | None,
    platform_path: Path,
    output_dir: Path,
    physical: Dict[str, Any],
    *,
    seed: int,
    workers: int,
    region_count: int,
    architecture: Path | None,
    architecture_id: str,
    route_channel_width: int,
    reuse_validated_phase6_equivalence: bool = False,
    managed_dag_node: bool = False,
    _validation_session: _ValidationSession | None = None,
) -> Dict[str, Any]:
    session = _validation_session or _ValidationSession()
    shared = validate_shared_phase1_5(
        shared_root,
        platform_path,
        reuse_managed_checkpoint=True,
        _validation_session=session,
    )
    paths = _shared_paths(shared_root)
    split_root = (
        baseline_phase6_root / "split"
        if baseline_phase6_root is not None
        else shared_root / "split"
    )
    if baseline_phase6_root is not None:
        baseline = validate_phase6_checkpoint(
            baseline_phase6_root,
            shared_root,
            None,
            platform_path,
            validation_mode=(
                "validated-checkpoint-reuse"
                if reuse_validated_phase6_equivalence
                else "full-replay"
            ),
            _validation_session=session,
        )
        if baseline["provider"] != "baseline":
            raise ValidationError("physical lookahead requires baseline Phase 6")
    if not managed_dag_node:
        session.validate_physical(physical)
    if physical.get("execution", {}).get("requested_workers") != workers:
        raise ValidationError("resumed physical-lookahead worker count disagrees")
    physical_architecture = physical.get("architecture", {})
    expected_architecture_sha256 = (
        _sha256(architecture.expanduser().resolve())
        if architecture is not None and not managed_dag_node
        else None
    )
    if expected_architecture_sha256 is not None and physical_architecture.get(
        "sha256"
    ) != expected_architecture_sha256:
        raise ValidationError("resumed physical-lookahead architecture disagrees")
    if not managed_dag_node and physical.get("split_manifest", {}).get(
        "sha256"
    ) != _sha256(split_root / "manifest.json"):
        raise ValidationError("resumed physical-lookahead Phase 6 seal disagrees")
    for fpga in physical.get("fpgas", []):
        stages = fpga.get("stages", {})
        if stages.get("vpr_pack_place", {}).get("configuration", {}).get(
            "seed"
        ) != seed:
            raise ValidationError("resumed physical-lookahead VPR seed disagrees")
        if stages.get("vpr_route", {}).get("configuration", {}).get(
            "route_channel_width"
        ) != route_channel_width:
            raise ValidationError(
                "resumed physical-lookahead VPR channel width disagrees"
            )
    lookahead = materialize_academic_chimew_inputs(
        ir_path=paths["ir"],
        schedule_path=paths["schedule"],
        routes_path=paths["routes"],
        platform_path=platform_path,
        physical_report=physical,
        output_dir=output_dir / "lookahead",
        timing_paths_path=(
            shared_root / "timing/cut-timing-paths.json"
            if (shared_root / "timing/cut-timing-paths.json").is_file()
            else None
        ),
        region_count=region_count,
    )
    report = {
        "schema": EXPERIMENT_LOOKAHEAD_SCHEMA,
        "status": "pass",
        "seed": seed,
        "workers": workers,
        "region_count": region_count,
        "architecture_sha256": expected_architecture_sha256,
        "architecture_id": architecture_id,
        "route_channel_width": route_channel_width,
        "shared": shared,
        "metrics": lookahead["metrics"],
    }
    if managed_dag_node:
        report["validation_mode"] = MANAGED_DAG_VALIDATION_MODE
    else:
        report.update(
            {
                "baseline_phase6_manifest_sha256": _sha256(
                    split_root / "manifest.json"
                ),
                "physical_summary_sha256": _sha256(
                    output_dir / "physical/physical-summary.json"
                ),
                "lookahead_report_sha256": _sha256(
                    output_dir / "lookahead/academic-chimew-lookahead-report.json"
                ),
            }
        )
    write_json(output_dir / "experiment-lookahead-report.json", report)
    if not managed_dag_node:
        validate_physical_lookahead(
            output_dir,
            shared_root,
            baseline_phase6_root,
            platform_path,
            reuse_validated_phase6_equivalence=reuse_validated_phase6_equivalence,
            _validation_session=session,
            _physical_report=physical,
        )
    return report


def validate_physical_lookahead(
    root: Path,
    shared_root: Path,
    baseline_phase6_root: Path | None,
    platform_path: Path,
    *,
    expected_seed: int | None = None,
    expected_workers: int | None = None,
    expected_region_count: int | None = None,
    expected_architecture: Path | None = None,
    expected_route_channel_width: int | None = None,
    reuse_validated_phase6_equivalence: bool = False,
    managed_dag_node: bool = False,
    _validation_session: _ValidationSession | None = None,
    _physical_report: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    session = _validation_session or _ValidationSession()
    root = root.resolve()
    shared_root = shared_root.resolve()
    platform_path = platform_path.resolve()
    baseline_key = (
        str(baseline_phase6_root.resolve())
        if baseline_phase6_root is not None
        else None
    )
    architecture_key = (
        str(expected_architecture.resolve())
        if expected_architecture is not None
        else None
    )
    cache_key = (
        str(root),
        str(shared_root),
        baseline_key,
        str(platform_path),
        expected_seed,
        expected_workers,
        expected_region_count,
        architecture_key,
        expected_route_channel_width,
    )
    if cache_key in session.lookahead:
        return copy.deepcopy(session.lookahead[cache_key])
    validate_shared_phase1_5(
        shared_root,
        platform_path,
        reuse_managed_checkpoint=True,
        _validation_session=session,
    )
    split_root = (
        baseline_phase6_root / "split"
        if baseline_phase6_root is not None
        else shared_root / "split"
    )
    if baseline_phase6_root is not None:
        baseline = validate_phase6_checkpoint(
            baseline_phase6_root,
            shared_root,
            None,
            platform_path,
            validation_mode=(
                "validated-checkpoint-reuse"
                if reuse_validated_phase6_equivalence
                else "full-replay"
            ),
            _validation_session=session,
        )
        if baseline["provider"] != "baseline":
            raise ValidationError("physical lookahead requires baseline Phase 6")
    report = read_json(_require_file(root, "experiment-lookahead-report.json"))
    if report.get("schema") != EXPERIMENT_LOOKAHEAD_SCHEMA or report.get("status") != "pass":
        raise ValidationError("experiment physical-lookahead report is invalid")
    if managed_dag_node:
        if report.get("validation_mode") != MANAGED_DAG_VALIDATION_MODE:
            raise ValidationError("lookahead managed-validation contract is invalid")
    elif report.get("validation_mode") is not None:
        raise ValidationError("lookahead checkpoint requires managed validation")
    physical_path = _require_file(root, "physical/multi-fpga-physical-flow-report.json")
    physical_report = (
        _physical_report
        if _physical_report is not None
        else read_json(physical_path)
    )
    session.validate_physical(physical_report)
    expected = {
        "seed": expected_seed,
        "workers": expected_workers,
        "region_count": expected_region_count,
        "route_channel_width": expected_route_channel_width,
    }
    for field, value in expected.items():
        if value is not None and report.get(field) != value:
            raise ValidationError(
                f"experiment physical-lookahead {field} contract disagrees"
            )
    if (
        expected_architecture is not None
        and not managed_dag_node
        and report.get("architecture_sha256") != _sha256(expected_architecture.resolve())
    ):
        raise ValidationError(
            "experiment physical-lookahead architecture contract disagrees"
        )
    if expected_workers is not None and physical_report.get("execution", {}).get(
        "requested_workers"
    ) != expected_workers:
        raise ValidationError(
            "experiment physical-lookahead physical worker count disagrees"
        )
    if expected_seed is not None or expected_route_channel_width is not None:
        for fpga in physical_report.get("fpgas", []):
            stages = fpga.get("stages", {})
            if expected_seed is not None and stages.get(
                "vpr_pack_place", {}
            ).get("configuration", {}).get("seed") != expected_seed:
                raise ValidationError(
                    "experiment physical-lookahead VPR seed disagrees"
                )
            if expected_route_channel_width is not None and stages.get(
                "vpr_route", {}
            ).get("configuration", {}).get(
                "route_channel_width"
            ) != expected_route_channel_width:
                raise ValidationError(
                    "experiment physical-lookahead VPR channel width disagrees"
                )
    if not managed_dag_node:
        if report.get("physical_summary_sha256") != _sha256(
            _require_file(root, "physical/physical-summary.json")
        ):
            raise ValidationError("experiment physical-lookahead summary seal is broken")
        baseline_digest = report.get("baseline_phase6_manifest_sha256")
        split_manifest = split_root / "manifest.json"
        if split_manifest.is_file():
            if baseline_digest != _sha256(split_manifest):
                raise ValidationError(
                    "experiment physical-lookahead Phase 6 seal is broken"
                )
        elif not isinstance(baseline_digest, str) or len(baseline_digest) != 64 or any(
            character not in "0123456789abcdef" for character in baseline_digest
        ):
            raise ValidationError(
                "experiment physical-lookahead Phase 6 digest is invalid"
            )
    lookahead_report = _require_file(
        root, "lookahead/academic-chimew-lookahead-report.json"
    )
    if not managed_dag_node and report.get("lookahead_report_sha256") != _sha256(lookahead_report):
        raise ValidationError("experiment Chimew lookahead seal is broken")
    lookahead = read_json(lookahead_report)
    if lookahead.get("status") != "pass":
        raise ValidationError("experiment Chimew lookahead did not pass")
    for label in (
        "schedule",
        "crossings",
        "positions",
        "rudy_input",
        "bank_channel_input",
        "electrical_map",
    ):
        path = _require_file(root, f"lookahead/inputs/{label}.json")
        if lookahead.get("artifacts", {}).get(label, {}).get("sha256") != _sha256(path):
            raise ValidationError(f"experiment Chimew lookahead {label} seal is broken")
    result = {"status": "pass", "seed": report["seed"], "metrics": report["metrics"]}
    session.lookahead[cache_key] = copy.deepcopy(result)
    return result


def run_phase6_checkpoint(
    shared_root: Path,
    lookahead_root: Path | None,
    platform_path: Path,
    output_dir: Path,
    *,
    provider: str,
    equivalence_cycles: int = 16,
    equivalence_seed: int = 20260727,
    pin_planner: str | None = None,
    chimew_grouper: str | None = None,
    chimew_refiner: str | None = None,
    chimew_rudy: str | None = None,
    chimew_assigner: str | None = None,
    managed_storage: bool = False,
    managed_dag_node: bool = False,
) -> Dict[str, Any]:
    session = _ValidationSession()
    if provider not in _PROVIDERS:
        raise ValidationError("experiment Phase 6 provider is invalid")
    shared = validate_shared_phase1_5(
        shared_root,
        platform_path,
        reuse_managed_checkpoint=True,
        _validation_session=session,
    )
    if provider == "baseline":
        lookahead = None
    else:
        if lookahead_root is None:
            raise ValidationError(
                f"experiment Phase 6 provider {provider} requires physical lookahead"
            )
        if managed_dag_node:
            if _managed_checkpoint(
                lookahead_root, expected_stage="lookahead"
            ) is None:
                raise ValidationError(
                    "managed Phase 6 requires a validated lookahead checkpoint"
                )
            lookahead_report = read_json(
                _require_file(lookahead_root, "experiment-lookahead-report.json")
            )
            lookahead = {
                "status": lookahead_report.get("status"),
                "seed": lookahead_report.get("seed"),
                "metrics": lookahead_report.get("metrics"),
            }
        else:
            lookahead = validate_physical_lookahead(
                lookahead_root,
                shared_root,
                None,
                platform_path,
                _validation_session=session,
            )
    paths = _shared_paths(shared_root)
    output_dir = _prepare_empty_output(output_dir, "Phase 6 checkpoint")
    schedule_path = paths["schedule"]
    pin_plan_path = None
    position_hints_path = None
    electrical_binding_path = None
    if provider == "placement-aware":
        assert lookahead_root is not None
        region_count = int(
            read_json(lookahead_root / "experiment-lookahead-report.json")[
                "region_count"
            ]
        )
        positions = _placement_aware_positions(
            paths["ir"],
            schedule_path,
            lookahead_root / "lookahead/sources/placement.json",
            region_count=region_count,
        )
        position_hints_path = output_dir / "placement-aware-position-hints.json"
        write_json(position_hints_path, positions)
        plan = build_pin_plan(
            read_json(schedule_path),
            Platform.load(platform_path),
            positions,
            executable=pin_planner,
        )
        pin_plan_path = output_dir / "placement-aware-pin-plan.json"
        write_json(pin_plan_path, plan)
    elif provider == "chimew":
        assert lookahead_root is not None
        inputs = lookahead_root / "lookahead/inputs"
        sources = lookahead_root / "lookahead/sources"
        pipeline_root = output_dir / "chimew-pipeline"
        pipeline = run_chimew_phase6_pipeline(
            inputs / "schedule.json",
            platform_path,
            inputs / "crossings.json",
            inputs / "positions.json",
            inputs / "rudy_input.json",
            inputs / "bank_channel_input.json",
            inputs / "electrical_map.json",
            pipeline_root,
            source_paths={
                "routing": sources / "routing.json",
                "placement": sources / "placement.json",
                "netlist": paths["ir"],
                "architecture": sources / "architecture.json",
                "package_pins": sources / "package-pins.json",
            },
            grouper=chimew_grouper,
            refiner=chimew_refiner,
            rudy=chimew_rudy,
            assigner=chimew_assigner,
            region_count=int(
                read_json(lookahead_root / "experiment-lookahead-report.json")[
                    "region_count"
                ]
            ),
        )
        schedule_path = inputs / "schedule.json"
        adapter = pipeline_root / "phase6-adapter"
        pin_plan_path = adapter / "pin_plan.json"
        position_hints_path = adapter / "position_hints.json"
        electrical_binding_path = adapter / "electrical_binding.json"
        if pipeline.get("status") != "pass":
            raise ValidationError("experiment Chimew pipeline did not pass")
    shutil.copy2(schedule_path, output_dir / "schedule.json")
    phase6 = run_phase6(
        paths["ir"],
        paths["assignment"],
        output_dir / "schedule.json",
        platform_path,
        output_dir / "split",
        equivalence_cycles=equivalence_cycles,
        equivalence_seed=equivalence_seed,
        pin_plan_path=pin_plan_path,
        position_hints_path=position_hints_path,
        electrical_binding_path=electrical_binding_path,
        managed_storage=managed_storage,
    )
    report = {
        "schema": EXPERIMENT_PHASE6_SCHEMA,
        "status": "pass",
        "provider": provider,
        "shared": shared,
        "lookahead": lookahead,
        "equivalence": phase6["equivalence"],
    }
    if managed_dag_node:
        report["validation_mode"] = MANAGED_DAG_VALIDATION_MODE
    else:
        report.update(
            {
                "schedule_sha256": _sha256(output_dir / "schedule.json"),
                "manifest_sha256": _sha256(output_dir / "split/manifest.json"),
            }
        )
    write_json(output_dir / "experiment-phase6-report.json", report)
    if not managed_dag_node:
        validate_phase6_checkpoint(
            output_dir,
            shared_root,
            lookahead_root,
            platform_path,
            validation_mode="producer-self-check",
            _validation_session=session,
        )
    return report


def validate_phase6_checkpoint(
    root: Path,
    shared_root: Path,
    lookahead_root: Path | None,
    platform_path: Path,
    *,
    expected_provider: str | None = None,
    validation_mode: str = "full-replay",
    managed_dag_node: bool = False,
    _validation_session: _ValidationSession | None = None,
) -> Dict[str, Any]:
    if validation_mode not in _PHASE6_VALIDATION_MODES:
        raise ValidationError("experiment Phase 6 validation mode is invalid")
    if validation_mode == "validated-checkpoint-reuse":
        _validate_managed_phase6_checkpoint(root)
        report = read_json(_require_file(root, "experiment-phase6-report.json"))
        provider = report.get("provider")
        if (
            report.get("schema") != EXPERIMENT_PHASE6_SCHEMA
            or provider not in _PROVIDERS
        ):
            raise ValidationError("managed Phase 6 checkpoint report is invalid")
        if expected_provider is not None and provider != expected_provider:
            raise ValidationError("managed Phase 6 provider contract disagrees")
        return {
            "status": "pass",
            "provider": provider,
            "equivalence": report["equivalence"],
        }
    session = _validation_session or _ValidationSession()
    root = root.resolve()
    shared_root = shared_root.resolve()
    platform_path = platform_path.resolve()
    lookahead_key = (
        str(lookahead_root.resolve()) if lookahead_root is not None else None
    )
    cache_key = (str(root), str(shared_root), lookahead_key, str(platform_path))
    cached = session.phase6.get(cache_key)
    if cached is not None:
        if expected_provider is not None and cached["provider"] != expected_provider:
            raise ValidationError("experiment Phase 6 provider contract disagrees")
        return copy.deepcopy(cached)
    validate_shared_phase1_5(
        shared_root,
        platform_path,
        reuse_managed_checkpoint=True,
        _validation_session=session,
    )
    report = read_json(_require_file(root, "experiment-phase6-report.json"))
    provider = report.get("provider")
    if report.get("schema") != EXPERIMENT_PHASE6_SCHEMA or provider not in _PROVIDERS:
        raise ValidationError("experiment Phase 6 checkpoint report is invalid")
    if expected_provider is not None and provider != expected_provider:
        raise ValidationError("experiment Phase 6 provider contract disagrees")
    if managed_dag_node:
        if report.get("validation_mode") != MANAGED_DAG_VALIDATION_MODE:
            raise ValidationError("Phase 6 managed-validation contract is invalid")
        validation_mode = "producer-self-check"
    elif report.get("validation_mode") is not None:
        raise ValidationError("Phase 6 checkpoint requires managed validation")
    if provider == "baseline":
        if report.get("lookahead") is not None:
            raise ValidationError("baseline Phase 6 must not depend on lookahead")
    else:
        if lookahead_root is None:
            raise ValidationError(
                f"experiment Phase 6 provider {provider} requires physical lookahead"
            )
        if managed_dag_node:
            if _managed_checkpoint(
                lookahead_root, expected_stage="lookahead"
            ) is None:
                raise ValidationError(
                    "managed Phase 6 requires a validated lookahead checkpoint"
                )
        else:
            validate_physical_lookahead(
                lookahead_root,
                shared_root,
                None,
                platform_path,
                _validation_session=session,
            )
    paths = _shared_paths(shared_root)
    manifest = _require_file(root, "split/manifest.json")
    validate_phase6(
        paths["ir"],
        paths["assignment"],
        root / "schedule.json",
        platform_path,
        manifest,
        replay_equivalence=validation_mode == "full-replay",
        reconstruct_artifacts=validation_mode == "full-replay",
    )
    if provider == "placement-aware":
        validate_pin_plan(
            read_json(root / "schedule.json"),
            Platform.load(platform_path),
            read_json(root / "placement-aware-position-hints.json"),
            read_json(root / "placement-aware-pin-plan.json"),
        )
    elif provider == "chimew":
        validate_chimew_phase6_pipeline(root / "chimew-pipeline")
    if not managed_dag_node and (
        report.get("schedule_sha256") != _sha256(root / "schedule.json")
        or report.get("manifest_sha256") != _sha256(manifest)
    ):
        raise ValidationError("experiment Phase 6 checkpoint seal is broken")
    result = {
        "status": "pass",
        "provider": provider,
        "equivalence": report["equivalence"],
    }
    session.phase6[cache_key] = copy.deepcopy(result)
    return result


def run_phase7_checkpoint(
    shared_root: Path,
    lookahead_root: Path,
    phase6_root: Path,
    platform_path: Path,
    output_dir: Path,
    *,
    seed: int,
    workers: int,
    yosys: str | None = None,
    vpr: str | None = None,
    architecture_importer: str | None = None,
    packed_importer: str | None = None,
    route_checker: str | None = None,
    openparf_install: Path | None = None,
    openparf_python: Path | None = None,
    route_channel_width: int = 300,
    reuse_validated_phase6_equivalence: bool = False,
    managed_dag_node: bool = False,
) -> Dict[str, Any]:
    session = _ValidationSession()
    phase6 = validate_phase6_checkpoint(
        phase6_root,
        shared_root,
        lookahead_root,
        platform_path,
        validation_mode=(
            "validated-checkpoint-reuse"
            if reuse_validated_phase6_equivalence or managed_dag_node
            else "full-replay"
        ),
        _validation_session=session,
    )
    paths = _shared_paths(shared_root)
    path_database, logic_path_database = _physical_timing_databases(shared_root)
    output_dir = _prepare_empty_output(output_dir, "Phase 7 checkpoint")
    lookahead_report = read_json(lookahead_root / "experiment-lookahead-report.json")
    if phase6["provider"] == "baseline" and seed == lookahead_report["seed"]:
        _link_tree(lookahead_root / "physical", output_dir / "physical")
    else:
        run_multi_fpga_physical_flow(
            phase6_root / "split",
            platform_path,
            phase6_root / "schedule.json",
            output_dir / "physical",
            backend="open",
            architecture=lookahead_root / "physical/architecture/vtr-flagship.xml",
            yosys=yosys,
            vpr=vpr,
            architecture_importer=architecture_importer,
            packed_importer=packed_importer,
            route_checker=route_checker,
            openparf_install=openparf_install,
            openparf_python=openparf_python,
            seed=seed,
            route_channel_width=route_channel_width,
            workers=workers,
            original_ir_path=(paths["ir"] if path_database else None),
            assignment_path=(paths["assignment"] if path_database else None),
            routes_path=(paths["routes"] if path_database else None),
            path_database_path=path_database,
            logic_path_database_path=logic_path_database,
        )
    runtime = run_phase7c(
        phase6_root / "schedule.json",
        platform_path,
        paths["phase3_report"],
        paths["phase4_report"],
        paths["phase5_report"],
        phase6_root / "split/phase6_report.json",
        output_dir / "runtime",
        physical_summary_path=output_dir / "physical/physical-summary.json",
        routes_path=paths["routes"],
        board_link_timing_path=_board_link_timing(shared_root),
        materialize_physical_summary=False,
    )
    if runtime.get("status") != "pass":
        raise ValidationError("experiment Phase 7C did not reach physical closure")
    report = {
        "schema": EXPERIMENT_PHASE7_SCHEMA,
        "status": "pass",
        "provider": phase6["provider"],
        "physical_seed": seed,
        "workers": workers,
        "route_channel_width": route_channel_width,
        "qor_projection": _phase7_qor_projection(
            read_json(output_dir / "runtime/qor_report.json")
        ),
    }
    if managed_dag_node:
        report["validation_mode"] = MANAGED_DAG_VALIDATION_MODE
    else:
        report.update(
            {
                "phase6_manifest_sha256": _sha256(
                    phase6_root / "split/manifest.json"
                ),
                "frozen_upstream": {
                    "emuir_sha256": _sha256(paths["ir"]),
                    "assignment_sha256": _sha256(paths["assignment"]),
                    "routes_sha256": _sha256(paths["routes"]),
                    "schedule_sha256": _sha256(phase6_root / "schedule.json"),
                },
                "physical_summary_sha256": _sha256(
                    output_dir / "physical/physical-summary.json"
                ),
                "physical_flow_report_sha256": _sha256(
                    output_dir / "physical/multi-fpga-physical-flow-report.json"
                ),
                "qor_sha256": _sha256(output_dir / "runtime/qor_report.json"),
            }
        )
    write_json(output_dir / "experiment-phase7-report.json", report)
    if not managed_dag_node:
        validate_phase7_checkpoint(
            output_dir,
            shared_root,
            lookahead_root,
            phase6_root,
            platform_path,
            reuse_validated_phase6_equivalence=reuse_validated_phase6_equivalence,
            replay_qor=False,
            _validation_session=session,
        )
    return report


def validate_phase7_checkpoint(
    root: Path,
    shared_root: Path,
    lookahead_root: Path,
    phase6_root: Path,
    platform_path: Path,
    *,
    expected_seed: int | None = None,
    expected_workers: int | None = None,
    expected_route_channel_width: int | None = None,
    reuse_validated_phase6_equivalence: bool = False,
    replay_qor: bool = True,
    managed_dag_node: bool = False,
    _validation_session: _ValidationSession | None = None,
) -> Dict[str, Any]:
    session = _validation_session or _ValidationSession()
    phase6 = validate_phase6_checkpoint(
        phase6_root,
        shared_root,
        lookahead_root,
        platform_path,
        validation_mode=(
            "validated-checkpoint-reuse"
            if reuse_validated_phase6_equivalence or managed_dag_node
            else "full-replay"
        ),
        _validation_session=session,
    )
    report = read_json(_require_file(root, "experiment-phase7-report.json"))
    schema = report.get("schema")
    if (
        schema not in {LEGACY_EXPERIMENT_PHASE7_SCHEMA, EXPERIMENT_PHASE7_SCHEMA}
        or report.get("status") != "pass"
        or report.get("provider") != phase6["provider"]
    ):
        raise ValidationError("experiment Phase 7 checkpoint report is invalid")
    if managed_dag_node:
        if report.get("validation_mode") != MANAGED_DAG_VALIDATION_MODE:
            raise ValidationError("Phase 7 managed-validation contract is invalid")
    elif report.get("validation_mode") is not None:
        raise ValidationError("Phase 7 checkpoint requires managed validation")
    if expected_seed is not None and report.get("physical_seed") != expected_seed:
        raise ValidationError("experiment Phase 7 seed contract disagrees")
    if expected_workers is not None and report.get("workers") != expected_workers:
        raise ValidationError("experiment Phase 7 worker contract disagrees")
    if expected_route_channel_width is not None and report.get(
        "route_channel_width"
    ) != expected_route_channel_width:
        raise ValidationError("experiment Phase 7 channel-width contract disagrees")
    paths = _shared_paths(shared_root)
    if not managed_dag_node:
        expected_upstream = {
            "emuir_sha256": _sha256(paths["ir"]),
            "assignment_sha256": _sha256(paths["assignment"]),
            "routes_sha256": _sha256(paths["routes"]),
            "schedule_sha256": _sha256(phase6_root / "schedule.json"),
        }
        if report.get("frozen_upstream") != expected_upstream:
            raise ValidationError("experiment Phase 7 frozen-upstream seal is broken")
    physical_report = read_json(
        _require_file(root, "physical/multi-fpga-physical-flow-report.json")
    )
    session.validate_physical(physical_report)
    if expected_workers is not None and physical_report.get("execution", {}).get(
        "requested_workers"
    ) != expected_workers:
        raise ValidationError("experiment Phase 7 physical worker count disagrees")
    if expected_seed is not None or expected_route_channel_width is not None:
        for fpga in physical_report.get("fpgas", []):
            stages = fpga.get("stages", {})
            if expected_seed is not None and stages.get(
                "vpr_pack_place", {}
            ).get("configuration", {}).get("seed") != expected_seed:
                raise ValidationError("experiment Phase 7 VPR seed disagrees")
            if expected_route_channel_width is not None and stages.get(
                "vpr_route", {}
            ).get("configuration", {}).get(
                "route_channel_width"
            ) != expected_route_channel_width:
                raise ValidationError("experiment Phase 7 VPR channel width disagrees")
    physical_report_path = root / "physical/multi-fpga-physical-flow-report.json"
    if not managed_dag_node and (
        report.get("physical_summary_sha256")
        != _sha256(root / "physical/physical-summary.json")
        or report.get("qor_sha256") != _sha256(root / "runtime/qor_report.json")
        or (
            schema == EXPERIMENT_PHASE7_SCHEMA
            and report.get("physical_flow_report_sha256")
            != _sha256(physical_report_path)
        )
    ):
        raise ValidationError("experiment Phase 7 checkpoint seal is broken")
    qor = read_json(root / "runtime/qor_report.json")
    projection = _phase7_qor_projection(qor)
    if schema == EXPERIMENT_PHASE7_SCHEMA:
        if report.get("qor_projection") != projection or "qor" in report:
            raise ValidationError("experiment Phase 7 QoR projection is invalid")
    elif report.get("qor") != qor:
        raise ValidationError("experiment Phase 7 legacy QoR seal is broken")
    if replay_qor:
        with tempfile.TemporaryDirectory() as temporary:
            replay = run_phase7c(
                phase6_root / "schedule.json",
                platform_path,
                paths["phase3_report"],
                paths["phase4_report"],
                paths["phase5_report"],
                phase6_root / "split/phase6_report.json",
                Path(temporary),
                physical_summary_path=root / "physical/physical-summary.json",
                routes_path=paths["routes"],
                board_link_timing_path=_board_link_timing(shared_root),
                materialize_physical_summary=False,
            )
            if replay.get("status") != "pass" or read_json(
                Path(temporary) / "qor_report.json"
            ) != qor:
                raise ValidationError("experiment Phase 7 QoR replay disagrees")
    return {
        "status": "pass",
        "provider": report["provider"],
        "physical_seed": report["physical_seed"],
        "qor_projection": projection,
    }
