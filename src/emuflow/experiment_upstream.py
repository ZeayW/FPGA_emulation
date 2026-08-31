"""Fine-grained reusable Phase 1-5 experiment checkpoints.

The normal end-to-end driver is intentionally convenient and monolithic.  This
module exposes stable algorithms at cache boundaries which are useful for
controlled experiments: frontend, pre-partition timing, cut-path timing
projection, system routing, and TDM scheduling. Phase 3 lives in the dedicated
``experiment_partition`` module so its implementation closure does not
invalidate unrelated upstream stages. Every runner has a read-only validator
and records the exact hashes it consumed.
"""

from __future__ import annotations

import errno
import hashlib
import math
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from .errors import EmuFlowError, ValidationError
from .cut_segment_qualification import (
    build_cut_segment_qualification,
    validate_cut_segment_qualification,
)
from .experiment_stages import _prepare_empty_output, validate_shared_phase1_5
from .io import read_json, write_json
from .ir import EmuIR
from .opensta import (
    DEFAULT_TIMING_MODEL,
    run_opensta_path_database,
)
from .phase1 import analyze_clock_topology, run_phase1
from .phase4 import run_phase4, validate_phase4
from .phase5 import run_phase5, validate_phase5
from .platform import Platform
from .routing import load_route_constraints
from .sta import (
    derive_partition_net_weights,
    project_sta_path_database,
    validate_sta_path_database,
)
from .synthesis import run_generic_yosys
from .tdm import TDM_STATIC_EXACT_PROVIDER
from .vpr import VTR_HARD_BLOCK_PROFILE, run_vtr_yosys
from .vtr_netlist import normalize_vtr_hard_block_json


EXPERIMENT_FRONTEND_SCHEMA = "emuflow.experiment-frontend-checkpoint/v1"
EXPERIMENT_TIMING_SCHEMA = "emuflow.experiment-timing-checkpoint/v1"
EXPERIMENT_PARTITION_SCHEMA = "emuflow.experiment-partition-checkpoint/v1"
EXPERIMENT_CUT_TIMING_SCHEMA = "emuflow.experiment-cut-timing-checkpoint/v4"
EXPERIMENT_ROUTE_SCHEMA = "emuflow.experiment-route-checkpoint/v1"
EXPERIMENT_TDM_SCHEMA = "emuflow.experiment-tdm-checkpoint/v1"
EXPERIMENT_SHARED_SCHEMA = "emuflow.experiment-shared-phase1-5/v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _require(root: Path, relative: str) -> Path:
    path = root.resolve() / relative
    if not path.is_file() or path.is_symlink():
        raise ValidationError(f"upstream experiment artifact is missing: {relative}")
    return path


def _float_equal(left: float, right: float) -> bool:
    return math.isclose(float(left), float(right), rel_tol=1.0e-12, abs_tol=1.0e-12)


