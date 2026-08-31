"""Compile the canonical real-RTL/contest-BoardDB Phase 1-7 experiment DAG."""

from __future__ import annotations

import copy
import hashlib
import math
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from .errors import ValidationError
from .experiment_storage import validate_experiment_write_path
from .benchmark import BenchmarkRun
from .contest_public import PUBLIC_CONTEST_BOARDDB_REPORT_SCHEMA
from .end_to_end_validation_matrix import load_end_to_end_validation_matrix
from .experiment_dag import (
    EXPERIMENT_SPEC_V2_SCHEMA,
    validate_experiment_spec,
)
from .experiment_identity import (
    build_implementation_closure,
    validate_implementation_closure,
)
from .io import read_json, write_json
from .partition import CUT_MODE_SEQUENTIAL_ONLY, CUT_MODE_STATIC_EXACT
from .combinational_cut import (
    STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
    STATIC_EXACT_CANDIDATE_FRONTIER_V1,
    STATIC_EXACT_CANDIDATE_POLICIES,
    STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH,
)
from .platform import Platform
from .mfspart_refine import (
    DEFAULT_BOTTLENECK_BETA,
    DEFAULT_TIMING_PATH_BETA,
)
from .routing import load_route_constraints
from .tdm import TDM_STATIC_EXACT_PROVIDER
from .tdm_ratio import TDM_TIMING_DAG_RATIO_PROVIDER
from .timing_routing import (
    GLOBAL_CANDIDATE_PROVIDER,
    NATIVE_TIMING_EVALUATED_PROVIDER,
)


CANONICAL_EXPERIMENT_CONFIG_SCHEMA = "emuflow.canonical-experiment-config/v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"canonical experiment {label} must be a file path")
    path = Path(value).expanduser().resolve()
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"canonical experiment {label} is not a regular file")
    return path


def _directory(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"canonical experiment {label} must be a directory path")
    path = Path(value).expanduser().resolve()
    if path.is_symlink() or not path.is_dir():
        raise ValidationError(f"canonical experiment {label} is not a directory")
    return path


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"canonical experiment {label} must be positive")
    return value


def _append_option(command: list[str], option: str, value: Any) -> None:
    if value is not None:
        command.extend((option, str(value)))


def _identity_argv(
    arguments: Sequence[str], bindings: Mapping[str, str]
) -> list[str]:
    reverse: Dict[str, str] = {}
    for label, value in bindings.items():
        if value not in reverse or label < reverse[value]:
            reverse[value] = label
    return [
        f"{{input:{reverse[argument]}}}" if argument in reverse else argument
        for argument in arguments
    ]


def _canonical_case_contract(
    repository_root: Path,
    case_id: str,
    rtl: Path,
    platform: Path,
    route_constraints_path: Path,
    boarddb_report_path: Path,
    top: str,
    clocks: Sequence[str],
    clock_periods: Mapping[str, float],
) -> Dict[str, Any]:
    matrix_path = repository_root / "benchmarks/end_to_end_validation_matrix.json"
    matrix, matrix_validation = load_end_to_end_validation_matrix(matrix_path)
    records = [record for record in matrix["cases"] if record["id"] == case_id]
    if len(records) != 1:
        raise ValidationError(
            "canonical experiment case_id is absent from the end-to-end matrix"
        )
    record = records[0]
    run_spec_path = repository_root / record["workload"]["run_spec"]
    run_spec = BenchmarkRun.load(run_spec_path).value
    if top != run_spec["top"] or list(clocks) != run_spec["clocks"]:
        raise ValidationError(
            "canonical experiment top/clocks do not match the matrix run spec"
        )
    expected_periods = run_spec.get("clock_periods_ns")
    if expected_periods is None or {
        name: float(value) for name, value in expected_periods.items()
    } != dict(clock_periods):
        raise ValidationError(
            "canonical experiment clock periods do not match the matrix run spec"
        )
    source_names = {Path(value).name for value in run_spec["sources"]}
    if rtl.name not in source_names:
        raise ValidationError(
            "canonical experiment RTL does not match a matrix run-spec source"
        )

    contest_case_id = record["platform"]["contest_case_id"]
    boarddb_report = read_json(boarddb_report_path)
    if (
        boarddb_report.get("schema") != PUBLIC_CONTEST_BOARDDB_REPORT_SCHEMA
        or boarddb_report.get("status") != "pass"
        or boarddb_report.get("case_id") != contest_case_id
        or boarddb_report.get("gate") != "materialize-boarddb"
        or boarddb_report.get("qualification")
        != "academic-architecture-projection"
    ):
        raise ValidationError(
            "canonical experiment BoardDB report is not the matrix contest case"
        )
    contest_matrix_path = repository_root / record["platform"]["contest_matrix"]
    from .contest_validation_matrix import load_contest_validation_matrix

    _, contest_validation = load_contest_validation_matrix(contest_matrix_path)
    if boarddb_report.get("matrix_sha256") != contest_validation["matrix_sha256"]:
        raise ValidationError(
            "canonical experiment BoardDB report contest matrix is stale"
        )
    artifacts = boarddb_report.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValidationError("canonical experiment BoardDB artifact seal is missing")
    for relative, path, label in (
        ("boarddb.json", platform, "platform"),
        ("route_constraints.json", route_constraints_path, "route constraints"),
    ):
        matches = [
            item
            for item in artifacts
            if isinstance(item, dict) and item.get("path") == relative
        ]
        if (
            len(matches) != 1
            or matches[0].get("sha256") != _sha256(path)
            or matches[0].get("bytes") != path.stat().st_size
        ):
            raise ValidationError(
                f"canonical experiment {label} bytes do not match the BoardDB report"
            )
    platform_document = read_json(platform)
    if (
        platform_document.get("schema") != "emuflow.boarddb/v1"
        or platform_document.get("platform", {}).get("name")
        != contest_case_id.replace(".", "-") + "-rtl"
    ):
        raise ValidationError(
            "canonical experiment platform is not the named contest projection"
        )
    normalized_route_constraints = load_route_constraints(
        route_constraints_path, Platform.load(platform)
    )
    return {
        "matrix_path": matrix_path,
        "matrix_sha256": matrix_validation["matrix_sha256"],
        "run_spec_path": run_spec_path,
        "contest_case_id": contest_case_id,
        "boarddb_report": boarddb_report,
        "route_constraints": normalized_route_constraints,
        "physical_mapping_profile": run_spec["physical_mapping_profile"],
    }


_COMPONENTS: Dict[str, Sequence[str]] = {
    "frontend": (
        "src/emuflow/experiment_upstream.py::run_frontend_checkpoint,validate_frontend_checkpoint",
        "src/emuflow/phase1.py",
        "src/emuflow/ir.py",
        "src/emuflow/platform.py",
        "src/emuflow/resources.py",
        "src/emuflow/yosys.py",
        "src/emuflow/synthesis.py",
        "src/emuflow/vpr.py",
        "src/emuflow/vtr_netlist.py",
        "scripts/yosys",
    ),
    "timing": (
        "src/emuflow/experiment_upstream.py::run_timing_checkpoint,validate_timing_checkpoint",
        "src/emuflow/opensta.py",
        "src/emuflow/sta.py",
        "src/emuflow/ir.py",
        "scripts/opensta",
    ),
    "partition": (
        "src/emuflow/experiment_upstream.py::validate_timing_checkpoint",
        "src/emuflow/experiment_partition.py",
        "src/emuflow/phase3.py",
        "src/emuflow/partition.py",
        "src/emuflow/combinational_cut.py",
        "src/emuflow/partition_hops.py",
        "src/emuflow/mfspart.py",
        "src/emuflow/mfspart_initial.py",
        "src/emuflow/mfspart_refine.py",
        "src/emuflow/mfspart_provider.py",
        "src/emuflow/tritonpart.py",
        "src/emuflow/routing.py",
        "src/native/hop_partition_refiner.cpp",
        "src/native/mfspart_refiner.cpp",
        "src/native/mfspart_refiner_checker.cpp",
    ),
    "cut-timing": (
        "src/emuflow/experiment_upstream.py::run_cut_timing_checkpoint,validate_cut_timing_checkpoint",
        "src/emuflow/opensta.py",
        "src/emuflow/sta.py",
        "scripts/opensta",
    ),
    "route": (
        "src/emuflow/experiment_upstream.py::run_route_checkpoint,validate_route_checkpoint",
        "src/emuflow/phase4.py",
        "src/emuflow/routing.py",
        "src/emuflow/combinational_cut.py",
        "src/emuflow/timing_routing.py",
        "src/emuflow/routing_candidates.py",
        "src/emuflow/routing_batches.py",
        "src/emuflow/tdm_feedback.py",
        "src/emuflow/physical_route_feedback.py",
        "src/native/tlr_router.cpp",
    ),
    "tdm": (
        "src/emuflow/experiment_upstream.py::run_tdm_checkpoint,validate_tdm_checkpoint",
        "src/emuflow/phase5.py",
        "src/emuflow/tdm.py",
        "src/emuflow/combinational_cut.py",
        "src/emuflow/tdm_ratio.py",
        "src/emuflow/tdm_timing_dag.py",
        "src/emuflow/tdm_slot.py",
        "src/emuflow/tdm_compatibility.py",
        "src/emuflow/tdm_cp_sat.py",
        "src/emuflow/tdm_feedback.py",
        "src/native/tdm_ratio_optimizer.cpp",
        "src/native/tdm_timing_dag_optimizer.cpp",
        "src/native/tdm_slot_optimizer.cpp",
    ),
    "shared": (
        "src/emuflow/experiment_upstream.py::materialize_shared_phase1_5,validate_materialized_shared_phase1_5",
        "src/emuflow/experiment_stages.py",
        "src/emuflow/phase3.py",
        "src/emuflow/phase4.py",
        "src/emuflow/phase5.py",
        "src/emuflow/combinational_cut.py",
    ),
    "phase6": (
        "src/emuflow/experiment_stages.py",
        "src/emuflow/phase6.py",
        "src/emuflow/netlist.py",
        "src/emuflow/runtime.py",
        "src/emuflow/equivalence.py",
        "src/emuflow/combinational_cut.py",
        "src/emuflow/routing.py",
        "src/emuflow/tdm.py",
        "src/emuflow/pin_planning.py",
        "src/emuflow/chimew_pipeline.py",
        "src/emuflow/chimew_phase6.py",
        "src/emuflow/chimew_qualification.py",
        "rtl/transport",
        "src/native/placement_aware_pin_planner.cpp",
        "src/native/chimew_bank_channel_assigner.cpp",
        "src/native/chimew_position_refiner.cpp",
        "src/native/chimew_rudy.cpp",
        "src/native/chimew_signal_grouper.cpp",
    ),
    "lookahead": (
        "src/emuflow/experiment_stages.py",
        "src/emuflow/academic_chimew.py",
        "src/emuflow/chimew_bank_channel.py",
        "src/emuflow/chimew_grouping.py",
        "src/emuflow/chimew_phase6.py",
        "src/emuflow/chimew_qualification.py",
        "src/emuflow/chimew_refinement.py",
        "src/emuflow/chimew_rudy.py",
        "src/emuflow/multi_fpga_physical_flow.py",
        "src/emuflow/physical_backend.py",
        "src/emuflow/openparf.py",
        "src/emuflow/packed_placement.py",
        "src/emuflow/vpr.py",
        "scripts/openparf",
        "src/native/chimew_position_refiner.cpp",
        "src/native/chimew_signal_grouper.cpp",
        "src/native/vtr_architecture_importer.cpp",
        "src/native/vpr_packed_netlist_importer.cpp",
        "src/native/vpr_route_checker.cpp",
    ),
    "phase7": (
        "src/emuflow/experiment_stages.py",
        "src/emuflow/multi_fpga_physical_flow.py",
        "src/emuflow/physical_backend.py",
        "src/emuflow/openparf.py",
        "src/emuflow/packed_placement.py",
        "src/emuflow/phase7c.py",
        "src/emuflow/system_timing.py",
        "src/emuflow/local_path_timing.py",
        "src/emuflow/logic_segment_timing.py",
        "src/emuflow/static_exact_timing.py",
        "src/emuflow/combinational_cut.py",
        "src/emuflow/vpr.py",
        "scripts/openparf",
        "src/native/vtr_architecture_importer.cpp",
        "src/native/vpr_packed_netlist_importer.cpp",
        "src/native/vpr_route_checker.cpp",
    ),
    "qor-compare": (
        "src/emuflow/canonical_qor.py",
        "src/emuflow/experiment_stages.py",
        "src/emuflow/multi_fpga_physical_flow.py",
        "src/emuflow/runtime.py",
        "src/emuflow/system_timing.py",
        "src/emuflow/static_exact_timing.py",
    ),
    "static-exact-qor-compare": (
        "src/emuflow/static_exact_qor.py",
        "src/emuflow/canonical_qor.py",
        "src/emuflow/experiment_stages.py",
        "src/emuflow/multi_fpga_physical_flow.py",
        "src/emuflow/runtime.py",
        "src/emuflow/system_timing.py",
        "src/emuflow/static_exact_timing.py",
        "src/emuflow/combinational_cut.py",
    ),
}


def _closure(repository_root: Path, stage: str) -> Dict[str, Any]:
    return build_implementation_closure(repository_root, _COMPONENTS[stage])


def _artifact(path: str, role: str) -> Dict[str, str]:
    retention = {
        "consumer-checkpoint": "required",
        "source-input": "required",
        "evidence-critical": "required",
        "diagnostic": "optional",
    }[role]
    return {"path": path, "role": role, "retention": retention}