def _portable_cut_timing_projection(
    artifact: Mapping[str, Any],
    database_name: str,
    *,
    database_sha256: str | None = None,
) -> Dict[str, Any]:
    """Canonicalize projected-STA provenance without retaining staging paths.

    New projections seal their input with ``input_sha256`` so that a checkpoint
    remains independently reconstructible after an atomic move into the
    content-addressed store.  Accept the former filename-only form only for
    reading historical checkpoints; all newly produced projections use the
    digest form.
    """
    source = artifact.get("source")
    if not isinstance(source, dict):
        raise ValidationError("cut-timing projection source is invalid")
    if source.get("provider") != "partition-projected-sta-paths-v1":
        raise ValidationError("cut-timing projection source is invalid")

    input_sha256 = source.get("input_sha256")
    if input_sha256 is not None:
        if not isinstance(input_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", input_sha256):
            raise ValidationError("cut-timing projection source is invalid")
        if database_sha256 is not None and input_sha256 != database_sha256:
            raise ValidationError("cut-timing projection source is invalid")
        portable = dict(artifact)
        portable["source"] = {
            "provider": source["provider"],
            "input_sha256": input_sha256,
        }
        return portable

    input_path = source.get("input")
    if not isinstance(input_path, str) or not input_path or Path(input_path).name != database_name:
        raise ValidationError("cut-timing projection source is invalid")
    portable = dict(artifact)
    portable["source"] = {**source, "input": database_name}
    return portable


def run_frontend_checkpoint(
    platform_path: Path,
    output_dir: Path,
    *,
    sources: Iterable[Path] = (),
    top: Optional[str] = None,
    clocks: Iterable[str] = (),
    yosys_json: Optional[Path] = None,
    yosys: Optional[str] = None,
    mapping_profile: str = "vtr-hard-blocks",
    require_no_fabric_clock: bool = True,
) -> Dict[str, Any]:
    output_dir = _prepare_empty_output(output_dir, "frontend checkpoint")
    sources = [path.resolve() for path in sources]
    source_records = []
    source_root = output_dir / "sources"
    source_root.mkdir()
    for index, path in enumerate(sources):
        if not path.is_file() or path.is_symlink():
            raise EmuFlowError(f"frontend RTL source is not a regular file: {path}")
        relative = f"{index:04d}-{path.name}"
        copied = source_root / relative
        shutil.copy2(path, copied)
        source_records.append(
            {
                "original_path": str(path),
                "artifact": f"sources/{relative}",
                "bytes": copied.stat().st_size,
                "sha256": _sha256(copied),
            }
        )
    synthesized = output_dir / "synthesized.json"
    synthesis_report: Dict[str, Any] | None = None
    normalization_report: Dict[str, Any] | None = None
    if yosys_json is not None:
        if sources:
            raise EmuFlowError("frontend checkpoint accepts RTL or Yosys JSON, not both")
        source_json = yosys_json.resolve()
        if not source_json.is_file():
            raise EmuFlowError(f"Yosys JSON does not exist: {source_json}")
        shutil.copy2(source_json, synthesized)
        copied = source_root / "0000-provided-yosys.json"
        shutil.copy2(source_json, copied)
        source_records.append(
            {
                "original_path": str(source_json),
                "artifact": "sources/0000-provided-yosys.json",
                "bytes": copied.stat().st_size,
                "sha256": _sha256(copied),
            }
        )
        mode = "provided-yosys-json"
    else:
        if not sources or top is None:
            raise EmuFlowError("frontend checkpoint requires RTL sources and --top")
        if mapping_profile == "vtr-hard-blocks":
            raw = output_dir / "vtr-hard-block-atoms.json"
            synthesis_report = run_vtr_yosys(
                sources,
                top,
                output_dir / "design.eblif",
                executable=yosys,
                log_path=output_dir / "yosys.log",
                hard_blocks=True,
                json_output=raw,
            )
            normalization_report = normalize_vtr_hard_block_json(
                raw, synthesized, top=top
            )
            mode = "vtr-lut6-ff-hard-blocks"
        elif mapping_profile == "generic-soft":
            synthesis_report = run_generic_yosys(
                sources,
                top,
                synthesized,
                executable=yosys,
                log_path=output_dir / "yosys.log",
            )
            mode = "generic-lut6-ff"
        else:
            raise ValidationError("frontend mapping profile is invalid")
    phase1 = run_phase1(
        synthesized,
        platform_path,
        output_dir / "phase1",
        top=top,
        clocks=clocks,
        require_no_fabric_clock=require_no_fabric_clock,
    )
    if phase1.get("status") != "pass":
        raise ValidationError(f"frontend Phase 1 failed: {phase1.get('status')}")
    report = {
        "schema": EXPERIMENT_FRONTEND_SCHEMA,
        "status": "pass",
        "mode": mode,
        "mapping_profile": (
            VTR_HARD_BLOCK_PROFILE if mode == "vtr-lut6-ff-hard-blocks" else mapping_profile
        ),
        "top": top,
        "clocks": sorted(set(clocks)),
        "require_no_fabric_clock": require_no_fabric_clock,
        "source_sha256": {
            record["original_path"]: record["sha256"]
            for record in source_records
        },
        "source_artifacts": source_records,
        "synthesized_sha256": _sha256(synthesized),
        "emuir_sha256": _sha256(output_dir / "phase1/design.emuir.json"),
        "platform_sha256": _sha256(platform_path.resolve()),
        "phase1_report_sha256": _sha256(output_dir / "phase1/phase1_report.json"),
        "phase1": phase1,
        **({"synthesis": synthesis_report} if synthesis_report is not None else {}),
        **(
            {"normalization": normalization_report}
            if normalization_report is not None
            else {}
        ),
    }
    write_json(output_dir / "experiment-frontend-report.json", report)
    validate_frontend_checkpoint(output_dir, platform_path)
    return report


def validate_frontend_checkpoint(root: Path, platform_path: Path) -> Dict[str, Any]:
    report = read_json(_require(root, "experiment-frontend-report.json"))
    if report.get("schema") != EXPERIMENT_FRONTEND_SCHEMA or report.get("status") != "pass":
        raise ValidationError("frontend checkpoint report is invalid")
    synthesized = _require(root, "synthesized.json")
    ir_path = _require(root, "phase1/design.emuir.json")
    phase1_path = _require(root, "phase1/phase1_report.json")
    normalized_platform = _require(root, "phase1/platform.normalized.json")
    source_records = report.get("source_artifacts")
    if not isinstance(source_records, list) or not source_records:
        raise ValidationError("frontend source artifacts are missing")
    seen_sources = set()
    for record in source_records:
        if not isinstance(record, dict):
            raise ValidationError("frontend source artifact record is invalid")
        relative = record.get("artifact")
        relative_path = Path(relative) if isinstance(relative, str) else None
        original_path = record.get("original_path")
        if (
            relative_path is None
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or len(relative_path.parts) != 2
            or relative_path.parts[0] != "sources"
            or relative in seen_sources
        ):
            raise ValidationError("frontend source artifact path is invalid")
        if not isinstance(original_path, str) or not original_path:
            raise ValidationError("frontend source provenance path is invalid")
        seen_sources.add(relative)
        copied = _require(root, relative)
        if (
            record.get("bytes") != copied.stat().st_size
            or record.get("sha256") != _sha256(copied)
        ):
            raise ValidationError("frontend source artifact seal is broken")
    if report.get("source_sha256") != {
        record.get("original_path"): record.get("sha256")
        for record in source_records
    }:
        raise ValidationError("frontend source provenance is inconsistent")
    if report.get("synthesized_sha256") != _sha256(synthesized):
        raise ValidationError("frontend synthesized JSON seal is broken")
    if report.get("emuir_sha256") != _sha256(ir_path):
        raise ValidationError("frontend EmuIR seal is broken")
    if report.get("phase1_report_sha256") != _sha256(phase1_path):
        raise ValidationError("frontend Phase 1 report seal is broken")
    if report.get("platform_sha256") != _sha256(platform_path.resolve()):
        raise ValidationError("frontend platform input seal is broken")
    platform = Platform.load(platform_path)
    if read_json(normalized_platform) != platform.to_dict():
        raise ValidationError("frontend normalized platform is not the supplied BoardDB")
    ir = EmuIR.load(ir_path)
    phase1 = read_json(phase1_path)
    totals = ir.resource_totals()
    aggregate = {
        resource: sum(fpga.effective_capacity.get(resource, 0) for fpga in platform.fpgas)
        for resource in sorted(
            {resource for fpga in platform.fpgas for resource in fpga.effective_capacity}
        )
    }
    if phase1.get("resource_totals") != totals.to_dict(include_zeros=False):
        raise ValidationError("frontend Phase 1 resource totals are invalid")
    if phase1.get("fits_on_platform") != totals.fits_capacity(aggregate):
        raise ValidationError("frontend Phase 1 capacity result is invalid")
    if phase1.get("clock_topology") != analyze_clock_topology(ir):
        raise ValidationError("frontend Phase 1 clock-topology result is invalid")
    if report.get("require_no_fabric_clock") and phase1["clock_topology"][
        "fabric_logic_clock_nets"
    ]:
        raise ValidationError("frontend checkpoint contains an unsafe fabric clock")
    return {
        "status": "pass",
        "design": ir.value["design"]["name"],
        "instances": len(ir.value["instances"]),
        "nets": len(ir.value["nets"]),
    }


def run_timing_checkpoint(
    frontend_root: Path,
    output_dir: Path,
    *,
    clocks: Mapping[str, float],
    timing_model_path: Path = DEFAULT_TIMING_MODEL,
    architecture_timing_db_path: Optional[Path] = None,
    opensta: Optional[str] = None,
    max_paths: int = 200000,
    criticality_scale: float = 9.0,
    criticality_exponent: float = 2.0,
) -> Dict[str, Any]:
    ir_path = _require(frontend_root, "phase1/design.emuir.json")
    output_dir = _prepare_empty_output(output_dir, "timing checkpoint")
    database = output_dir / "path-database.json"
    sta = run_opensta_path_database(
        ir_path,
        database,
        clocks=clocks,
        timing_model_path=timing_model_path,
        architecture_timing_db_path=architecture_timing_db_path,
        executable=opensta,
        max_paths=max_paths,
        log_path=output_dir / "opensta.log",
    )
    weights = derive_partition_net_weights(
        database,
        ir_path,
        output_dir / "partition-net-weights.json",
        criticality_scale=criticality_scale,
        criticality_exponent=criticality_exponent,
    )
    report = {
        "schema": EXPERIMENT_TIMING_SCHEMA,
        "status": "pass",
        "frontend_emuir_sha256": _sha256(ir_path),
        "clocks": dict(sorted((name, float(period)) for name, period in clocks.items())),
        "max_paths": max_paths,
        "criticality_scale": float(criticality_scale),
        "criticality_exponent": float(criticality_exponent),
        "timing_model_sha256": _sha256(timing_model_path.resolve()),
        "architecture_timing_db_sha256": (
            _sha256(architecture_timing_db_path.resolve())
            if architecture_timing_db_path is not None
            else None
        ),
        "path_database_sha256": _sha256(database),
        "partition_net_weights_sha256": _sha256(output_dir / "partition-net-weights.json"),
        "sta": sta,
        "weights": weights,
    }
    write_json(output_dir / "experiment-timing-report.json", report)
    validate_timing_checkpoint(frontend_root, output_dir)
    return report


def validate_timing_checkpoint(frontend_root: Path, root: Path) -> Dict[str, Any]:
    ir_path = _require(frontend_root, "phase1/design.emuir.json")
    report = read_json(_require(root, "experiment-timing-report.json"))
    if report.get("schema") != EXPERIMENT_TIMING_SCHEMA or report.get("status") != "pass":
        raise ValidationError("timing checkpoint report is invalid")
    database_path = _require(root, "path-database.json")
    weights_path = _require(root, "partition-net-weights.json")
    if report.get("frontend_emuir_sha256") != _sha256(ir_path):
        raise ValidationError("timing checkpoint frontend seal is broken")
    if report.get("path_database_sha256") != _sha256(database_path):
        raise ValidationError("timing path database seal is broken")
    if report.get("partition_net_weights_sha256") != _sha256(weights_path):
        raise ValidationError("timing partition weights seal is broken")
    checked = validate_sta_path_database(database_path, ir_path)
    database = read_json(database_path)
    weights = read_json(weights_path)
    scale = float(report["criticality_scale"])
    exponent = float(report["criticality_exponent"])
    expected_criticality: Dict[str, float] = {}
    expected_counts: Dict[str, int] = {}
    for path in database["paths"]:
        criticality = max(0.0, min(1.0, 1.0 - path["slack_ns"] / path["clock_period_ns"]))
        for net in path["path_nets"]:
            expected_criticality[net] = max(expected_criticality.get(net, 0.0), criticality)
            expected_counts[net] = expected_counts.get(net, 0) + 1
    if weights.get("criticality") != dict(sorted(expected_criticality.items())):
        raise ValidationError("timing partition criticality reconstruction failed")
    if weights.get("path_count") != dict(sorted(expected_counts.items())):
        raise ValidationError("timing partition path counts are invalid")
    expected_weights = {
        net: 1.0 + scale * criticality**exponent
        for net, criticality in sorted(expected_criticality.items())
        if criticality > 0.0
    }
    actual_weights = weights.get("weights")
    if not isinstance(actual_weights, dict) or set(actual_weights) != set(expected_weights):
        raise ValidationError("timing partition weight coverage is invalid")
    if any(not _float_equal(actual_weights[net], value) for net, value in expected_weights.items()):
        raise ValidationError("timing partition weight reconstruction failed")
    return {"status": "pass", "paths": checked["paths"], "weighted_nets": len(expected_weights)}


def run_cut_timing_checkpoint(
    frontend_root: Path,
    timing_root: Path,
    partition_root: Path,
    output_dir: Path,
    *,
    clocks: Mapping[str, float],
    timing_model_path: Path = DEFAULT_TIMING_MODEL,
    architecture_timing_db_path: Optional[Path] = None,
    opensta: Optional[str] = None,
    max_paths: int = 200000,
) -> Dict[str, Any]:
    ir_path = _require(frontend_root, "phase1/design.emuir.json")
    assignment_path = _require(partition_root, "assignment.json")
    validate_timing_checkpoint(frontend_root, timing_root)
    assignment = read_json(assignment_path)
    cut_nets = sorted(
        item["net"] for item in assignment.get("cut_nets", [])
        if isinstance(item, dict) and isinstance(item.get("net"), str)
    )
    if not cut_nets:
        raise ValidationError("cut-timing checkpoint requires partition cut nets")
    output_dir = _prepare_empty_output(output_dir, "cut-timing checkpoint")
    complete_database = _require(timing_root, "path-database.json")
    qualification = build_cut_segment_qualification(
        ir_path,
        assignment_path,
        complete_database,
        timing_model_path=timing_model_path,
        architecture_timing_db_path=architecture_timing_db_path,
    )
    qualification_path = output_dir / "cut-segment-qualification.json"
    write_json(qualification_path, qualification)
    # Functional cut coverage and segment identities come from the independent
    # EmuIR qualification above.  Phase 4/5 and final global timing continue to
    # consume the complete original TimingPathDB population; provider-specific
    # directed queries are not a sound reconvergent-path enumeration method.
    projection = project_sta_path_database(
        complete_database,
        assignment_path,
        output_dir / "cut-timing-paths.json",
    )
    report = {
        "schema": EXPERIMENT_CUT_TIMING_SCHEMA,
        "status": "pass",
        "emuir_sha256": _sha256(ir_path),
        "assignment_sha256": _sha256(assignment_path),
        "complete_path_database_sha256": _sha256(complete_database),
        "cut_nets": cut_nets,
        "clocks": dict(sorted((name, float(period)) for name, period in clocks.items())),
        "timing_model_sha256": _sha256(timing_model_path.resolve()),
        "architecture_timing_db_sha256": (
            _sha256(architecture_timing_db_path.resolve())
            if architecture_timing_db_path is not None
            else None
        ),
        "cut_segment_qualification_sha256": _sha256(qualification_path),
        "cut_timing_paths_sha256": _sha256(output_dir / "cut-timing-paths.json"),
        "cut_segment_qualification": qualification,
        "projection": projection,
    }
    write_json(output_dir / "experiment-cut-timing-report.json", report)
    validate_cut_timing_checkpoint(
        frontend_root,
        timing_root,
        partition_root,
        output_dir,
        timing_model_path=timing_model_path,
        architecture_timing_db_path=architecture_timing_db_path,
    )
    return report


def validate_cut_timing_checkpoint(
    frontend_root: Path,
    timing_root: Path,
    partition_root: Path,
    root: Path,
    *,
    timing_model_path: Path = DEFAULT_TIMING_MODEL,
    architecture_timing_db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    ir_path = _require(frontend_root, "phase1/design.emuir.json")
    complete_database = _require(timing_root, "path-database.json")
    validate_timing_checkpoint(frontend_root, timing_root)
    assignment_path = _require(partition_root, "assignment.json")
    report = read_json(_require(root, "experiment-cut-timing-report.json"))
    if report.get("schema") != EXPERIMENT_CUT_TIMING_SCHEMA or report.get("status") != "pass":
        raise ValidationError("cut-timing checkpoint report is invalid")
    qualification_path = _require(root, "cut-segment-qualification.json")
    projected = _require(root, "cut-timing-paths.json")
    if report.get("emuir_sha256") != _sha256(ir_path) or report.get(
        "assignment_sha256"
    ) != _sha256(assignment_path):
        raise ValidationError("cut-timing input seal is broken")
    if report.get("complete_path_database_sha256") != _sha256(
        complete_database
    ):
        raise ValidationError("cut-timing complete path database seal is broken")
    if report.get("cut_timing_paths_sha256") != _sha256(projected):
        raise ValidationError("cut-timing artifact seal is broken")
    if report.get("cut_segment_qualification_sha256") != _sha256(
        qualification_path
    ):
        raise ValidationError("cut-timing qualification artifact seal is broken")
    if report.get("timing_model_sha256") != _sha256(timing_model_path.resolve()):
        raise ValidationError("cut-timing model seal is broken")
    expected_architecture_sha = (
        _sha256(architecture_timing_db_path.resolve())
        if architecture_timing_db_path is not None
        else None
    )
    if report.get("architecture_timing_db_sha256") != expected_architecture_sha:
        raise ValidationError("cut-timing architecture timing seal is broken")
    assignment = read_json(assignment_path)
    expected_cut_nets = sorted(
        item["net"]
        for item in assignment.get("cut_nets", [])
        if isinstance(item, dict) and isinstance(item.get("net"), str)
    )
    if report.get("cut_nets") != expected_cut_nets:
        raise ValidationError("cut-timing cut-net set is not reconstructed")
    qualification = read_json(qualification_path)
    if report.get("cut_segment_qualification") != qualification:
        raise ValidationError("cut-timing embedded qualification disagrees")
    qualification_validation = validate_cut_segment_qualification(
        qualification,
        ir_path,
        assignment_path,
        complete_database,
        timing_model_path=timing_model_path,
        architecture_timing_db_path=architecture_timing_db_path,
    )
    with tempfile.TemporaryDirectory(prefix="emuflow-cut-timing-validate-") as temporary:
        rebuilt = Path(temporary) / "projected.json"
        project_sta_path_database(
            complete_database, assignment_path, rebuilt
        )
        if _portable_cut_timing_projection(
            read_json(rebuilt),
            complete_database.name,
            database_sha256=_sha256(complete_database),
        ) != _portable_cut_timing_projection(
            read_json(projected),
            complete_database.name,
            database_sha256=_sha256(complete_database),
        ):
            raise ValidationError("cut-timing projection reconstruction failed")
    return {
        "status": "pass",
        "paths": validate_sta_path_database(complete_database, ir_path)["paths"],
        "cut_nets": len(expected_cut_nets),
        "timed_nets": qualification_validation["timed_structural_nets"],
        "untimed_nets": qualification_validation["no_timed_endpoint_nets"],
    }


def run_route_checkpoint(
    partition_root: Path,
    cut_timing_root: Path,
    platform_path: Path,
    output_dir: Path,
    *,
    constraints_path: Optional[Path] = None,
    frame_slots: Optional[int] = None,
    max_iterations: Optional[int] = None,
    provider: Optional[str] = None,
    candidate_workers: int = 1,
    router: Optional[str] = None,
) -> Dict[str, Any]:
    assignment = _require(partition_root, "assignment.json")
    timing_paths = _require(cut_timing_root, "cut-timing-paths.json")
    output_dir = _prepare_empty_output(output_dir, "route checkpoint")
    phase4 = run_phase4(
        assignment,
        platform_path,
        output_dir,
        constraints_path=constraints_path,
        frame_slots=frame_slots,
        max_iterations=max_iterations,
        provider=provider,
        timing_paths_path=timing_paths,
        router=router,
        candidate_workers=candidate_workers,
    )
    report = {
        "schema": EXPERIMENT_ROUTE_SCHEMA,
        "status": "pass",
        "assignment_sha256": _sha256(assignment),
        "timing_paths_sha256": _sha256(timing_paths),
        "platform_sha256": _sha256(platform_path.resolve()),
        "routes_sha256": _sha256(output_dir / "routes.json"),
        "phase4_report_sha256": _sha256(output_dir / "phase4_report.json"),
        "phase4": phase4,
    }
    write_json(output_dir / "experiment-route-report.json", report)
    validate_route_checkpoint(
        partition_root,
        cut_timing_root,
        platform_path,
        output_dir,
        constraints_path=constraints_path,
    )
    return report


def validate_route_checkpoint(
    partition_root: Path,
    cut_timing_root: Path,
    platform_path: Path,
    root: Path,
    *,
    constraints_path: Path | None = None,
    expected_provider: str | None = None,
    expected_candidate_workers: int | None = None,
) -> Dict[str, Any]:
    assignment = _require(partition_root, "assignment.json")
    timing_paths = _require(cut_timing_root, "cut-timing-paths.json")
    routes = _require(root, "routes.json")
    report = read_json(_require(root, "experiment-route-report.json"))
    if report.get("schema") != EXPERIMENT_ROUTE_SCHEMA or report.get("status") != "pass":
        raise ValidationError("route checkpoint report is invalid")
    phase4_report = _require(root, "phase4_report.json")
    for label, path in {
        "assignment_sha256": assignment,
        "timing_paths_sha256": timing_paths,
        "platform_sha256": platform_path.resolve(),
        "routes_sha256": routes,
        "phase4_report_sha256": phase4_report,
    }.items():
        if report.get(label) != _sha256(path):
            raise ValidationError(f"route checkpoint {label} seal is broken")
    phase4 = read_json(phase4_report)
    if report.get("phase4") != phase4:
        raise ValidationError("route checkpoint embedded Phase 4 report disagrees")
    if expected_provider is not None and (
        read_json(routes).get("provider") != expected_provider
        or phase4.get("provider") != expected_provider
    ):
        raise ValidationError("route checkpoint provider contract disagrees")
    if expected_candidate_workers is not None:
        candidate_generation = phase4.get("candidate_generation")
        if (
            not isinstance(candidate_generation, dict)
            or candidate_generation.get("requested_workers")
            != expected_candidate_workers
        ):
            raise ValidationError(
                "route checkpoint candidate-worker contract disagrees"
            )
    if constraints_path is not None:
        expected_constraints = load_route_constraints(
            constraints_path, Platform.load(platform_path)
        )
        if read_json(_require(root, "route_constraints.normalized.json")) != (
            expected_constraints
        ) or read_json(routes).get("constraints") != expected_constraints:
            raise ValidationError("route checkpoint constraints contract disagrees")
    return {"status": "pass", **validate_phase4(assignment, platform_path, routes, timing_paths_path=timing_paths)}


def run_tdm_checkpoint(
    route_root: Path,
    platform_path: Path,
    output_dir: Path,
    *,
    simulation_frames: int = 16,
    provider: Optional[str] = None,
    ratio_max_iterations: int = 500,
    max_ratio: Optional[int] = None,
    ratio_quantum: int = 8,
    post_refinement_iterations: int = 200,
    slot_refinement_iterations: int = 0,
    ratio_optimizer: Optional[str] = None,
    timing_dag_optimizer: Optional[str] = None,
    slot_optimizer: Optional[str] = None,
) -> Dict[str, Any]:
    routes = _require(route_root, "routes.json")
    output_dir = _prepare_empty_output(output_dir, "TDM checkpoint")
    phase5 = run_phase5(
        routes,
        platform_path,
        output_dir,
        simulation_frames=simulation_frames,
        provider=provider,
        ratio_optimizer=ratio_optimizer,
        timing_dag_optimizer=timing_dag_optimizer,
        slot_optimizer=slot_optimizer,
        ratio_max_iterations=ratio_max_iterations,
        max_ratio=max_ratio,
        ratio_quantum=ratio_quantum,
        post_refinement_iterations=post_refinement_iterations,
        slot_refinement_iterations=slot_refinement_iterations,
    )
    report = {
        "schema": EXPERIMENT_TDM_SCHEMA,
        "status": "pass",
        "routes_sha256": _sha256(routes),
        "platform_sha256": _sha256(platform_path.resolve()),
        "schedule_sha256": _sha256(output_dir / "schedule.json"),
        "phase5_report_sha256": _sha256(output_dir / "phase5_report.json"),
        "phase5": phase5,
    }
    write_json(output_dir / "experiment-tdm-report.json", report)
    validate_tdm_checkpoint(route_root, platform_path, output_dir)
    return report


def validate_tdm_checkpoint(
    route_root: Path,
    platform_path: Path,
    root: Path,
    *,
    constraints_path: Path | None = None,
    expected_provider: str | None = None,
) -> Dict[str, Any]:
    routes = _require(route_root, "routes.json")
    schedule = _require(root, "schedule.json")
    ratio_plan = root / "ratio_plan.json"
    report = read_json(_require(root, "experiment-tdm-report.json"))
    if report.get("schema") != EXPERIMENT_TDM_SCHEMA or report.get("status") != "pass":
        raise ValidationError("TDM checkpoint report is invalid")
    phase5_report = _require(root, "phase5_report.json")
    phase5 = read_json(phase5_report)
    route_document = read_json(routes)
    schedule_document = read_json(schedule)
    actual_provider = schedule_document.get("provider")
    exact_mode = route_document.get("semantic_contract") is not None
    if exact_mode != (actual_provider == TDM_STATIC_EXACT_PROVIDER):
        raise ValidationError(
            "TDM checkpoint exact-cut route/schedule contract disagrees"
        )
    if phase5.get("provider") != actual_provider:
        raise ValidationError(
            "TDM checkpoint Phase 5 provider disagrees with schedule"
        )
    if exact_mode and ratio_plan.exists():
        raise ValidationError(
            "static exact TDM checkpoint may not contain a ratio plan"
        )
    if constraints_path is not None:
        constraints = load_route_constraints(
            constraints_path, Platform.load(platform_path)
        )
        if route_document.get("constraints") != constraints:
            raise ValidationError("TDM route-constraints contract disagrees")
        if not exact_mode:
            if not ratio_plan.is_file():
                raise ValidationError(
                    "TDM contest constraints require a ratio plan"
                )
            configuration = read_json(ratio_plan).get("configuration", {})
            if (
                configuration.get("ratio_quantum")
                != constraints["tdm_ratio_quantum"]
                or configuration.get("max_ratio") != constraints["frame_slots"]
            ):
                raise ValidationError(
                    "TDM ratio constraints contract disagrees"
                )
    for label, path in {
        "routes_sha256": routes,
        "platform_sha256": platform_path.resolve(),
        "schedule_sha256": schedule,
        "phase5_report_sha256": phase5_report,
    }.items():
        if report.get(label) != _sha256(path):
            raise ValidationError(f"TDM checkpoint {label} seal is broken")
    if report.get("phase5") != phase5:
        raise ValidationError("TDM checkpoint embedded Phase 5 report disagrees")
    # The Phase 5 provider selects the optimization policy.  Academic ratio
    # providers materialize a schedule using a separate, explicitly recorded
    # schedule provider, so bind the checkpoint contract to the former when it
    # is present.  Baseline schedules have no optimization provider and retain
    # the historical direct provider contract.
    actual_provider = phase5.get(
        "optimization_provider", phase5.get("provider")
    )
    if expected_provider is not None and actual_provider != expected_provider:
        raise ValidationError("TDM checkpoint provider contract disagrees")
    checked = validate_phase5(
        routes, platform_path, schedule, ratio_plan_path=ratio_plan if ratio_plan.is_file() else None
    )
    return {"status": "pass", **checked}


def _link_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError as error:
        if error.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES}:
            raise
        shutil.copy2(source, destination)


def materialize_shared_phase1_5(
    frontend_root: Path,
    timing_root: Path,
    partition_root: Path,
    cut_timing_root: Path,
    route_root: Path,
    tdm_root: Path,
    platform_path: Path,
    output_dir: Path,
    *,
    timing_model_path: Path = DEFAULT_TIMING_MODEL,
    architecture_timing_db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    # ``experiment_partition`` imports the reusable timing validator from this
    # module, so import its Phase 3 validator lazily to avoid a module cycle.
    from .experiment_partition import validate_partition_checkpoint

    validate_frontend_checkpoint(frontend_root, platform_path)
    validate_timing_checkpoint(frontend_root, timing_root)
    validate_partition_checkpoint(frontend_root, timing_root, platform_path, partition_root)
    validate_cut_timing_checkpoint(
        frontend_root,
        timing_root,
        partition_root,
        cut_timing_root,
        timing_model_path=timing_model_path,
        architecture_timing_db_path=architecture_timing_db_path,
    )
    validate_route_checkpoint(partition_root, cut_timing_root, platform_path, route_root)
    validate_tdm_checkpoint(route_root, platform_path, tdm_root)
    output_dir = _prepare_empty_output(output_dir, "shared Phase 1-5 checkpoint")
    mapping = {
        "frontend/phase1/design.emuir.json": frontend_root / "phase1/design.emuir.json",
        "frontend/phase1/phase1_report.json": frontend_root / "phase1/phase1_report.json",
        "partition/clusters.json": partition_root / "clusters.json",
        "partition/assignment.json": partition_root / "assignment.json",
        "partition/phase3_report.json": partition_root / "phase3_report.json",
        "system-route/routes.json": route_root / "routes.json",
        "system-route/phase4_report.json": route_root / "phase4_report.json",
        "tdm/schedule.json": tdm_root / "schedule.json",
        "tdm/phase5_report.json": tdm_root / "phase5_report.json",
        "timing/path-database.json": timing_root / "path-database.json",
        "timing/partition-net-weights.json": timing_root / "partition-net-weights.json",
        "timing/cut-timing-paths.json": cut_timing_root / "cut-timing-paths.json",
        "timing/cut-segment-qualification.json": cut_timing_root / "cut-segment-qualification.json",
    }
    ratio_plan = tdm_root / "ratio_plan.json"
    if ratio_plan.is_file():
        mapping["tdm/ratio_plan.json"] = ratio_plan
    for relative, source in mapping.items():
        _link_or_copy(source, output_dir / relative)
    report = {
        "schema": EXPERIMENT_SHARED_SCHEMA,
        "status": "pass",
        "platform_sha256": _sha256(platform_path.resolve()),
        "artifacts": {
            relative: {"sha256": _sha256(output_dir / relative)}
            for relative in sorted(mapping)
        },
    }
    write_json(output_dir / "experiment-shared-report.json", report)
    validate_materialized_shared_phase1_5(output_dir, platform_path)
    return report


def validate_materialized_shared_phase1_5(
    root: Path, platform_path: Path
) -> Dict[str, Any]:
    report = read_json(_require(root, "experiment-shared-report.json"))
    if report.get("schema") != EXPERIMENT_SHARED_SCHEMA or report.get("status") != "pass":
        raise ValidationError("shared Phase 1-5 checkpoint report is invalid")
    if report.get("platform_sha256") != _sha256(platform_path.resolve()):
        raise ValidationError("shared Phase 1-5 platform seal is broken")
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValidationError("shared Phase 1-5 artifact table is invalid")
    for relative, record in artifacts.items():
        if not isinstance(record, dict) or record.get("sha256") != _sha256(_require(root, relative)):
            raise ValidationError(f"shared Phase 1-5 artifact seal is broken: {relative}")
    return validate_shared_phase1_5(root, platform_path)