def compile_canonical_experiment_spec(
    config_path: Path, repository_root: Path, output_path: Path
) -> Dict[str, Any]:
    output_path = validate_experiment_write_path(output_path)
    config = read_json(config_path)
    if config.get("schema") != CANONICAL_EXPERIMENT_CONFIG_SCHEMA:
        raise ValidationError("canonical experiment config schema is invalid")
    repository_root = _directory(str(repository_root), "repository_root")
    case_id = config.get("case_id")
    source_commit = config.get("source_commit")
    if not isinstance(case_id, str) or not case_id:
        raise ValidationError("canonical experiment case_id is invalid")
    if not isinstance(source_commit, str) or len(source_commit) != 40:
        raise ValidationError("canonical experiment source_commit is invalid")
    rtl = _file(config.get("rtl_source"), "rtl_source")
    platform = _file(config.get("platform"), "platform")
    boarddb_report_path = _file(
        config.get("boarddb_report"), "boarddb_report"
    )
    route_constraints = _file(
        config.get("route_constraints"), "route_constraints"
    )
    partition_constraints_value = config.get("partition_constraints")
    partition_constraints = (
        _file(partition_constraints_value, "partition_constraints")
        if partition_constraints_value is not None
        else None
    )
    tritonpart_solution_value = config.get("tritonpart_solution")
    tritonpart_solution = (
        _file(tritonpart_solution_value, "tritonpart_solution")
        if tritonpart_solution_value is not None
        else None
    )
    timing_model = _file(config.get("timing_model"), "timing_model")
    architecture_timing = _file(
        config.get("architecture_timing_db"), "architecture_timing_db"
    )
    physical_architecture = _file(
        config.get("physical_architecture"), "physical_architecture"
    )
    tools_raw = config.get("tools")
    if not isinstance(tools_raw, dict):
        raise ValidationError("canonical experiment tools must be an object")
    required_tools = {
        "emuflow",
        "yosys",
        "opensta",
        "openroad",
        "hop_refiner",
        "router",
        "ratio_optimizer",
        "timing_dag_optimizer",
        "slot_optimizer",
        "pin_planner",
        "chimew_grouper",
        "chimew_refiner",
        "chimew_rudy",
        "chimew_assigner",
        "vpr",
        "architecture_importer",
        "packed_importer",
        "route_checker",
        "openparf_python",
    }
    if set(tools_raw) != required_tools:
        raise ValidationError(
            "canonical experiment tools must exactly cover " + ", ".join(sorted(required_tools))
        )
    tools = {label: _file(value, f"tool {label}") for label, value in tools_raw.items()}
    openparf_install = _directory(config.get("openparf_install"), "openparf_install")
    openparf_manifest = _file(config.get("openparf_manifest"), "openparf_manifest")
    openparf_closure = validate_implementation_closure(
        read_json(openparf_manifest), root=openparf_install
    )
    if openparf_closure["components"] != ["openparf", "openparf.py"]:
        raise ValidationError(
            "canonical experiment OpenPARF manifest must seal openparf.py and "
            "the complete openparf package"
        )
    top = config.get("top")
    if not isinstance(top, str) or not top:
        raise ValidationError("canonical experiment top is invalid")
    clocks = config.get("clocks")
    periods = config.get("clock_periods")
    if (
        not isinstance(clocks, list)
        or not clocks
        or not all(isinstance(item, str) and item for item in clocks)
        or len(clocks) != len(set(clocks))
        or not isinstance(periods, dict)
        or set(periods) != set(clocks)
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
            for value in periods.values()
        )
    ):
        raise ValidationError("canonical experiment clocks/periods are invalid")
    cut_mode = config.get("cut_mode", CUT_MODE_SEQUENTIAL_ONLY)
    if cut_mode not in {CUT_MODE_SEQUENTIAL_ONLY, CUT_MODE_STATIC_EXACT}:
        raise ValidationError("canonical experiment cut_mode is invalid")
    if cut_mode == CUT_MODE_STATIC_EXACT and len(clocks) != 1:
        raise ValidationError(
            "canonical static exact cut requires exactly one virtual DUT clock"
        )
    contract = _canonical_case_contract(
        repository_root,
        case_id,
        rtl,
        platform,
        route_constraints,
        boarddb_report_path,
        top,
        clocks,
        {name: float(periods[name]) for name in clocks},
    )
    workers = _positive_integer(config.get("physical_workers", 8), "physical_workers")
    physical_peak_gib = _positive_integer(
        config.get("physical_peak_gib", 48), "physical_peak_gib"
    )
    partition_peak_gib = _positive_integer(
        config.get("partition_peak_gib", 24), "partition_peak_gib"
    )
    partition_retained_gib = _positive_integer(
        config.get("partition_retained_gib", 6), "partition_retained_gib"
    )
    route_candidate_workers = _positive_integer(
        config.get("route_candidate_workers", workers),
        "route_candidate_workers",
    )
    channel_width = _positive_integer(
        config.get("physical_route_channel_width", 300),
        "physical_route_channel_width",
    )
    region_count = _positive_integer(config.get("region_count", 4), "region_count")
    physical_seeds_value = config.get("physical_seeds", [1])
    if (
        not isinstance(physical_seeds_value, list)
        or not physical_seeds_value
        or any(
            isinstance(seed, bool) or not isinstance(seed, int) or seed < 1
            for seed in physical_seeds_value
        )
        or physical_seeds_value != sorted(set(physical_seeds_value))
    ):
        raise ValidationError(
            "canonical experiment physical_seeds must be a sorted, unique "
            "non-empty list of positive integers"
        )
    physical_seeds = tuple(physical_seeds_value)
    partition_seed = config.get("partition_seed", 0)
    if isinstance(partition_seed, bool) or not isinstance(partition_seed, int) or partition_seed < 0:
        raise ValidationError("canonical experiment partition_seed is invalid")
    partition_seed_attempts = _positive_integer(
        config.get("partition_seed_attempts", 1),
        "partition_seed_attempts",
    )
    if tritonpart_solution is not None and partition_seed_attempts != 1:
        raise ValidationError(
            "canonical experiment tritonpart_solution requires "
            "partition_seed_attempts=1"
        )
    partition_repair_balance = config.get(
        "partition_repair_balance", False
    )
    if not isinstance(partition_repair_balance, bool):
        raise ValidationError(
            "canonical experiment partition_repair_balance must be boolean"
        )
    mfspart_post_refinement = config.get(
        "mfspart_post_refinement", cut_mode == CUT_MODE_STATIC_EXACT
    )
    if not isinstance(mfspart_post_refinement, bool):
        raise ValidationError(
            "canonical experiment mfspart_post_refinement must be boolean"
        )
    mfspart_post_refinement_early_stop = _positive_integer(
        config.get("mfspart_post_refinement_early_stop", 1000),
        "mfspart_post_refinement_early_stop",
    )
    mfspart_post_refinement_bottleneck_beta = config.get(
        "mfspart_post_refinement_bottleneck_beta",
        DEFAULT_BOTTLENECK_BETA,
    )
    if (
        isinstance(mfspart_post_refinement_bottleneck_beta, bool)
        or not isinstance(
            mfspart_post_refinement_bottleneck_beta, (int, float)
        )
        or not math.isfinite(mfspart_post_refinement_bottleneck_beta)
        or mfspart_post_refinement_bottleneck_beta < 0
    ):
        raise ValidationError(
            "canonical experiment mfspart_post_refinement_bottleneck_beta "
            "must be a finite non-negative number"
        )
    mfspart_post_refinement_bottleneck_beta = float(
        mfspart_post_refinement_bottleneck_beta
    )
    max_cross_fpga_dependency_depth = _positive_integer(
        config.get(
            "max_cross_fpga_dependency_depth",
            STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH,
        ),
        "max_cross_fpga_dependency_depth",
    )
    mfspart_post_refinement_timing_path_beta = config.get(
        "mfspart_post_refinement_timing_path_beta",
        DEFAULT_TIMING_PATH_BETA,
    )
    if (
        isinstance(mfspart_post_refinement_timing_path_beta, bool)
        or not isinstance(
            mfspart_post_refinement_timing_path_beta, (int, float)
        )
        or not math.isfinite(mfspart_post_refinement_timing_path_beta)
        or mfspart_post_refinement_timing_path_beta < 0
    ):
        raise ValidationError(
            "canonical experiment mfspart_post_refinement_timing_path_beta "
            "must be a finite non-negative number"
        )
    mfspart_post_refinement_timing_path_beta = float(
        mfspart_post_refinement_timing_path_beta
    )
    comb_segment_budget_slots = _positive_integer(
        config.get("comb_segment_budget_slots", 1),
        "comb_segment_budget_slots",
    )
    static_exact_candidate_policy = config.get(
        "static_exact_candidate_policy",
        STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    )
    if static_exact_candidate_policy not in STATIC_EXACT_CANDIDATE_POLICIES:
        raise ValidationError(
            "canonical experiment static_exact_candidate_policy is invalid"
        )
    minimum_combinational_cut_nets = config.get(
        "minimum_combinational_cut_nets",
        0,
    )
    if (
        isinstance(minimum_combinational_cut_nets, bool)
        or not isinstance(minimum_combinational_cut_nets, int)
        or minimum_combinational_cut_nets < 0
        or (
            cut_mode != CUT_MODE_STATIC_EXACT
            and minimum_combinational_cut_nets != 0
        )
    ):
        raise ValidationError(
            "canonical experiment minimum_combinational_cut_nets must be "
            "non-negative for static exact mode and zero otherwise"
        )
    if (
        cut_mode == CUT_MODE_STATIC_EXACT
        and static_exact_candidate_policy == STATIC_EXACT_CANDIDATE_FRONTIER_V1
        and max_cross_fpga_dependency_depth not in {1, 2}
    ):
        raise ValidationError(
            "canonical legacy static exact policy requires "
            "max_cross_fpga_dependency_depth to be 1 or 2"
        )
    executable = str(tools["emuflow"])
    base_inputs = {
        "rtl": _sha256(rtl),
        "platform": _sha256(platform),
        "boarddb_report": _sha256(boarddb_report_path),
        "route_constraints": _sha256(route_constraints),
        "end_to_end_matrix": contract["matrix_sha256"],
        "benchmark_run_spec": _sha256(contract["run_spec_path"]),
        "timing_model": _sha256(timing_model),
        "architecture_timing_db": _sha256(architecture_timing),
        "physical_architecture": _sha256(physical_architecture),
        "openparf_manifest": _sha256(openparf_manifest),
        "openparf_implementation": openparf_closure["implementation_sha256"],
        **{f"tool.{label}": _sha256(path) for label, path in sorted(tools.items())},
    }
    if partition_constraints is not None:
        base_inputs["partition_constraints"] = _sha256(partition_constraints)
    if tritonpart_solution is not None:
        base_inputs["tritonpart_solution"] = _sha256(tritonpart_solution)
    base_bindings = {
        "rtl": str(rtl),
        "platform": str(platform),
        "boarddb_report": str(boarddb_report_path),
        "route_constraints": str(route_constraints),
        "end_to_end_matrix": str(contract["matrix_path"]),
        "benchmark_run_spec": str(contract["run_spec_path"]),
        "timing_model": str(timing_model),
        "architecture_timing_db": str(architecture_timing),
        "physical_architecture": str(physical_architecture),
        "openparf_manifest": str(openparf_manifest),
        "openparf_implementation": str(openparf_install),
        **{f"tool.{label}": str(path) for label, path in sorted(tools.items())},
    }
    if partition_constraints is not None:
        base_bindings["partition_constraints"] = str(partition_constraints)
    if tritonpart_solution is not None:
        base_bindings["tritonpart_solution"] = str(tritonpart_solution)
    closures = {stage: _closure(repository_root, stage) for stage in _COMPONENTS}

    nodes: list[Dict[str, Any]] = []

    def node(
        node_id: str,
        stage: str,
        dependencies: Sequence[str],
        command: list[str],
        validator: list[str],
        artifacts: list[Dict[str, str]],
        *,
        inputs: Sequence[str] = (),
        configuration: Mapping[str, Any] | None = None,
        peak_gib: int,
        retained_gib: int,
        provider: str | None = None,
        physical_seed: int | None = None,
    ) -> None:
        selected_inputs = {label: base_inputs[label] for label in sorted(inputs)}
        runtime_values = set(command) | set(validator)
        execution_bindings = {
            label: base_bindings[label]
            for label in sorted(inputs)
            if base_bindings[label] in runtime_values
        }
        record: Dict[str, Any] = {
            "id": node_id,
            "stage": stage,
            "dependencies": list(dependencies),
            "inputs": selected_inputs,
            "configuration": dict(configuration or {}),
            "implementation": closures[stage],
            "command": command,
            "execution_bindings": execution_bindings,
            "command_identity": _identity_argv(command, execution_bindings),
            "validator_implementation": closures[stage],
            "validator": validator,
            "validator_identity": _identity_argv(
                validator, execution_bindings
            ),
            "environment": {"EMUFLOW_EXPERIMENT_POLICY": "canonical-real-rtl-v1"},
            "storage_estimate": {
                "peak_bytes": peak_gib * 1024**3,
                "retained_bytes": retained_gib * 1024**3,
            },
            "artifacts": artifacts,
        }
        if provider is not None:
            record["provider"] = provider
        if physical_seed is not None:
            record["physical_seed"] = physical_seed
        nodes.append(record)

    frontend_command = [
        executable, "experiment-stage", "frontend-run",
        "--platform", str(platform), "--source", str(rtl), "--top", top,
        "--mapping-profile", contract["physical_mapping_profile"],
        "--yosys", str(tools["yosys"]),
        "--managed-dag-node",
    ]
    for clock in clocks:
        frontend_command.extend(("--clock", clock))
    frontend_command.extend(("--out", "{output_dir}"))
    node(
        "frontend", "frontend", [], frontend_command,
        [executable, "experiment-stage", "frontend-validate", "{artifact_root}", "--platform", str(platform), "--managed-dag-node"],
        [_artifact("sources", "source-input"), _artifact("phase1", "consumer-checkpoint"), _artifact("synthesized.json", "consumer-checkpoint"), _artifact("experiment-frontend-report.json", "evidence-critical")],
        inputs=("rtl", "platform", "boarddb_report", "end_to_end_matrix", "benchmark_run_spec", "tool.emuflow", "tool.yosys"),
        configuration={"case_id": case_id, "contest_case_id": contract["contest_case_id"], "top": top, "clocks": clocks, "mapping_profile": contract["physical_mapping_profile"], "require_no_fabric_clock": True},
        peak_gib=16, retained_gib=4,
    )
    period_args = [f"{clock}={float(periods[clock]):.12g}" for clock in clocks]
    timing_command = [
        executable, "experiment-stage", "timing-run", "--frontend", "{dependency:frontend}",
        "--timing-model", str(timing_model), "--architecture-timing-db", str(architecture_timing),
        "--opensta", str(tools["opensta"]),
        "--managed-dag-node",
    ]
    for value in period_args:
        timing_command.extend(("--clock-period", value))
    timing_command.extend(("--out", "{output_dir}"))
    node(
        "timing", "timing", ["frontend"], timing_command,
        [executable, "experiment-stage", "timing-validate", "{artifact_root}", "--frontend", "{dependency:frontend}", "--managed-dag-node"],
        [_artifact("path-database.json", "consumer-checkpoint"), _artifact("partition-net-weights.json", "consumer-checkpoint"), _artifact("experiment-timing-report.json", "evidence-critical")],
        inputs=("timing_model", "architecture_timing_db", "tool.emuflow", "tool.opensta"),
        configuration={"clock_periods": periods, "max_paths": 200000, "criticality_scale": 9.0, "criticality_exponent": 2.0},
        peak_gib=16, retained_gib=4,
    )
    partition_command = [
        executable, "experiment-stage", "partition-run", "--frontend", "{dependency:frontend}",
        "--timing", "{dependency:timing}", "--platform", str(platform),
        "--provider", "tritonpart", "--seed", str(partition_seed),
        "--seed-attempts", str(partition_seed_attempts),
        "--cut-mode", cut_mode,
        "--max-cross-fpga-dependency-depth",
        str(max_cross_fpga_dependency_depth),
        "--comb-segment-budget-slots", str(comb_segment_budget_slots),
        "--static-exact-candidate-policy", static_exact_candidate_policy,
        "--minimum-combinational-cut-nets",
        str(minimum_combinational_cut_nets),
        "--managed-dag-node",
        "--route-constraints", str(route_constraints), "--openroad", str(tools["openroad"]), "--hop-refiner", str(tools["hop_refiner"]),
        "--out", "{output_dir}",
    ]
    partition_validator = [
        executable, "experiment-stage", "partition-validate", "{artifact_root}",
        "--frontend", "{dependency:frontend}", "--timing", "{dependency:timing}",
        "--platform", str(platform), "--route-constraints", str(route_constraints),
        "--provider", "tritonpart", "--seed", str(partition_seed),
        "--seed-attempts", str(partition_seed_attempts),
        "--cut-mode", cut_mode,
        "--max-cross-fpga-dependency-depth",
        str(max_cross_fpga_dependency_depth),
        "--comb-segment-budget-slots", str(comb_segment_budget_slots),
        "--static-exact-candidate-policy", static_exact_candidate_policy,
        "--minimum-combinational-cut-nets",
        str(minimum_combinational_cut_nets),
        "--online-validation",
    ]
    if mfspart_post_refinement:
        partition_command.insert(-2, "--mfspart-post-refinement")
        partition_command[-2:-2] = [
            "--mfspart-post-refinement-early-stop",
            str(mfspart_post_refinement_early_stop),
            "--mfspart-post-refinement-bottleneck-beta",
            format(mfspart_post_refinement_bottleneck_beta, ".17g"),
            "--mfspart-post-refinement-timing-path-beta",
            format(mfspart_post_refinement_timing_path_beta, ".17g"),
        ]
        partition_validator.extend(
            (
                "--mfspart-post-refinement",
                "--mfspart-post-refinement-early-stop",
                str(mfspart_post_refinement_early_stop),
                "--mfspart-post-refinement-bottleneck-beta",
                format(mfspart_post_refinement_bottleneck_beta, ".17g"),
                "--mfspart-post-refinement-timing-path-beta",
                format(mfspart_post_refinement_timing_path_beta, ".17g"),
            )
        )
    else:
        partition_validator.append("--no-mfspart-post-refinement")
    if partition_constraints is not None:
        partition_command.extend(("--constraints", str(partition_constraints)))
        partition_validator.extend(("--constraints", str(partition_constraints)))
    if tritonpart_solution is not None:
        partition_command.extend(
            ("--tritonpart-solution", str(tritonpart_solution))
        )
        partition_validator.extend(
            ("--tritonpart-solution", str(tritonpart_solution))
        )
    if partition_repair_balance:
        partition_command.insert(-2, "--repair-balance")
    partition_validator.append(
        "--repair-balance"
        if partition_repair_balance
        else "--no-repair-balance"
    )
    node(
        "partition", "partition", ["frontend", "timing"], partition_command,
        partition_validator,
        [_artifact("clusters.json", "consumer-checkpoint"), _artifact("constraints.normalized.json", "consumer-checkpoint"), _artifact("assignment.json", "consumer-checkpoint"), _artifact("phase3_report.json", "consumer-checkpoint"), *([_artifact("mfspart-post-refinement", "consumer-checkpoint")] if mfspart_post_refinement else []), _artifact("experiment-partition-report.json", "evidence-critical")],
        inputs=("platform", "route_constraints", *( ("partition_constraints",) if partition_constraints is not None else () ), *( ("tritonpart_solution",) if tritonpart_solution is not None else () ), "tool.emuflow", "tool.openroad", "tool.hop_refiner"),
        configuration={"provider": "tritonpart", "seed": partition_seed, "seed_attempts": partition_seed_attempts, "repair_balance": partition_repair_balance, "mfspart_post_refinement": mfspart_post_refinement, "mfspart_post_refinement_early_stop": mfspart_post_refinement_early_stop, "mfspart_post_refinement_bottleneck_beta": mfspart_post_refinement_bottleneck_beta, "mfspart_post_refinement_timing_path_beta": mfspart_post_refinement_timing_path_beta, "route_constraints": contract["route_constraints"], "partition_constraints_sha256": base_inputs.get("partition_constraints"), "tritonpart_solution_sha256": base_inputs.get("tritonpart_solution"), "timeout_seconds": 3600, "num_initial_solutions": 50, "num_best_initial_solutions": 10, "cut_mode": cut_mode, "max_cross_fpga_dependency_depth": max_cross_fpga_dependency_depth, "comb_segment_budget_slots": comb_segment_budget_slots, "static_exact_candidate_policy": static_exact_candidate_policy, "minimum_combinational_cut_nets": minimum_combinational_cut_nets, "partition_peak_gib": partition_peak_gib, "partition_retained_gib": partition_retained_gib},
        peak_gib=partition_peak_gib, retained_gib=partition_retained_gib,
    )
    cut_command = [
        executable, "experiment-stage", "cut-timing-run", "--frontend", "{dependency:frontend}",
        "--timing", "{dependency:timing}", "--partition", "{dependency:partition}",
        "--timing-model", str(timing_model), "--architecture-timing-db", str(architecture_timing),
        "--managed-dag-node",
    ]
    for value in period_args:
        cut_command.extend(("--clock-period", value))
    cut_command.extend(("--out", "{output_dir}"))
    node(
        "cut-timing", "cut-timing", ["frontend", "timing", "partition"], cut_command,
        [executable, "experiment-stage", "cut-timing-validate", "{artifact_root}", "--frontend", "{dependency:frontend}", "--timing", "{dependency:timing}", "--partition", "{dependency:partition}", "--timing-model", str(timing_model), "--architecture-timing-db", str(architecture_timing), "--managed-dag-node"],
        [_artifact("cut-timing-paths.json", "consumer-checkpoint"), _artifact("cut-segment-qualification.json", "evidence-critical"), _artifact("experiment-cut-timing-report.json", "evidence-critical")],
        inputs=("timing_model", "architecture_timing_db", "tool.emuflow"),
        configuration={"clock_periods": periods},
        peak_gib=4, retained_gib=1,
    )
    route_provider = (
        NATIVE_TIMING_EVALUATED_PROVIDER
        if cut_mode == CUT_MODE_STATIC_EXACT
        else GLOBAL_CANDIDATE_PROVIDER
    )
    effective_candidate_workers = (
        1 if cut_mode == CUT_MODE_STATIC_EXACT else route_candidate_workers
    )
    node(
        "route", "route", ["partition", "cut-timing"],
        [executable, "experiment-stage", "route-run", "--partition", "{dependency:partition}", "--cut-timing", "{dependency:cut-timing}", "--platform", str(platform), "--constraints", str(route_constraints), "--provider", route_provider, "--candidate-workers", str(effective_candidate_workers), "--router", str(tools["router"]), "--managed-storage", "--managed-dag-node", "--out", "{output_dir}"],
        [executable, "experiment-stage", "route-validate", "{artifact_root}", "--partition", "{dependency:partition}", "--cut-timing", "{dependency:cut-timing}", "--platform", str(platform), "--constraints", str(route_constraints), "--provider", route_provider, "--candidate-workers", str(effective_candidate_workers), "--managed-dag-node"],
        [_artifact("routes.json", "consumer-checkpoint"), _artifact("phase4_report.json", "consumer-checkpoint"), _artifact("experiment-route-report.json", "evidence-critical")],
        inputs=("platform", "route_constraints", "tool.emuflow", "tool.router"),
        configuration={"provider": route_provider, "candidate_workers": effective_candidate_workers, "route_constraints": contract["route_constraints"], "cut_mode": cut_mode},
        peak_gib=12, retained_gib=3,
    )
    tdm_provider = (
        TDM_STATIC_EXACT_PROVIDER
        if cut_mode == CUT_MODE_STATIC_EXACT
        else TDM_TIMING_DAG_RATIO_PROVIDER
    )
    tdm_command = [
        executable, "experiment-stage", "tdm-run", "--route",
        "{dependency:route}", "--platform", str(platform), "--provider",
        tdm_provider,
        "--managed-storage",
        "--managed-dag-node",
    ]
    tdm_inputs = ["platform", "route_constraints", "tool.emuflow"]
    tdm_artifacts = [
        _artifact("schedule.json", "consumer-checkpoint"),
        _artifact("phase5_report.json", "consumer-checkpoint"),
        _artifact("experiment-tdm-report.json", "evidence-critical"),
    ]
    if cut_mode != CUT_MODE_STATIC_EXACT:
        tdm_command.extend((
            "--ratio-quantum", str(contract["route_constraints"]["tdm_ratio_quantum"]),
            "--max-ratio", str(contract["route_constraints"]["frame_slots"]),
            "--ratio-optimizer", str(tools["ratio_optimizer"]),
            "--timing-dag-optimizer", str(tools["timing_dag_optimizer"]),
            "--slot-optimizer", str(tools["slot_optimizer"]),
        ))
        tdm_inputs.extend((
            "tool.ratio_optimizer",
            "tool.timing_dag_optimizer",
            "tool.slot_optimizer",
        ))
        tdm_artifacts.insert(1, _artifact("ratio_plan.json", "consumer-checkpoint"))
    tdm_command.extend(("--out", "{output_dir}"))
    node(
        "tdm", "tdm", ["route"],
        tdm_command,
        [executable, "experiment-stage", "tdm-validate", "{artifact_root}", "--route", "{dependency:route}", "--platform", str(platform), "--constraints", str(route_constraints), "--provider", tdm_provider, "--managed-dag-node"],
        tdm_artifacts,
        inputs=tuple(tdm_inputs),
        configuration={"provider": tdm_provider, "simulation_frames": 16, "ratio_max_iterations": 500, "ratio_quantum": contract["route_constraints"]["tdm_ratio_quantum"], "max_ratio": contract["route_constraints"]["frame_slots"], "post_refinement_iterations": 200, "cut_mode": cut_mode},
        peak_gib=12, retained_gib=3,
    )
    shared_dependencies = ["frontend", "timing", "partition", "cut-timing", "route", "tdm"]
    node(
        "shared-phase1-5", "shared", shared_dependencies,
        [executable, "experiment-stage", "shared-materialize", "--frontend", "{dependency:frontend}", "--timing", "{dependency:timing}", "--partition", "{dependency:partition}", "--cut-timing", "{dependency:cut-timing}", "--route", "{dependency:route}", "--tdm", "{dependency:tdm}", "--platform", str(platform), "--timing-model", str(timing_model), "--architecture-timing-db", str(architecture_timing), "--managed-dag-node", "--out", "{output_dir}"],
        [executable, "experiment-stage", "shared-validate", "--shared", "{artifact_root}", "--platform", str(platform), "--managed-dag-node"],
        [_artifact("frontend", "consumer-checkpoint"), _artifact("timing", "consumer-checkpoint"), _artifact("partition", "consumer-checkpoint"), _artifact("system-route", "consumer-checkpoint"), _artifact("tdm", "consumer-checkpoint"), _artifact("experiment-shared-report.json", "evidence-critical")],
        inputs=("platform", "tool.emuflow"), configuration={"materialization": "same-filesystem-hardlink-or-copy"}, peak_gib=2, retained_gib=1,
    )

    baseline_command = [executable, "experiment-stage", "phase6-run", "--shared", "{dependency:shared-phase1-5}", "--platform", str(platform), "--provider", "baseline", "--managed-storage", "--managed-dag-node", "--out", "{output_dir}"]
    node(
        "phase6-baseline", "phase6", ["shared-phase1-5"], baseline_command,
        [executable, "experiment-stage", "phase6-validate", "{artifact_root}", "--shared", "{dependency:shared-phase1-5}", "--platform", str(platform), "--provider", "baseline", "--managed-dag-node"],
        [_artifact("split", "consumer-checkpoint"), _artifact("schedule.json", "consumer-checkpoint"), _artifact("experiment-phase6-report.json", "evidence-critical")],
        inputs=("platform", "tool.emuflow"), configuration={"provider": "baseline", "equivalence_cycles": 16}, peak_gib=12, retained_gib=4, provider="baseline",
    )
    lookahead_command = [
        executable, "experiment-stage", "lookahead-run", "--shared", "{dependency:shared-phase1-5}",
        "--baseline-phase6", "{dependency:phase6-baseline}", "--reuse-validated-phase6-equivalence", "--platform", str(platform),
        "--seed", "1", "--workers", str(workers), "--region-count", str(region_count),
        "--architecture", str(physical_architecture), "--yosys", str(tools["yosys"]), "--vpr", str(tools["vpr"]),
        "--architecture-importer", str(tools["architecture_importer"]), "--packed-importer", str(tools["packed_importer"]),
        "--route-checker", str(tools["route_checker"]), "--openparf-install", str(openparf_install),
        "--openparf-python", str(tools["openparf_python"]), "--route-channel-width", str(channel_width), "--out", "{output_dir}",
    ]
    lookahead_command.insert(-2, "--managed-dag-node")
    node(
        "physical-lookahead", "lookahead", ["shared-phase1-5", "phase6-baseline"], lookahead_command,
        [executable, "experiment-stage", "lookahead-validate", "{artifact_root}", "--shared", "{dependency:shared-phase1-5}", "--baseline-phase6", "{dependency:phase6-baseline}", "--reuse-validated-phase6-equivalence", "--platform", str(platform), "--seed", "1", "--workers", str(workers), "--region-count", str(region_count), "--architecture", str(physical_architecture), "--route-channel-width", str(channel_width), "--managed-dag-node"],
        [_artifact("physical", "consumer-checkpoint"), _artifact("lookahead", "consumer-checkpoint"), _artifact("experiment-lookahead-report.json", "evidence-critical")],
        inputs=("platform", "physical_architecture", "openparf_manifest", "openparf_implementation", "tool.emuflow", "tool.yosys", "tool.vpr", "tool.architecture_importer", "tool.packed_importer", "tool.route_checker", "tool.openparf_python"),
        configuration={"physical_seed": 1, "physical_workers": workers, "physical_peak_gib": physical_peak_gib, "region_count": region_count, "route_channel_width": channel_width}, peak_gib=physical_peak_gib, retained_gib=10,
    )
    phase6_research_providers = (
        ()
        if cut_mode == CUT_MODE_STATIC_EXACT
        else ("placement-aware", "chimew")
    )
    for provider in phase6_research_providers:
        phase6_id = f"phase6-{provider}"
        extra_artifacts = (
            [_artifact("placement-aware-position-hints.json", "consumer-checkpoint"), _artifact("placement-aware-pin-plan.json", "consumer-checkpoint")]
            if provider == "placement-aware"
            else [_artifact("chimew-pipeline", "consumer-checkpoint")]
        )
        phase6_command = [executable, "experiment-stage", "phase6-run", "--shared", "{dependency:shared-phase1-5}", "--lookahead", "{dependency:physical-lookahead}", "--platform", str(platform), "--provider", provider, "--managed-storage", "--managed-dag-node"]
        phase6_inputs = ["platform", "tool.emuflow"]
        if provider == "placement-aware":
            phase6_command.extend(("--pin-planner", str(tools["pin_planner"])))
            phase6_inputs.append("tool.pin_planner")
        else:
            for argument, label in (
                ("--chimew-grouper", "chimew_grouper"),
                ("--chimew-refiner", "chimew_refiner"),
                ("--chimew-rudy", "chimew_rudy"),
                ("--chimew-assigner", "chimew_assigner"),
            ):
                phase6_command.extend((argument, str(tools[label])))
                phase6_inputs.append(f"tool.{label}")
        phase6_command.extend(("--out", "{output_dir}"))
        node(
            phase6_id, "phase6", ["shared-phase1-5", "physical-lookahead"],
            phase6_command,
            [executable, "experiment-stage", "phase6-validate", "{artifact_root}", "--shared", "{dependency:shared-phase1-5}", "--lookahead", "{dependency:physical-lookahead}", "--platform", str(platform), "--provider", provider, "--managed-dag-node"],
            [_artifact("split", "consumer-checkpoint"), _artifact("schedule.json", "consumer-checkpoint"), _artifact("experiment-phase6-report.json", "evidence-critical"), *extra_artifacts],
            inputs=tuple(phase6_inputs), configuration={"provider": provider, "equivalence_cycles": 16}, peak_gib=12, retained_gib=4, provider=provider,
        )
    phase7_providers = (
        ("baseline",)
        if cut_mode == CUT_MODE_STATIC_EXACT
        else ("baseline", "placement-aware", "chimew")
    )
    for provider in phase7_providers:
        for seed in physical_seeds:
            phase6_id = f"phase6-{provider}"
            phase7_id = f"phase7-{provider}-seed{seed}"
            node(
                phase7_id, "phase7", ["shared-phase1-5", "physical-lookahead", phase6_id],
                [executable, "experiment-stage", "phase7-run", "--shared", "{dependency:shared-phase1-5}", "--lookahead", "{dependency:physical-lookahead}", "--phase6", f"{{dependency:{phase6_id}}}", "--reuse-validated-phase6-equivalence", "--platform", str(platform), "--seed", str(seed), "--workers", str(workers), "--yosys", str(tools["yosys"]), "--vpr", str(tools["vpr"]), "--architecture-importer", str(tools["architecture_importer"]), "--packed-importer", str(tools["packed_importer"]), "--route-checker", str(tools["route_checker"]), "--openparf-install", str(openparf_install), "--openparf-python", str(tools["openparf_python"]), "--route-channel-width", str(channel_width), "--managed-storage", "--managed-dag-node", "--out", "{output_dir}"],
                [executable, "experiment-stage", "phase7-validate", "{artifact_root}", "--shared", "{dependency:shared-phase1-5}", "--lookahead", "{dependency:physical-lookahead}", "--phase6", f"{{dependency:{phase6_id}}}", "--reuse-validated-phase6-equivalence", "--platform", str(platform), "--seed", str(seed), "--workers", str(workers), "--route-channel-width", str(channel_width), "--managed-dag-node"],
                [
                    _artifact("runtime", "evidence-critical"),
                    _artifact(
                        "physical/physical-summary.json", "evidence-critical"
                    ),
                    _artifact(
                        "physical/multi-fpga-physical-flow-report.json",
                        "evidence-critical",
                    ),
                    _artifact(
                        "experiment-phase7-report.json", "evidence-critical"
                    ),
                ],
                inputs=("platform", "openparf_manifest", "openparf_implementation", "tool.emuflow", "tool.yosys", "tool.vpr", "tool.architecture_importer", "tool.packed_importer", "tool.route_checker", "tool.openparf_python"),
                configuration={"physical_backend": "open", "physical_workers": workers, "physical_peak_gib": physical_peak_gib, "physical_seed": seed, "route_channel_width": channel_width},
                peak_gib=physical_peak_gib, retained_gib=8, provider=provider, physical_seed=seed,
            )
    phase7_ids = [
        f"phase7-{provider}-seed{seed}"
        for provider in phase7_providers
        for seed in physical_seeds
    ]
    if cut_mode == CUT_MODE_STATIC_EXACT:
        spec = {
            "schema": EXPERIMENT_SPEC_V2_SCHEMA,
            "experiment_id": case_id,
            "source_commit": source_commit,
            "nodes": nodes,
        }
        validated = validate_experiment_spec(spec)
        write_json(output_path, spec)
        return {
            "status": "pass",
            "experiment_id": case_id,
            "nodes": len(validated["nodes"]),
            "physical_terminal_nodes": len(physical_seeds),
            "terminal_nodes": len(physical_seeds),
            "cut_mode": cut_mode,
            "static_exact_candidate_policy": static_exact_candidate_policy,
            "max_cross_fpga_dependency_depth": (
                max_cross_fpga_dependency_depth
            ),
            "output": str(output_path.resolve()),
        }
    comparison_command = [
        executable,
        "experiment-stage",
        "qor-compare-run",
        "--shared",
        "{dependency:shared-phase1-5}",
    ]
    comparison_validator = [
        executable,
        "experiment-stage",
        "qor-compare-validate",
        "{artifact_root}",
        "--shared",
        "{dependency:shared-phase1-5}",
    ]
    for provider in ("baseline", "placement-aware", "chimew"):
        for seed in physical_seeds:
            phase7_id = f"phase7-{provider}-seed{seed}"
            arm = (
                "--arm",
                provider,
                str(seed),
                f"{{dependency:{phase7_id}}}",
            )
            comparison_command.extend(arm)
            comparison_validator.extend(arm)
    comparison_command.extend(("--out", "{output_dir}"))
    node(
        "qor-comparison",
        "qor-compare",
        ["shared-phase1-5", *phase7_ids],
        comparison_command,
        comparison_validator,
        [_artifact("canonical-qor-comparison.json", "evidence-critical")],
        inputs=("tool.emuflow",),
        configuration={
            "providers": ["baseline", "placement-aware", "chimew"],
            "physical_seeds": list(physical_seeds),
            "primary_metrics": [
                "global_target_clock_wns_ns",
                "global_target_clock_tns_ns",
            ],
        },
        peak_gib=2,
        retained_gib=1,
    )
    spec = {
        "schema": EXPERIMENT_SPEC_V2_SCHEMA,
        "experiment_id": case_id,
        "source_commit": source_commit,
        "nodes": nodes,
    }
    validated = validate_experiment_spec(spec)
    write_json(output_path, spec)
    return {
        "status": "pass",
        "experiment_id": case_id,
        "nodes": len(validated["nodes"]),
        "physical_terminal_nodes": 3 * len(physical_seeds),
        "terminal_nodes": 1,
        "output": str(output_path.resolve()),
    }


def _static_exact_prefixed_node(
    raw: Mapping[str, Any], prefix: str
) -> Dict[str, Any]:
    """Namespace one canonical arm while retaining common frontend/timing."""

    common = {"frontend", "timing"}

    def mapped(node_id: str) -> str:
        return node_id if node_id in common else f"{prefix}-{node_id}"

    def rewrite(value: Any) -> Any:
        if isinstance(value, str):
            for dependency in raw.get("dependencies", []):
                value = value.replace(
                    f"{{dependency:{dependency}}}",
                    f"{{dependency:{mapped(dependency)}}}",
                )
            return value
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        return value

    result = rewrite(copy.deepcopy(dict(raw)))
    result["id"] = mapped(raw["id"])
    result["dependencies"] = [
        mapped(dependency) for dependency in raw.get("dependencies", [])
    ]
    return result


def compile_static_exact_ab_experiment_spec(
    config_path: Path,
    repository_root: Path,
    output_path: Path,
    *,
    legacy_max_depth: int = 2,
    generalized_max_depth: int = 8,
    minimum_combinational_cut_nets: int = 1,
    partition_seed: int | None = None,
) -> Dict[str, Any]:
    """Compile one cache DAG with three Phase 3--7 cut-policy branches.

    The canonical frontend and complete TimingPathDB are emitted once.  Each
    policy then owns its assignment, routing, schedule, lookahead, split, and
    physical terminal.  The final node independently rebuilds the complete
    Phase 7 comparison rather than comparing Phase 3 or Phase 6 proxies.
    """

    output_path = validate_experiment_write_path(output_path)
    repository_root = _directory(str(repository_root), "repository_root")
    config = read_json(config_path)
    if config.get("schema") != CANONICAL_EXPERIMENT_CONFIG_SCHEMA:
        raise ValidationError("canonical experiment config schema is invalid")
    if config.get("tritonpart_solution") is not None:
        raise ValidationError(
            "Static Exact A/B requires policy-specific partition searches; "
            "use a single-arm canonical experiment for a precomputed solution"
        )
    for name, value in (
        ("legacy_max_depth", legacy_max_depth),
        ("generalized_max_depth", generalized_max_depth),
        ("minimum_combinational_cut_nets", minimum_combinational_cut_nets),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValidationError(f"Static Exact A/B {name} must be positive")
    if legacy_max_depth not in {1, 2}:
        raise ValidationError("Static Exact legacy A/B depth must be 1 or 2")
    controlled_partition_seed = (
        config.get("partition_seed", 0)
        if partition_seed is None
        else partition_seed
    )
    if (
        isinstance(controlled_partition_seed, bool)
        or not isinstance(controlled_partition_seed, int)
        or controlled_partition_seed < 0
    ):
        raise ValidationError("Static Exact A/B partition seed is invalid")
    physical_seeds = config.get("physical_seeds", [1])
    if (
        not isinstance(physical_seeds, list)
        or not physical_seeds
        or any(
            isinstance(seed, bool) or not isinstance(seed, int) or seed < 1
            for seed in physical_seeds
        )
        or physical_seeds != sorted(set(physical_seeds))
    ):
        raise ValidationError("Static Exact A/B physical seeds are invalid")
    arm_configs = {
        "seq": {
            "label": "sequential-only",
            "cut_mode": CUT_MODE_SEQUENTIAL_ONLY,
            "static_exact_candidate_policy": STATIC_EXACT_CANDIDATE_FRONTIER_V1,
            "max_cross_fpga_dependency_depth": 1,
            "minimum_combinational_cut_nets": 0,
        },
        "v1": {
            "label": "legacy-static-exact-v1",
            "cut_mode": CUT_MODE_STATIC_EXACT,
            "static_exact_candidate_policy": STATIC_EXACT_CANDIDATE_FRONTIER_V1,
            "max_cross_fpga_dependency_depth": legacy_max_depth,
            # Legacy v1 is a compatibility/negative-control arm. Its
            # potential-frontier filter can legitimately release no selected
            # combinational boundary, so only generalized v2 owns the positive
            # exercise contract below.
            "minimum_combinational_cut_nets": 0,
        },
        "v2": {
            "label": "generalized-static-exact-v2",
            "cut_mode": CUT_MODE_STATIC_EXACT,
            "static_exact_candidate_policy": STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
            "max_cross_fpga_dependency_depth": generalized_max_depth,
            "minimum_combinational_cut_nets": minimum_combinational_cut_nets,
        },
    }
    compiled: Dict[str, Dict[str, Any]] = {}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=output_path.parent, prefix=".static-exact-ab-compile-"
    ) as temporary:
        temporary_root = Path(temporary)
        for prefix, arm in arm_configs.items():
            arm_config = dict(config)
            arm_config.update(
                {
                    key: value
                    for key, value in arm.items()
                    if key != "label"
                }
            )
            # A policy comparison must not let each arm search a different
            # random-seed portfolio and then compare unrelated winners.  One
            # explicit partition seed is shared by all arms; physical-tool
            # seeds remain a separate paired axis below.
            arm_config["partition_seed"] = controlled_partition_seed
            arm_config["partition_seed_attempts"] = 1
            arm_config_path = temporary_root / f"{prefix}-config.json"
            arm_spec_path = temporary_root / f"{prefix}-spec.json"
            write_json(arm_config_path, arm_config)
            compile_canonical_experiment_spec(
                arm_config_path, repository_root, arm_spec_path
            )
            compiled[prefix] = read_json(arm_spec_path)

    common_ids = {"frontend", "timing"}
    sequential_keep = {
        *common_ids,
        "partition",
        "cut-timing",
        "route",
        "tdm",
        "shared-phase1-5",
        "phase6-baseline",
        "physical-lookahead",
        *{
            f"phase7-baseline-seed{seed}" for seed in physical_seeds
        },
    }
    reference_common = [
        node
        for node in compiled["seq"]["nodes"]
        if node["id"] in common_ids
    ]
    for prefix in ("v1", "v2"):
        candidate_common = [
            node
            for node in compiled[prefix]["nodes"]
            if node["id"] in common_ids
        ]
        if candidate_common != reference_common:
            raise ValidationError(
                "Static Exact A/B frontend/timing branches are not identical"
            )
    nodes = copy.deepcopy(reference_common)
    for prefix in ("seq", "v1", "v2"):
        arm_nodes = compiled[prefix]["nodes"]
        for raw in arm_nodes:
            if raw["id"] in common_ids:
                continue
            if prefix == "seq" and raw["id"] not in sequential_keep:
                continue
            nodes.append(_static_exact_prefixed_node(raw, prefix))

    executable = str(_file(config["tools"]["emuflow"], "tool emuflow"))
    platform = _file(config.get("platform"), "platform")
    dependencies: list[str] = []
    command = [
        executable,
        "experiment-stage",
        "static-exact-qor-compare-run",
        "--platform",
        str(platform),
        "--reuse-validated-phase6-equivalence",
    ]
    validator = [
        executable,
        "experiment-stage",
        "static-exact-qor-compare-validate",
        "{artifact_root}",
        "--platform",
        str(platform),
        "--reuse-validated-phase6-equivalence",
    ]
    for prefix, arm in arm_configs.items():
        shared_id = f"{prefix}-shared-phase1-5"
        lookahead_id = f"{prefix}-physical-lookahead"
        phase6_id = f"{prefix}-phase6-baseline"
        for seed in physical_seeds:
            phase7_id = f"{prefix}-phase7-baseline-seed{seed}"
            for dependency in (
                shared_id,
                lookahead_id,
                phase6_id,
                phase7_id,
            ):
                if dependency not in dependencies:
                    dependencies.append(dependency)
            arguments = [
                "--arm",
                arm["label"],
                str(seed),
                f"{{dependency:{shared_id}}}",
                f"{{dependency:{lookahead_id}}}",
                f"{{dependency:{phase6_id}}}",
                f"{{dependency:{phase7_id}}}",
            ]
            command.extend(arguments)
            validator.extend(arguments)
    command.extend(("--out", "{output_dir}"))
    input_hashes = {
        "platform": _sha256(platform),
        "tool.emuflow": _sha256(Path(executable)),
    }
    bindings = {"platform": str(platform), "tool.emuflow": executable}
    closure = _closure(repository_root, "static-exact-qor-compare")
    nodes.append(
        {
            "id": "static-exact-qor-comparison",
            "stage": "static-exact-qor-compare",
            "dependencies": dependencies,
            "inputs": input_hashes,
            "configuration": {
                "labels": [
                    arm_configs[prefix]["label"]
                    for prefix in ("seq", "v1", "v2")
                ],
                "physical_seeds": physical_seeds,
                "legacy_max_depth": legacy_max_depth,
                "generalized_max_depth": generalized_max_depth,
                "legacy_minimum_combinational_cut_nets": 0,
                "generalized_minimum_combinational_cut_nets": (
                    minimum_combinational_cut_nets
                ),
                "partition_seed": controlled_partition_seed,
                "partition_seed_attempts": 1,
                "primary_metrics": [
                    "global_target_clock_wns_ns",
                    "global_target_clock_tns_ns",
                ],
            },
            "implementation": closure,
            "execution_bindings": bindings,
            "command": command,
            "command_identity": _identity_argv(command, bindings),
            "validator_implementation": closure,
            "validator": validator,
            "validator_identity": _identity_argv(validator, bindings),
            "environment": {
                "EMUFLOW_EXPERIMENT_POLICY": "static-exact-ab-v1"
            },
            "storage_estimate": {
                "peak_bytes": 2 * 1024**3,
                "retained_bytes": 1024**3,
            },
            "artifacts": [
                _artifact(
                    "static-exact-qor-comparison.json", "evidence-critical"
                )
            ],
        }
    )
    case_id = config.get("case_id")
    spec = {
        "schema": EXPERIMENT_SPEC_V2_SCHEMA,
        "experiment_id": f"{case_id}-static-exact-ab",
        "source_commit": config.get("source_commit"),
        "nodes": nodes,
    }
    validated = validate_experiment_spec(spec)
    write_json(output_path, spec)
    return {
        "status": "pass",
        "experiment_id": spec["experiment_id"],
        "nodes": len(validated["nodes"]),
        "physical_terminal_nodes": 3 * len(physical_seeds),
        "terminal_nodes": 1,
        "physical_seeds": physical_seeds,
        "output": str(output_path.resolve()),
    }
