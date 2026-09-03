from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence


class _Python38BooleanOptionalAction(argparse.Action):
    """Python 3.8 equivalent of :class:`argparse.BooleanOptionalAction`."""

    def __init__(self, option_strings, dest, default=None, **kwargs):
        expanded = []
        for option in option_strings:
            expanded.append(option)
            if option.startswith("--"):
                expanded.append("--no-" + option[2:])
        super().__init__(
            option_strings=expanded,
            dest=dest,
            default=default,
            nargs=0,
            **kwargs,
        )

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, not option_string.startswith("--no-"))


_BooleanOptionalAction = getattr(
    argparse, "BooleanOptionalAction", _Python38BooleanOptionalAction
)


from .archive import (
    DEFAULT_MAX_COPY_BYTES,
    cleanup_validation_source,
    create_validation_archive,
    validate_validation_archive,
)
from .architecture import ArchitectureDB
from .benchmark import run_benchmark
from .board_arm_mps4 import materialize_arm_mps4_boarddb
from .board_link_timing import (
    build_board_link_timing_model,
    validate_board_link_timing,
)
from .board_support import validate_board_support_overlay_file
from .bsp import run_phase8a
from .cross_stage import (
    evaluate_cross_stage_candidate,
    run_cross_stage_optimization,
    validate_cross_stage_candidate,
    validate_cross_stage_report,
)
from .chimew_phase6 import run_chimew_phase6_adapter
from .chimew_grouping import materialize_chimew_schedule_ratios
from .chimew_pipeline import (
    run_chimew_phase6_pipeline,
    validate_chimew_phase6_pipeline,
)
from .chimew_correlation import (
    build_chimew_vivado_correlation,
    validate_chimew_vivado_correlation,
)
from .chimew_qualification import build_chimew_phase6_qualification
from .contest_eda2025 import (
    evaluate_eda2025_routes,
    import_eda2025_instance,
    materialize_eda2025_rtl_boarddb,
    optimize_eda2025_routing,
    optimize_eda2025_topology,
)
from .contest_eda2024 import (
    evaluate_eda2024_solution,
    materialize_eda2024_rtl_boarddb,
)
from .contest_eda2023 import (
    evaluate_eda2023_solution,
    import_eda2023_case,
    materialize_eda2023_rtl_boarddb,
    optimize_eda2023_tdm,
)
from .contest_eda2023_chimew import run_eda2023_contest_chimew_ab
from .contest_iccad2019 import (
    evaluate_iccad2019_solution,
    import_iccad2019_instance,
    materialize_iccad2019_rtl_boarddb,
    optimize_iccad2019_ratios,
)
from .contest_public import (
    build_contest_boarddb_farm_spec,
    build_contest_evaluation_farm_spec,
    build_contest_fetch_farm_spec,
    build_contest_import_farm_spec,
    evaluate_public_contest_case,
    fetch_public_contest_case,
    import_public_contest_case,
    materialize_public_contest_boarddb,
    validate_public_contest_evaluation,
)
from .contest_validation_matrix import load_contest_validation_matrix
from .end_to_end_validation_matrix import load_end_to_end_validation_matrix
from .canonical_experiment import (
    compile_canonical_experiment_spec,
    compile_static_exact_ab_experiment_spec,
)
from .partition_qualification import compile_partition_qualification_spec
from .canonical_qor import (
    parse_canonical_qor_arms,
    run_canonical_qor_comparison,
    validate_canonical_qor_comparison,
)
from .static_exact_qor import (
    parse_static_exact_qor_arms,
    run_static_exact_qor_comparison,
    validate_static_exact_qor_comparison,
)
from .mfspart_refine import DEFAULT_TIMING_PATH_BETA
from .combinational_cut import (
    STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
    STATIC_EXACT_CANDIDATE_FRONTIER_V1,
    STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH,
    characterize_combinational_cuts,
    validate_combinational_cut_characterization,
)
from .experiment_dag import (
    build_experiment_farm_spec,
    import_experiment_checkpoint,
    plan_experiment,
    run_experiment_node,
)
from .experiment_identity import (
    build_implementation_closure,
    validate_implementation_closure,
)
from .experiment_store import (
    apply_legacy_run_retirement,
    apply_experiment_gc,
    create_experiment_evidence_bundle,
    inventory_experiment_store,
    plan_experiment_gc,
    plan_legacy_run_migration,
    plan_legacy_run_retirement,
    resume_legacy_run_retirement,
    validate_experiment_evidence_bundle,
)
from .experiment_stages import (
    run_phase6_checkpoint,
    run_phase7_checkpoint,
    run_physical_lookahead,
    resume_physical_lookahead,
    validate_phase6_checkpoint,
    validate_phase7_checkpoint,
    validate_physical_lookahead,
    validate_shared_phase1_5,
)
from .experiment_upstream import (
    materialize_shared_phase1_5,
    run_cut_timing_checkpoint,
    run_frontend_checkpoint,
    run_route_checkpoint,
    run_tdm_checkpoint,
    run_timing_checkpoint,
    validate_cut_timing_checkpoint,
    validate_frontend_checkpoint,
    validate_materialized_shared_phase1_5,
    validate_route_checkpoint,
    validate_tdm_checkpoint,
    validate_timing_checkpoint,
)
from .experiment_partition import (
    run_partition_checkpoint,
    validate_partition_checkpoint,
)
from .errors import EmuFlowError
from .fpga_interchange import (
    check_ir_architecture_capacity,
    run_fpga_interchange_architecture_import,
    validate_fpga_interchange_architecture,
)
from .io import json_write_policy, read_json, write_json
from .ir import EmuIR
from .lowering import run_placement_ir_lowering
from .multi_fpga_flow import (
    finalize_multi_fpga_physical_checkpoint,
    run_multi_fpga_flow,
    validate_multi_fpga_flow_bundle,
)
from .multi_fpga_bsp_flow import run_multi_fpga_bsp_flow
from .multi_fpga_physical_flow import run_multi_fpga_physical_flow
from .opensta import (
    DEFAULT_TIMING_MODEL,
    parse_clock_definitions,
    run_opensta_path_database,
)
from .open_physical_flow import run_open_physical_flow
from .phase1 import run_phase1
from .phase2 import run_phase2
from .phase3 import run_phase3, validate_phase3
from .phase4 import run_phase4, validate_phase4
from .phase5 import run_phase5, validate_phase5
from .phase6 import run_phase6, validate_phase6
from .phase7c import run_phase7c
from .packed_netlist import (
    run_packed_netlist_import,
    validate_packed_netlist_file,
)
from .packed_placement import run_packed_openparf_placement
from .partition_feedback import run_partition_feedback
from .partition_pressure import run_partition_pressure_reference
from .physical_pins import (
    SERIAL_TRANSCEIVER_PROVIDER,
    run_phase6b,
    validate_package_pin_binding,
    validate_serial_transceiver_binding,
)
from .physical_regions import (
    run_physical_region_merge,
    validate_fpga_interchange_architecture_regions,
)
from .placement import Placement
from .platform import Platform
from .pin_planning import (
    build_pin_plan,
    build_signal_position_hints,
    validate_pin_plan,
)
from .release import run_phase7d
from .route_artifact import validate_vpr_route_artifacts
from .routing_tdm_comparison import (
    build_system_route_tdm_ab_comparison,
    build_system_route_tdm_scale_comparison,
)
from .runtime_sync import (
    run_runtime_sync_materialization,
    validate_runtime_sync_provider,
)
from .synthesis import (
    VALID_SYNTHESIS_POLICIES,
    VALID_XILINX_FAMILIES,
    run_yosys,
)
from .sta import (
    derive_partition_net_weights,
    import_vivado_path_database_tsv,
    import_vivado_sta_tsv,
    project_sta_path_database,
    validate_sta_path_database,
    write_vivado_cut_net_map,
    write_vivado_net_map,
)
from .serial_wrapper import run_phase6c
from .serial_phy_provider import validate_serial_phy_provider_file
from .serial_phy_elaboration import run_serial_phy_elaboration
from .serial_phy_recipe import materialize_serial_phy_recipe
from .tdm import TDM_BASELINE_PROVIDER
from .tdm_ratio import TDM_RATIO_PROVIDER, TDM_TIMING_DAG_RATIO_PROVIDER
from .timing_routing import (
    GLOBAL_CANDIDATE_PROVIDER,
    NATIVE_ROUTER_PROVIDER,
    NATIVE_TIMING_EVALUATED_PROVIDER,
    ROUTE_TDM_PROVIDER,
    TLR_PROVIDER,
)
from .yosys import import_yosys_json
from .verilog import emit_mapped_verilog
from .vtr_architecture import (
    fetch_pinned_vtr_architecture,
    read_vpr_placement_dimensions,
    run_vtr_architecture_import,
    validate_vtr_architecture_db,
    validate_vtr_timing_db_file,
)
from .vpr import run_vpr, run_vpr_route_packed, run_vtr_yosys
from .vivado_board_flow import (
    run_vivado_board_flow,
    validate_vivado_board_flow_bundle,
)
from .vivado_board_timing import run_vivado_board_timing
from .vivado_pin_sites import derive_vivado_pin_sites
from .validation_farm import (
    detach_validation_farm_task,
    launch_validation_farm,
    prepare_validation_farm,
    reconcile_validation_farm,
    run_validation_farm_task,
    validation_farm_status,
    validate_validation_farm,
)


def _print_json(value: Dict[str, Any]) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _keyed_paths(values: Sequence[str], option: str) -> Dict[str, Path]:
    result: Dict[str, Path] = {}
    for value in values:
        key, separator, raw_path = value.partition("=")
        if not separator or not key or not raw_path:
            raise ValueError(f"{option}: expected KEY=PATH, got {value!r}")
        if key in result:
            raise ValueError(f"{option}: duplicate key {key!r}")
        result[key] = Path(raw_path)
    return result


def _keyed_values(values: Sequence[str], option: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key or not item:
            raise ValueError(f"{option}: expected KEY=VALUE, got {value!r}")
        if key in result:
            raise ValueError(f"{option}: duplicate key {key!r}")
        result[key] = item
    return result


def _jsonable_cli_configuration(args: argparse.Namespace) -> Dict[str, Any]:
    def normalize(value: Any) -> Any:
        if isinstance(value, Path):
            return str(value.resolve())
        if isinstance(value, dict):
            return {
                str(key): normalize(item)
                for key, item in sorted(value.items())
            }
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        return value

    return {
        key: normalize(value)
        for key, value in sorted(vars(args).items())
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="emuflow",
        description="Open multi-FPGA emulation flow frontend",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    archive_parser = subparsers.add_parser(
        "archive", help="archive and safely clean completed validation runs"
    )
    archive_subparsers = archive_parser.add_subparsers(
        dest="archive_command", required=True
    )
    archive_create = archive_subparsers.add_parser(
        "create", help="create a checked, storage-bounded validation archive"
    )
    archive_create.add_argument("--flow", type=Path, required=True)
    archive_create.add_argument("--out", type=Path, required=True)
    archive_create.add_argument("--run-id", required=True)
    archive_create.add_argument("--source-commit")
    archive_create.add_argument(
        "--max-copy-bytes", type=int, default=DEFAULT_MAX_COPY_BYTES
    )
    archive_create.add_argument(
        "--tool-version", action="append", default=[], metavar="NAME=VERSION"
    )
    archive_validate = archive_subparsers.add_parser(
        "validate", help="verify an archive without its original run directory"
    )
    archive_validate.add_argument("archive", type=Path)
    archive_cleanup = archive_subparsers.add_parser(
        "cleanup", help="delete a source run only after sealed archive validation"
    )
    archive_cleanup.add_argument("archive", type=Path)
    archive_cleanup.add_argument("--flow", type=Path, required=True)

    farm_parser = subparsers.add_parser(
        "validation-farm",
        help="schedule version-pinned validation tasks on shared-filesystem nodes",
    )
    farm_subparsers = farm_parser.add_subparsers(
        dest="farm_command", required=True
    )
    farm_prepare = farm_subparsers.add_parser(
        "prepare", help="validate a farm spec and reserve isolated run directories"
    )
    farm_prepare.add_argument("--spec", type=Path, required=True)
    farm_prepare.add_argument("--out", type=Path, required=True)
    farm_prepare.add_argument(
        "--ssh-known-hosts",
        type=Path,
        help="absolute known_hosts file to content-seal into farm submission",
    )
    farm_validate = farm_subparsers.add_parser(
        "validate", help="verify task isolation and the pinned install contract"
    )
    farm_validate.add_argument("farm", type=Path)
    farm_launch = farm_subparsers.add_parser(
        "launch", help="submit prepared tasks to their assigned nodes"
    )
    farm_launch.add_argument("farm", type=Path)
    farm_launch.add_argument("--submit-workers", type=int, default=8)
    farm_status = farm_subparsers.add_parser(
        "status", help="summarize task states from the shared filesystem"
    )
    farm_status.add_argument("farm", type=Path)
    farm_reconcile = farm_subparsers.add_parser(
        "reconcile", help="probe expired leases and mark confirmed-dead attempts retryable"
    )
    farm_reconcile.add_argument("farm", type=Path)
    farm_worker = farm_subparsers.add_parser(
        "worker", help=argparse.SUPPRESS
    )
    farm_worker.add_argument("--task", type=Path, required=True)
    farm_worker.add_argument("--detach", action="store_true")

    experiment = subparsers.add_parser(
        "experiment-cache",
        help="plan and execute content-addressed staged validation checkpoints",
    )
    experiment_subparsers = experiment.add_subparsers(
        dest="experiment_command", required=True
    )
    experiment_closure = experiment_subparsers.add_parser(
        "implementation-closure",
        help="seal the exact source/tool files implementing one DAG stage",
    )
    experiment_closure.add_argument("--root", type=Path, required=True)
    experiment_closure.add_argument(
        "--component", action="append", default=[], required=True
    )
    experiment_closure.add_argument("--out", type=Path, required=True)
    experiment_closure_validate = experiment_subparsers.add_parser(
        "implementation-validate",
        help="validate a stage implementation closure and optionally its files",
    )
    experiment_closure_validate.add_argument("manifest", type=Path)
    experiment_closure_validate.add_argument("--root", type=Path)
    experiment_inventory = experiment_subparsers.add_parser(
        "inventory", help="inventory valid, invalid, transient, and role bytes"
    )
    experiment_inventory.add_argument("--cache", type=Path, required=True)
    experiment_inventory.add_argument("--out", type=Path)
    experiment_evidence = experiment_subparsers.add_parser(
        "evidence-create",
        help="create a self-contained evidence bundle for terminal DAG nodes",
    )
    experiment_evidence.add_argument("--plan", type=Path, required=True)
    experiment_evidence.add_argument(
        "--terminal", action="append", default=[], required=True
    )
    experiment_evidence.add_argument("--out", type=Path, required=True)
    experiment_evidence_validate = experiment_subparsers.add_parser(
        "evidence-validate", help="validate evidence without its source cache"
    )
    experiment_evidence_validate.add_argument("bundle", type=Path)
    experiment_gc_plan = experiment_subparsers.add_parser(
        "gc-plan", help="plan reference-aware cache reclamation without deleting"
    )
    experiment_gc_plan.add_argument("--cache", type=Path, required=True)
    experiment_gc_plan.add_argument("--root-plan", type=Path, action="append", default=[])
    experiment_gc_plan.add_argument(
        "--minimum-age-seconds", type=int, default=7 * 24 * 3600
    )
    experiment_gc_plan.add_argument("--out", type=Path, required=True)
    experiment_gc_apply = experiment_subparsers.add_parser(
        "gc-apply", help="apply an unchanged GC plan by its exact SHA-256"
    )
    experiment_gc_apply.add_argument("--plan", type=Path, required=True)
    experiment_gc_apply.add_argument("--expected-plan-sha256", required=True)
    experiment_migration = experiment_subparsers.add_parser(
        "migration-plan",
        help="inventory legacy run trees and recommend validation/import actions",
    )
    experiment_migration.add_argument("--root", type=Path, required=True)
    experiment_migration.add_argument("--out", type=Path, required=True)
    experiment_retire_plan = experiment_subparsers.add_parser(
        "retirement-plan",
        help="content-seal explicitly retired legacy run trees without deleting",
    )
    experiment_retire_plan.add_argument("--migration-plan", type=Path, required=True)
    experiment_retire_plan.add_argument("--name", action="append", default=[], required=True)
    experiment_retire_plan.add_argument("--reason", required=True)
    experiment_retire_plan.add_argument("--out", type=Path, required=True)
    experiment_retire_apply = experiment_subparsers.add_parser(
        "retirement-apply",
        help="apply an unchanged legacy retirement plan and retain tombstones",
    )
    experiment_retire_apply.add_argument("--plan", type=Path, required=True)
    experiment_retire_apply.add_argument("--expected-plan-sha256", required=True)
    experiment_retire_apply.add_argument("--receipt-root", type=Path, required=True)
    experiment_retire_resume = experiment_subparsers.add_parser(
        "retirement-resume",
        help="resume removal of an unchanged atomically quarantined retirement",
    )
    experiment_retire_resume.add_argument("--receipt-root", type=Path, required=True)
    experiment_retire_resume.add_argument(
        "--expected-receipt-sha256", required=True
    )
    experiment_plan = experiment_subparsers.add_parser(
        "plan", help="resolve cache hits and the next runnable DAG frontier"
    )
    experiment_plan.add_argument("--spec", type=Path, required=True)
    experiment_plan.add_argument("--cache", type=Path, required=True)
    experiment_plan.add_argument("--out", type=Path, required=True)
    experiment_import = experiment_subparsers.add_parser(
        "import", help="validate and register an already completed checkpoint"
    )
    experiment_import.add_argument("--plan", type=Path, required=True)
    experiment_import.add_argument("--expected-plan-sha256")
    experiment_import.add_argument("--node", required=True)
    experiment_import.add_argument("--artifact-root", type=Path, required=True)
    experiment_farm = experiment_subparsers.add_parser(
        "farm-spec", help="compile only the current cache-miss frontier"
    )
    experiment_farm.add_argument("--plan", type=Path, required=True)
    experiment_farm.add_argument("--install-dir", type=Path, required=True)
    experiment_farm.add_argument("--node", action="append", default=[])
    experiment_farm.add_argument(
        "--experiment-node",
        action="append",
        default=[],
        help="submit only this ready/revalidate experiment node; repeatable",
    )
    experiment_farm.add_argument(
        "--worker-arg",
        action="append",
        default=[],
        help=(
            "seal one validation-farm worker wrapper argument; repeat in argv "
            "order (supports {install})"
        ),
    )
    experiment_farm.add_argument("--farm-id", required=True)
    experiment_farm.add_argument(
        "--worker-launcher",
        type=Path,
        help=(
            "absolute, content-sealed outer launcher used to enter the worker "
            "runtime before invoking the pinned install"
        ),
    )
    experiment_farm.add_argument("--out", type=Path, required=True)
    experiment_run = experiment_subparsers.add_parser(
        "run-node", help=argparse.SUPPRESS
    )
    experiment_run.add_argument("--plan", type=Path, required=True)
    experiment_run.add_argument("--expected-plan-sha256")
    experiment_run.add_argument("--node", required=True)
    experiment_run.add_argument("--run-dir", type=Path, required=True)

    experiment_stage = subparsers.add_parser(
        "experiment-stage",
        help="run or validate reusable Phase 1-7 experiment checkpoints",
    )
    experiment_stage_subparsers = experiment_stage.add_subparsers(
        dest="experiment_stage_command", required=True
    )
    frontend_run = experiment_stage_subparsers.add_parser(
        "frontend-run", help="run one reusable synthesis and Phase 1 checkpoint"
    )
    frontend_run.add_argument("--platform", type=Path, required=True)
    frontend_run.add_argument("--source", type=Path, action="append", default=[])
    frontend_run.add_argument("--top")
    frontend_run.add_argument("--clock", action="append", default=[])
    frontend_run.add_argument("--yosys-json", type=Path)
    frontend_run.add_argument("--yosys")
    frontend_run.add_argument(
        "--mapping-profile",
        choices=("vtr-hard-blocks", "generic-soft"),
        default="vtr-hard-blocks",
    )
    frontend_run.add_argument("--allow-fabric-clock", action="store_true")
    frontend_run.add_argument("--managed-dag-node", action="store_true")
    frontend_run.add_argument("--out", type=Path, required=True)
    frontend_validate = experiment_stage_subparsers.add_parser(
        "frontend-validate", help="independently validate a frontend checkpoint"
    )
    frontend_validate.add_argument("root", type=Path)
    frontend_validate.add_argument("--platform", type=Path, required=True)
    frontend_validate.add_argument("--managed-dag-node", action="store_true")

    timing_run = experiment_stage_subparsers.add_parser(
        "timing-run", help="run reusable pre-partition OpenSTA timing"
    )
    timing_run.add_argument("--frontend", type=Path, required=True)
    timing_run.add_argument("--clock-period", action="append", default=[], required=True)
    timing_run.add_argument("--timing-model", type=Path, default=DEFAULT_TIMING_MODEL)
    timing_run.add_argument("--architecture-timing-db", type=Path)
    timing_run.add_argument("--opensta")
    timing_run.add_argument("--max-paths", type=int, default=200000)
    timing_run.add_argument("--criticality-scale", type=float, default=9.0)
    timing_run.add_argument("--criticality-exponent", type=float, default=2.0)
    timing_run.add_argument("--managed-dag-node", action="store_true")
    timing_run.add_argument("--out", type=Path, required=True)
    timing_validate = experiment_stage_subparsers.add_parser(
        "timing-validate", help="independently validate a timing checkpoint"
    )
    timing_validate.add_argument("root", type=Path)
    timing_validate.add_argument("--frontend", type=Path, required=True)
    timing_validate.add_argument("--managed-dag-node", action="store_true")

    partition_run = experiment_stage_subparsers.add_parser(
        "partition-run", help="run one reusable timing-driven Phase 3 checkpoint"
    )
    partition_run.add_argument("--frontend", type=Path, required=True)
    partition_run.add_argument("--timing", type=Path, required=True)
    partition_run.add_argument("--platform", type=Path, required=True)
    partition_run.add_argument(
        "--provider",
        choices=(
            "tritonpart",
            "greedy",
            "repart",
            "repart-replication",
            "mfspart",
            "patron",
        ),
        default="patron",
    )
    partition_run.add_argument("--seed", type=int, default=0)
    partition_run.add_argument("--constraints", type=Path)
    partition_run.add_argument("--route-constraints", type=Path)
    partition_run.add_argument("--min-used-fpgas", type=int)
    partition_run.add_argument("--balance-tolerance", type=float)
    partition_run.add_argument("--openroad")
    partition_run.add_argument(
        "--tritonpart-solution",
        type=Path,
        help=(
            "import a precomputed TritonPart .part solution and seal its "
            "content in the reusable Phase 3 checkpoint"
        ),
    )
    partition_run.add_argument("--hop-refiner")
    partition_run.add_argument("--patron-refiner")
    partition_run.add_argument("--patron-max-moves", type=int)
    partition_run.add_argument(
        "--patron-flow-refinement", action="store_true"
    )
    partition_run.add_argument(
        "--patron-algorithm-version",
        type=int,
        choices=(6, 9, 10, 11, 12, 13, 14),
        default=6,
    )
    partition_run.add_argument("--patron-initial-assignment", type=Path)
    partition_run.add_argument("--patron-initial-clusters", type=Path)
    partition_run.add_argument(
        "--patron-physical-system-timing", type=Path
    )
    partition_run.add_argument(
        "--patron-physical-feedback-scale", type=float, default=0.0
    )
    partition_run.add_argument("--mfspart-coarsener")
    partition_run.add_argument("--mfspart-initializer")
    partition_run.add_argument("--mfspart-refiner")
    partition_run.add_argument("--mfspart-refiner-checker")
    partition_run.add_argument("--mfspart-legalizer")
    partition_run.add_argument(
        "--mfspart-post-refinement",
        action=_BooleanOptionalAction,
        default=None,
        help=(
            "directionally post-refine a TritonPart assignment with the "
            "source-bound MFSPart FM refiner"
        ),
    )
    partition_run.add_argument(
        "--mfspart-post-refinement-early-stop", type=int, default=1000
    )
    partition_run.add_argument(
        "--mfspart-post-refinement-bottleneck-beta",
        type=float,
        default=256.0,
    )
    partition_run.add_argument(
        "--mfspart-post-refinement-timing-path-beta",
        type=float,
        default=DEFAULT_TIMING_PATH_BETA,
    )
    partition_run.add_argument("--timeout-seconds", type=int, default=3600)
    partition_run.add_argument("--seed-attempts", type=int, default=1)
    partition_run.add_argument(
        "--repair-balance",
        action="store_true",
        help=(
            "deterministically legalize a TritonPart candidate against "
            "the independently checked multi-resource upper bounds"
        ),
    )
    partition_run.add_argument("--num-initial-solutions", type=int, default=50)
    partition_run.add_argument("--num-best-initial-solutions", type=int, default=10)
    partition_run.add_argument(
        "--cut-mode",
        choices=("sequential-only", "static-exact-combinational"),
        default="static-exact-combinational",
    )
    partition_run.add_argument(
        "--max-cross-fpga-dependency-depth",
        type=int,
        default=STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH,
    )
    partition_run.add_argument(
        "--static-exact-candidate-policy",
        choices=(
            STATIC_EXACT_CANDIDATE_FRONTIER_V1,
            STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
        ),
        default=STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    )
    partition_run.add_argument(
        "--minimum-combinational-cut-nets", type=int, default=0
    )
    partition_run.add_argument(
        "--managed-dag-node",
        action="store_true",
        help=(
            "reuse validated immutable dependencies and defer the duplicate "
            "producer-side full validation to experiment-cache's independent "
            "validator"
        ),
    )
    partition_run.add_argument("--out", type=Path, required=True)
    partition_validate = experiment_stage_subparsers.add_parser(
        "partition-validate", help="independently validate a Phase 3 checkpoint"
    )
    partition_validate.add_argument("root", type=Path)
    partition_validate.add_argument("--frontend", type=Path, required=True)
    partition_validate.add_argument("--timing", type=Path, required=True)
    partition_validate.add_argument("--platform", type=Path, required=True)
    partition_validate.add_argument("--constraints", type=Path)
    partition_validate.add_argument("--route-constraints", type=Path)
    partition_validate.add_argument("--tritonpart-solution", type=Path)
    partition_validate.add_argument(
        "--mfspart-post-refinement",
        action=_BooleanOptionalAction,
        default=None,
    )
    partition_validate.add_argument(
        "--mfspart-post-refinement-early-stop", type=int
    )
    partition_validate.add_argument(
        "--mfspart-post-refinement-bottleneck-beta", type=float
    )
    partition_validate.add_argument(
        "--mfspart-post-refinement-timing-path-beta", type=float
    )
    partition_validate.add_argument("--provider")
    partition_validate.add_argument("--patron-initial-assignment", type=Path)
    partition_validate.add_argument("--patron-initial-clusters", type=Path)
    partition_validate.add_argument(
        "--patron-physical-system-timing", type=Path
    )
    partition_validate.add_argument(
        "--patron-physical-feedback-scale", type=float
    )
    partition_validate.add_argument(
        "--patron-flow-refinement",
        action=_BooleanOptionalAction,
        default=None,
    )
    partition_validate.add_argument(
        "--patron-algorithm-version",
        type=int,
        choices=(6, 9, 10, 11, 12, 13, 14),
    )
    partition_validate.add_argument("--patron-max-moves", type=int)
    partition_validate.add_argument("--seed", type=int)
    partition_validate.add_argument("--seed-attempts", type=int)
    partition_validate.add_argument(
        "--repair-balance",
        action=_BooleanOptionalAction,
        default=None,
    )
    partition_validate.add_argument(
        "--cut-mode",
        choices=("sequential-only", "static-exact-combinational"),
    )
    partition_validate.add_argument(
        "--max-cross-fpga-dependency-depth", type=int
    )
    partition_validate.add_argument(
        "--static-exact-candidate-policy",
        choices=(
            STATIC_EXACT_CANDIDATE_FRONTIER_V1,
            STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
        ),
    )
    partition_validate.add_argument(
        "--minimum-combinational-cut-nets", type=int
    )
    partition_validate.add_argument(
        "--online-validation",
        action="store_true",
        help=(
            "check only the Phase-3 runtime contract; do not hash artifacts "
            "or replay the optimizer"
        ),
    )

    cut_timing_run = experiment_stage_subparsers.add_parser(
        "cut-timing-run", help="extract and project partition cut timing paths"
    )
    cut_timing_run.add_argument("--frontend", type=Path, required=True)
    cut_timing_run.add_argument("--timing", type=Path, required=True)
    cut_timing_run.add_argument("--partition", type=Path, required=True)
    cut_timing_run.add_argument("--clock-period", action="append", default=[], required=True)
    cut_timing_run.add_argument("--timing-model", type=Path, default=DEFAULT_TIMING_MODEL)
    cut_timing_run.add_argument("--architecture-timing-db", type=Path)
    cut_timing_run.add_argument("--opensta")
    cut_timing_run.add_argument("--max-paths", type=int, default=200000)
    cut_timing_run.add_argument("--managed-dag-node", action="store_true")
    cut_timing_run.add_argument("--out", type=Path, required=True)
    cut_timing_validate = experiment_stage_subparsers.add_parser(
        "cut-timing-validate", help="independently validate cut timing"
    )
    cut_timing_validate.add_argument("root", type=Path)
    cut_timing_validate.add_argument("--frontend", type=Path, required=True)
    cut_timing_validate.add_argument("--timing", type=Path, required=True)
    cut_timing_validate.add_argument("--partition", type=Path, required=True)
    cut_timing_validate.add_argument(
        "--timing-model", type=Path, default=DEFAULT_TIMING_MODEL
    )
    cut_timing_validate.add_argument("--architecture-timing-db", type=Path)
    cut_timing_validate.add_argument("--managed-dag-node", action="store_true")

    route_run = experiment_stage_subparsers.add_parser(
        "route-run", help="run one reusable timing-aware Phase 4 checkpoint"
    )
    route_run.add_argument("--partition", type=Path, required=True)
    route_run.add_argument("--cut-timing", type=Path, required=True)
    route_run.add_argument("--platform", type=Path, required=True)
    route_run.add_argument("--constraints", type=Path)
    route_run.add_argument("--frame-slots", type=int)
    route_run.add_argument("--max-iterations", type=int)
    route_run.add_argument("--provider")
    route_run.add_argument("--candidate-workers", type=int, default=1)
    route_run.add_argument("--router")
    route_run.add_argument("--managed-storage", action="store_true")
    route_run.add_argument("--managed-dag-node", action="store_true")
    route_run.add_argument("--out", type=Path, required=True)
    route_validate = experiment_stage_subparsers.add_parser(
        "route-validate", help="independently validate a Phase 4 checkpoint"
    )
    route_validate.add_argument("root", type=Path)
    route_validate.add_argument("--partition", type=Path, required=True)
    route_validate.add_argument("--cut-timing", type=Path, required=True)
    route_validate.add_argument("--platform", type=Path, required=True)
    route_validate.add_argument("--constraints", type=Path)
    route_validate.add_argument("--provider")
    route_validate.add_argument("--candidate-workers", type=int)
    route_validate.add_argument("--managed-dag-node", action="store_true")

    tdm_run = experiment_stage_subparsers.add_parser(
        "tdm-run", help="run one reusable timing-aware Phase 5 checkpoint"
    )
    tdm_run.add_argument("--route", type=Path, required=True)
    tdm_run.add_argument("--platform", type=Path, required=True)
    tdm_run.add_argument("--simulation-frames", type=int, default=16)
    tdm_run.add_argument("--provider")
    tdm_run.add_argument("--ratio-max-iterations", type=int, default=500)
    tdm_run.add_argument("--max-ratio", type=int)
    tdm_run.add_argument("--ratio-quantum", type=int, default=8)
    tdm_run.add_argument("--post-refinement-iterations", type=int, default=200)
    tdm_run.add_argument("--slot-refinement-iterations", type=int, default=0)
    tdm_run.add_argument("--ratio-optimizer")
    tdm_run.add_argument("--timing-dag-optimizer")
    tdm_run.add_argument("--slot-optimizer")
    tdm_run.add_argument("--managed-storage", action="store_true")
    tdm_run.add_argument("--managed-dag-node", action="store_true")
    tdm_run.add_argument("--out", type=Path, required=True)
    tdm_validate = experiment_stage_subparsers.add_parser(
        "tdm-validate", help="independently validate a Phase 5 checkpoint"
    )
    tdm_validate.add_argument("root", type=Path)
    tdm_validate.add_argument("--route", type=Path, required=True)
    tdm_validate.add_argument("--platform", type=Path, required=True)
    tdm_validate.add_argument("--constraints", type=Path)
    tdm_validate.add_argument("--provider")
    tdm_validate.add_argument("--managed-dag-node", action="store_true")

    shared_materialize = experiment_stage_subparsers.add_parser(
        "shared-materialize", help="materialize a hard-linked validated Phase 1-5 view"
    )
    shared_materialize.add_argument("--frontend", type=Path, required=True)
    shared_materialize.add_argument("--timing", type=Path, required=True)
    shared_materialize.add_argument("--partition", type=Path, required=True)
    shared_materialize.add_argument("--cut-timing", type=Path, required=True)
    shared_materialize.add_argument("--route", type=Path, required=True)
    shared_materialize.add_argument("--tdm", type=Path, required=True)
    shared_materialize.add_argument("--platform", type=Path, required=True)
    shared_materialize.add_argument(
        "--timing-model", type=Path, default=DEFAULT_TIMING_MODEL
    )
    shared_materialize.add_argument("--architecture-timing-db", type=Path)
    shared_materialize.add_argument("--managed-dag-node", action="store_true")
    shared_materialize.add_argument("--out", type=Path, required=True)
    shared_validate = experiment_stage_subparsers.add_parser(
        "shared-validate", help="validate a frozen Phase 1-5 flow root"
    )
    shared_validate.add_argument("--shared", type=Path, required=True)
    shared_validate.add_argument("--platform", type=Path, required=True)
    shared_validate.add_argument("--managed-dag-node", action="store_true")
    lookahead_run = experiment_stage_subparsers.add_parser(
        "lookahead-run", help="run one reusable physical-lookahead checkpoint"
    )
    lookahead_run.add_argument("--shared", type=Path, required=True)
    lookahead_run.add_argument("--baseline-phase6", type=Path)
    lookahead_run.add_argument(
        "--reuse-validated-phase6-equivalence", action="store_true"
    )
    lookahead_run.add_argument("--platform", type=Path, required=True)
    lookahead_run.add_argument("--seed", type=int, default=1)
    lookahead_run.add_argument("--workers", type=int, default=8)
    lookahead_run.add_argument("--region-count", type=int, default=4)
    lookahead_run.add_argument("--architecture", type=Path)
    lookahead_run.add_argument(
        "--architecture-id", default="vtr-flagship-k6-n10-40nm"
    )
    lookahead_run.add_argument("--yosys")
    lookahead_run.add_argument("--vpr")
    lookahead_run.add_argument("--architecture-importer")
    lookahead_run.add_argument("--packed-importer")
    lookahead_run.add_argument("--route-checker")
    lookahead_run.add_argument("--openparf-install", type=Path)
    lookahead_run.add_argument("--openparf-python", type=Path)
    lookahead_run.add_argument("--route-channel-width", type=int, default=300)
    lookahead_run.add_argument("--managed-dag-node", action="store_true")
    lookahead_run.add_argument("--out", type=Path, required=True)
    lookahead_resume = experiment_stage_subparsers.add_parser(
        "lookahead-resume",
        help="finish one reusable lookahead around a resumed physical checkpoint",
    )
    lookahead_resume.add_argument("--shared", type=Path, required=True)
    lookahead_resume.add_argument("--baseline-phase6", type=Path)
    lookahead_resume.add_argument("--platform", type=Path, required=True)
    lookahead_resume.add_argument("--seed", type=int, default=1)
    lookahead_resume.add_argument("--workers", type=int, default=8)
    lookahead_resume.add_argument("--region-count", type=int, default=4)
    lookahead_resume.add_argument("--architecture", type=Path)
    lookahead_resume.add_argument(
        "--architecture-id", default="vtr-flagship-k6-n10-40nm"
    )
    lookahead_resume.add_argument("--route-channel-width", type=int, default=300)
    lookahead_resume.add_argument(
        "--reuse-validated-phase6-equivalence", action="store_true"
    )
    lookahead_resume.add_argument("--managed-dag-node", action="store_true")
    lookahead_resume.add_argument("--out", type=Path, required=True)
    lookahead_validate = experiment_stage_subparsers.add_parser(
        "lookahead-validate", help="independently validate a lookahead checkpoint"
    )
    lookahead_validate.add_argument("root", type=Path)
    lookahead_validate.add_argument("--shared", type=Path, required=True)
    lookahead_validate.add_argument("--baseline-phase6", type=Path)
    lookahead_validate.add_argument(
        "--reuse-validated-phase6-equivalence", action="store_true"
    )
    lookahead_validate.add_argument("--platform", type=Path, required=True)
    lookahead_validate.add_argument("--seed", type=int)
    lookahead_validate.add_argument("--workers", type=int)
    lookahead_validate.add_argument("--region-count", type=int)
    lookahead_validate.add_argument("--architecture", type=Path)
    lookahead_validate.add_argument("--route-channel-width", type=int)
    lookahead_validate.add_argument("--managed-dag-node", action="store_true")
    phase6_run = experiment_stage_subparsers.add_parser(
        "phase6-run", help="run one reusable Phase 6 provider checkpoint"
    )
    phase6_run.add_argument("--shared", type=Path, required=True)
    phase6_run.add_argument("--lookahead", type=Path)
    phase6_run.add_argument("--platform", type=Path, required=True)
    phase6_run.add_argument(
        "--provider",
        choices=("baseline", "placement-aware", "chimew"),
        required=True,
    )
    phase6_run.add_argument("--equivalence-cycles", type=int, default=16)
    phase6_run.add_argument("--equivalence-seed", type=int, default=20260727)
    phase6_run.add_argument("--pin-planner")
    phase6_run.add_argument("--chimew-grouper")
    phase6_run.add_argument("--chimew-refiner")
    phase6_run.add_argument("--chimew-rudy")
    phase6_run.add_argument("--chimew-assigner")
    phase6_run.add_argument("--managed-storage", action="store_true")
    phase6_run.add_argument("--managed-dag-node", action="store_true")
    phase6_run.add_argument("--out", type=Path, required=True)
    phase6_validate = experiment_stage_subparsers.add_parser(
        "phase6-validate", help="independently validate a Phase 6 checkpoint"
    )
    phase6_validate.add_argument("root", type=Path)
    phase6_validate.add_argument("--shared", type=Path, required=True)
    phase6_validate.add_argument("--lookahead", type=Path)
    phase6_validate.add_argument("--platform", type=Path, required=True)
    phase6_validate.add_argument(
        "--provider", choices=("baseline", "placement-aware", "chimew")
    )
    phase6_validate.add_argument("--managed-dag-node", action="store_true")
    phase7_run = experiment_stage_subparsers.add_parser(
        "phase7-run", help="run one provider/seed physical terminal checkpoint"
    )
    phase7_run.add_argument("--shared", type=Path, required=True)
    phase7_run.add_argument("--lookahead", type=Path, required=True)
    phase7_run.add_argument("--phase6", type=Path, required=True)
    phase7_run.add_argument(
        "--reuse-validated-phase6-equivalence", action="store_true"
    )
    phase7_run.add_argument("--platform", type=Path, required=True)
    phase7_run.add_argument("--seed", type=int, required=True)
    phase7_run.add_argument("--workers", type=int, default=8)
    phase7_run.add_argument("--yosys")
    phase7_run.add_argument("--vpr")
    phase7_run.add_argument("--architecture-importer")
    phase7_run.add_argument("--packed-importer")
    phase7_run.add_argument("--route-checker")
    phase7_run.add_argument("--openparf-install", type=Path)
    phase7_run.add_argument("--openparf-python", type=Path)
    phase7_run.add_argument("--route-channel-width", type=int, default=300)
    phase7_run.add_argument("--managed-storage", action="store_true")
    phase7_run.add_argument("--managed-dag-node", action="store_true")
    phase7_run.add_argument("--out", type=Path, required=True)
    phase7_validate = experiment_stage_subparsers.add_parser(
        "phase7-validate", help="independently validate a Phase 7 checkpoint"
    )
    phase7_validate.add_argument("root", type=Path)
    phase7_validate.add_argument("--shared", type=Path, required=True)
    phase7_validate.add_argument("--lookahead", type=Path, required=True)
    phase7_validate.add_argument("--phase6", type=Path, required=True)
    phase7_validate.add_argument(
        "--reuse-validated-phase6-equivalence", action="store_true"
    )
    phase7_validate.add_argument("--platform", type=Path, required=True)
    phase7_validate.add_argument("--seed", type=int)
    phase7_validate.add_argument("--workers", type=int)
    phase7_validate.add_argument("--route-channel-width", type=int)
    phase7_validate.add_argument("--managed-dag-node", action="store_true")
    qor_compare_run = experiment_stage_subparsers.add_parser(
        "qor-compare-run",
        help="aggregate all nine canonical Phase 7 QoR arms",
    )
    qor_compare_run.add_argument("--shared", type=Path, required=True)
    qor_compare_run.add_argument(
        "--arm",
        nargs=3,
        action="append",
        required=True,
        metavar=("PROVIDER", "SEED", "ROOT"),
    )
    qor_compare_run.add_argument("--out", type=Path, required=True)
    qor_compare_validate = experiment_stage_subparsers.add_parser(
        "qor-compare-validate",
        help="independently rebuild a canonical nine-arm QoR comparison",
    )
    qor_compare_validate.add_argument("root", type=Path)
    qor_compare_validate.add_argument("--shared", type=Path, required=True)
    qor_compare_validate.add_argument(
        "--arm",
        nargs=3,
        action="append",
        required=True,
        metavar=("PROVIDER", "SEED", "ROOT"),
    )
    static_exact_qor_run = experiment_stage_subparsers.add_parser(
        "static-exact-qor-compare-run",
        help="compare sequential, legacy-v1, and generalized-v2 Phase 1-7 arms",
    )
    static_exact_qor_run.add_argument("--platform", type=Path, required=True)
    static_exact_qor_run.add_argument(
        "--arm",
        nargs=6,
        action="append",
        required=True,
        metavar=(
            "LABEL",
            "SEED",
            "SHARED",
            "LOOKAHEAD",
            "PHASE6",
            "PHASE7",
        ),
    )
    static_exact_qor_run.add_argument(
        "--reuse-validated-phase6-equivalence", action="store_true"
    )
    static_exact_qor_run.add_argument("--out", type=Path, required=True)
    static_exact_qor_validate = experiment_stage_subparsers.add_parser(
        "static-exact-qor-compare-validate",
        help="independently rebuild a three-policy Static Exact QoR comparison",
    )
    static_exact_qor_validate.add_argument("root", type=Path)
    static_exact_qor_validate.add_argument(
        "--platform", type=Path, required=True
    )
    static_exact_qor_validate.add_argument(
        "--arm",
        nargs=6,
        action="append",
        required=True,
        metavar=(
            "LABEL",
            "SEED",
            "SHARED",
            "LOOKAHEAD",
            "PHASE6",
            "PHASE7",
        ),
    )
    static_exact_qor_validate.add_argument(
        "--reuse-validated-phase6-equivalence", action="store_true"
    )

    platform_parser = subparsers.add_parser("platform", help="BoardDB operations")
    platform_subparsers = platform_parser.add_subparsers(
        dest="platform_command", required=True
    )
    platform_validate = platform_subparsers.add_parser(
        "validate", help="validate and summarize a BoardDB"
    )
    platform_validate.add_argument("path", type=Path)
    platform_validate.add_argument("--normalized-out", type=Path)
    platform_mps4 = platform_subparsers.add_parser(
        "arm-mps4-materialize",
        help="materialize Arm's documented three-MPS4 serial-link topology",
    )
    platform_mps4.add_argument("--output", "-o", type=Path, required=True)
    platform_mps4.add_argument("--name", default="arm_mps4_3board_ring")
    platform_mps4.add_argument(
        "--fabric-clock-mhz", type=float, required=True
    )
    platform_mps4.add_argument(
        "--payload-bits-per-lane-per-cycle", type=int, required=True
    )
    platform_mps4.add_argument(
        "--latency-cycles", type=int, required=True
    )
    platform_mps4.add_argument(
        "--utilization-limit", type=float, default=0.75
    )
    platform_overlay = platform_subparsers.add_parser(
        "overlay-validate",
        help="validate board-specific site, reference-clock, and reset bindings",
    )
    platform_overlay.add_argument("--platform", type=Path, required=True)
    platform_overlay.add_argument("--overlay", type=Path, required=True)
    platform_overlay.add_argument("--normalized-out", type=Path)
    platform_gt_sites = platform_subparsers.add_parser(
        "vivado-derive-gt-sites",
        help="derive GT sites from BoardDB package pins using a Vivado device DB",
    )
    platform_gt_sites.add_argument("--platform", type=Path, required=True)
    platform_gt_sites.add_argument("--vivado", type=Path, required=True)
    platform_gt_sites.add_argument("--out", type=Path, required=True)
    platform_link_model = platform_subparsers.add_parser(
        "link-timing-model",
        help="materialize explicit directed link-delay bounds from BoardDB",
    )
    platform_link_model.add_argument("--platform", type=Path, required=True)
    platform_link_model.add_argument("--output", "-o", type=Path, required=True)
    platform_link_validate = platform_subparsers.add_parser(
        "link-timing-validate",
        help="validate a characterized or measured BoardLinkTimingDB",
    )
    platform_link_validate.add_argument("--platform", type=Path, required=True)
    platform_link_validate.add_argument("--input", type=Path, required=True)

    phy_provider = subparsers.add_parser(
        "phy-provider", help="serial PHY provider and vendor recipe operations"
    )
    phy_provider_subparsers = phy_provider.add_subparsers(
        dest="phy_provider_command", required=True
    )
    phy_provider_validate = phy_provider_subparsers.add_parser(
        "validate", help="validate source inventory and BoardDB compatibility"
    )
    phy_provider_validate.add_argument("--manifest", type=Path, required=True)
    phy_provider_validate.add_argument("--platform", type=Path)
    phy_provider_validate.add_argument("--normalized-out", type=Path)
    phy_provider_elaborate = phy_provider_subparsers.add_parser(
        "elaborate", help="elaborate provider sources with generated FPGA shells"
    )
    phy_provider_elaborate.add_argument("--manifest", type=Path, required=True)
    phy_provider_elaborate.add_argument("--platform", type=Path, required=True)
    phy_provider_elaborate.add_argument("--phase6c-dir", type=Path, required=True)
    phy_provider_elaborate.add_argument(
        "--runtime-controller", type=Path, required=True
    )
    phy_provider_elaborate.add_argument(
        "--transport", action="append", default=[], metavar="FPGA=PATH"
    )
    elaborate_tool = phy_provider_elaborate.add_mutually_exclusive_group(
        required=True
    )
    elaborate_tool.add_argument("--yosys", type=Path)
    elaborate_tool.add_argument("--vivado", type=Path)
    phy_provider_elaborate.add_argument("--out", type=Path, required=True)
    phy_provider_materialize = phy_provider_subparsers.add_parser(
        "materialize-recipe",
        help="materialize a source-visible vendor GT recipe into a build directory",
    )
    phy_provider_materialize.add_argument(
        "--manifest", type=Path, required=True
    )
    phy_provider_materialize.add_argument("--part", required=True)
    phy_provider_materialize.add_argument("--vivado", type=Path, required=True)
    phy_provider_materialize.add_argument("--platform", type=Path)
    phy_provider_materialize.add_argument("--out", type=Path, required=True)

    runtime_sync = subparsers.add_parser(
        "runtime-sync",
        help="source-visible distributed runtime synchronization operations",
    )
    runtime_sync_subparsers = runtime_sync.add_subparsers(
        dest="runtime_sync_command", required=True
    )
    runtime_sync_validate = runtime_sync_subparsers.add_parser(
        "validate-provider",
        help="validate the runtime synchronization source inventory",
    )
    runtime_sync_validate.add_argument("--provider", type=Path, required=True)
    runtime_sync_materialize = runtime_sync_subparsers.add_parser(
        "materialize",
        help="build a deterministic synchronization tree and HDL testbench",
    )
    runtime_sync_materialize.add_argument("--platform", type=Path, required=True)
    runtime_sync_materialize.add_argument("--provider", type=Path, required=True)
    runtime_sync_materialize.add_argument("--root")
    runtime_sync_materialize.add_argument(
        "--ready-stable-cycles", type=int, default=4
    )
    runtime_sync_materialize.add_argument("--out", type=Path, required=True)

    contest_parser = subparsers.add_parser(
        "contest", help="public multi-FPGA contest format adapters"
    )
    contest_subparsers = contest_parser.add_subparsers(
        dest="contest_command", required=True
    )
    contest_matrix = contest_subparsers.add_parser(
        "matrix-validate",
        help="validate the versioned public contest qualification matrix",
    )
    contest_matrix.add_argument("matrix", type=Path)
    contest_fetch = contest_subparsers.add_parser(
        "fetch-public",
        help="fetch one hash-pinned public matrix case and validate provenance",
    )
    contest_fetch.add_argument("--matrix", type=Path, required=True)
    contest_fetch.add_argument("--case-id", required=True)
    contest_fetch.add_argument("--out", type=Path, required=True)
    contest_import = contest_subparsers.add_parser(
        "import-public",
        help="semantically import one pinned public matrix case",
    )
    contest_import.add_argument("--matrix", type=Path, required=True)
    contest_import.add_argument("--case-id", required=True)
    contest_import.add_argument("--source-dir", type=Path, required=True)
    contest_import.add_argument("--out", type=Path, required=True)
    contest_evaluate_public = contest_subparsers.add_parser(
        "evaluate-public",
        help="build a replayable sealed evaluation for one public matrix case",
    )
    contest_evaluate_public.add_argument("--matrix", type=Path, required=True)
    contest_evaluate_public.add_argument("--case-id", required=True)
    contest_evaluate_public.add_argument("--source-dir", type=Path, required=True)
    contest_evaluate_public.add_argument("--import-dir", type=Path, required=True)
    contest_evaluate_public.add_argument("--routes", type=Path)
    contest_evaluate_public.add_argument("--expected-routes-sha256")
    contest_evaluate_public.add_argument("--tdm-plan", type=Path)
    contest_evaluate_public.add_argument("--expected-tdm-plan-sha256")
    contest_evaluate_public.add_argument("--solution", type=Path)
    contest_evaluate_public.add_argument("--expected-solution-sha256")
    contest_evaluate_public.add_argument("--new-topology", type=Path)
    contest_evaluate_public.add_argument("--expected-topology-sha256")
    contest_evaluate_public.add_argument(
        "--runtime-seconds", type=float, default=0.0
    )
    contest_evaluate_public.add_argument("--out", type=Path, required=True)
    contest_validate_evaluation = contest_subparsers.add_parser(
        "validate-public-evaluation",
        help="independently replay and validate a sealed public evaluation",
    )
    contest_validate_evaluation.add_argument("--matrix", type=Path, required=True)
    contest_validate_evaluation.add_argument("bundle", type=Path)
    contest_boarddb = contest_subparsers.add_parser(
        "materialize-public-boarddb",
        help="materialize a passed public import on an RTL-capable FPGA template",
    )
    contest_boarddb.add_argument("--matrix", type=Path, required=True)
    contest_boarddb.add_argument("--case-id", required=True)
    contest_boarddb.add_argument("--source-dir", type=Path, required=True)
    contest_boarddb.add_argument("--import-dir", type=Path, required=True)
    contest_boarddb.add_argument("--device-template", type=Path, required=True)
    contest_boarddb.add_argument("--out", type=Path, required=True)
    contest_boarddb.add_argument("--lane-scale", type=int, default=1)
    contest_boarddb.add_argument("--unweighted-link-lanes", type=int, default=1)
    contest_boarddb.add_argument("--fabric-clock-mhz", type=float, default=50.0)
    contest_boarddb.add_argument("--latency-cycles", type=int, default=2)
    contest_farm = contest_subparsers.add_parser(
        "matrix-fetch-farm-spec",
        help="compile selected public fetch gates into a validation-farm spec",
    )
    contest_farm.add_argument("matrix", type=Path)
    contest_farm.add_argument("--source-commit", required=True)
    contest_farm.add_argument("--install-dir", type=Path, required=True)
    contest_farm.add_argument("--node", action="append", required=True)
    contest_farm.add_argument("--tier", action="append")
    contest_farm.add_argument("--suite", action="append")
    contest_farm.add_argument("--slots-per-node", type=int, default=1)
    contest_farm.add_argument(
        "--ssl-cert-file",
        type=Path,
        help="absolute CA bundle path to content-seal into fetch tasks",
    )
    contest_farm.add_argument("--farm-id", required=True)
    contest_farm.add_argument("--output", "-o", type=Path, required=True)
    contest_import_farm = contest_subparsers.add_parser(
        "matrix-import-farm-spec",
        help="compile passed public fetches into semantic import farm tasks",
    )
    contest_import_farm.add_argument("matrix", type=Path)
    contest_import_farm.add_argument("--fetch-farm", type=Path, required=True)
    contest_import_farm.add_argument("--source-commit", required=True)
    contest_import_farm.add_argument("--install-dir", type=Path, required=True)
    contest_import_farm.add_argument("--node", action="append", required=True)
    contest_import_farm.add_argument("--tier", action="append")
    contest_import_farm.add_argument("--suite", action="append")
    contest_import_farm.add_argument("--slots-per-node", type=int, default=1)
    contest_import_farm.add_argument("--farm-id", required=True)
    contest_import_farm.add_argument("--output", "-o", type=Path, required=True)
    contest_boarddb_farm = contest_subparsers.add_parser(
        "matrix-boarddb-farm-spec",
        help="compile passed public imports into BoardDB projection farm tasks",
    )
    contest_boarddb_farm.add_argument("matrix", type=Path)
    contest_boarddb_farm.add_argument("--fetch-farm", type=Path, required=True)
    contest_boarddb_farm.add_argument("--import-farm", type=Path, required=True)
    contest_boarddb_farm.add_argument("--source-commit", required=True)
    contest_boarddb_farm.add_argument("--install-dir", type=Path, required=True)
    contest_boarddb_farm.add_argument("--node", action="append", required=True)
    contest_boarddb_farm.add_argument("--tier", action="append")
    contest_boarddb_farm.add_argument("--suite", action="append")
    contest_boarddb_farm.add_argument("--slots-per-node", type=int, default=1)
    contest_boarddb_farm.add_argument("--lane-scale", type=int, default=1)
    contest_boarddb_farm.add_argument("--unweighted-link-lanes", type=int, default=1)
    contest_boarddb_farm.add_argument("--farm-id", required=True)
    contest_boarddb_farm.add_argument("--output", "-o", type=Path, required=True)
    contest_evaluation_farm = contest_subparsers.add_parser(
        "matrix-evaluate-farm-spec",
        help="compile passed public imports and frozen candidates into evaluation tasks",
    )
    contest_evaluation_farm.add_argument("matrix", type=Path)
    contest_evaluation_farm.add_argument("--fetch-farm", type=Path, required=True)
    contest_evaluation_farm.add_argument("--import-farm", type=Path, required=True)
    contest_evaluation_farm.add_argument(
        "--candidates-root", type=Path, required=True
    )
    contest_evaluation_farm.add_argument("--source-commit", required=True)
    contest_evaluation_farm.add_argument("--install-dir", type=Path, required=True)
    contest_evaluation_farm.add_argument("--node", action="append", required=True)
    contest_evaluation_farm.add_argument("--tier", action="append")
    contest_evaluation_farm.add_argument("--suite", action="append")
    contest_evaluation_farm.add_argument("--slots-per-node", type=int, default=1)
    contest_evaluation_farm.add_argument(
        "--runtime-seconds", type=float, default=0.0
    )
    contest_evaluation_farm.add_argument("--farm-id", required=True)
    contest_evaluation_farm.add_argument("--output", "-o", type=Path, required=True)
    eda2024_evaluate = contest_subparsers.add_parser(
        "eda2024-evaluate",
        help="independently check a 2024 logic-replication solution",
    )
    eda2024_evaluate.add_argument("--case-dir", type=Path, required=True)
    eda2024_evaluate.add_argument("--solution", type=Path)
    eda2024_evaluate.add_argument(
        "--runtime-seconds", type=float, default=0.0
    )
    eda2024_evaluate.add_argument("--output", "-o", type=Path)
    eda2024_boarddb = contest_subparsers.add_parser(
        "eda2024-materialize-boarddb",
        help="project a public unweighted topology using explicit abstract lanes",
    )
    eda2024_boarddb.add_argument("--case-dir", type=Path, required=True)
    eda2024_boarddb.add_argument("--device-template", type=Path, required=True)
    eda2024_boarddb.add_argument("--output", "-o", type=Path, required=True)
    eda2024_boarddb.add_argument("--route-constraints-output", type=Path)
    eda2024_boarddb.add_argument("--name", required=True)
    eda2024_boarddb.add_argument("--lanes-per-edge", type=int, required=True)
    eda2024_boarddb.add_argument("--template-fpga")
    eda2024_boarddb.add_argument("--fabric-clock-mhz", type=float, default=50.0)
    eda2024_boarddb.add_argument("--latency-cycles", type=int, default=2)
    eda2024_boarddb.add_argument(
        "--link-mode",
        choices=("abstract", "parallel", "serial", "source_synchronous"),
        default="abstract",
    )
    eda2023_import = contest_subparsers.add_parser(
        "eda2023-import",
        help="normalize an official 2023 die-level routing case",
    )
    eda2023_import.add_argument("--case-dir", type=Path, required=True)
    eda2023_import.add_argument("--name", required=True)
    eda2023_import.add_argument("--out", type=Path, required=True)
    eda2023_boarddb = contest_subparsers.add_parser(
        "eda2023-materialize-boarddb",
        help="project public die Wire banks onto RTL-capable physical FPGAs",
    )
    eda2023_boarddb.add_argument("--instance", type=Path, required=True)
    eda2023_boarddb.add_argument("--device-template", type=Path, required=True)
    eda2023_boarddb.add_argument("--output", "-o", type=Path, required=True)
    eda2023_boarddb.add_argument("--route-constraints-output", type=Path)
    eda2023_boarddb.add_argument("--name", required=True)
    eda2023_boarddb.add_argument("--template-fpga")
    eda2023_boarddb.add_argument("--lane-scale", type=int, default=1)
    eda2023_boarddb.add_argument("--fabric-clock-mhz", type=float, default=50.0)
    eda2023_boarddb.add_argument("--latency-cycles", type=int, default=2)
    eda2023_boarddb.add_argument(
        "--link-mode",
        choices=("abstract", "parallel", "serial", "source_synchronous"),
        default="abstract",
    )
    eda2023_optimize = contest_subparsers.add_parser(
        "eda2023-optimize",
        help="assign legal per-Wire TDM ratios to routed die trees",
    )
    eda2023_optimize.add_argument("--instance", type=Path, required=True)
    eda2023_optimize.add_argument("--routes", type=Path, required=True)
    eda2023_optimize.add_argument("--out", type=Path, required=True)
    eda2023_optimize.add_argument("--optimizer")
    eda2023_optimize.add_argument("--max-iterations", type=int, default=100)
    eda2023_optimize.add_argument(
        "--post-refinement-iterations", type=int, default=2000
    )
    eda2023_optimize.add_argument("--exact-domain-limit", type=int, default=2048)
    eda2023_evaluate = contest_subparsers.add_parser(
        "eda2023-evaluate",
        help="independently check routed trees and a per-Wire TDM plan",
    )
    eda2023_evaluate.add_argument("--instance", type=Path, required=True)
    eda2023_evaluate.add_argument("--routes", type=Path, required=True)
    eda2023_evaluate.add_argument("--tdm-plan", type=Path, required=True)
    eda2023_evaluate.add_argument("--output", "-o", type=Path)
    eda2023_chimew = contest_subparsers.add_parser(
        "eda2023-chimew-ab",
        help=(
            "compare Chimew with the previous placement-aware Phase 6 baseline "
            "on a frozen EDA 2023 routed-die result"
        ),
    )
    eda2023_chimew.add_argument("--import-dir", type=Path, required=True)
    eda2023_chimew.add_argument("--routes", type=Path, required=True)
    eda2023_chimew.add_argument("--tdm-plan", type=Path, required=True)
    eda2023_chimew.add_argument("--grouper")
    eda2023_chimew.add_argument("--refiner")
    eda2023_chimew.add_argument("--rudy")
    eda2023_chimew.add_argument("--assigner")
    eda2023_chimew.add_argument("--pin-planner")
    eda2023_chimew.add_argument("--out", type=Path, required=True)
    iccad2019_import = contest_subparsers.add_parser(
        "iccad2019-import",
        help="normalize an official ICCAD 2019 Problem B instance",
    )
    iccad2019_import.add_argument("--input", type=Path, required=True)
    iccad2019_import.add_argument("--name", required=True)
    iccad2019_import.add_argument("--out", type=Path, required=True)
    iccad2019_boarddb = contest_subparsers.add_parser(
        "iccad2019-materialize-boarddb",
        help="populate a Problem B FPGA graph with an RTL-capable device template",
    )
    iccad2019_boarddb.add_argument("--instance", type=Path, required=True)
    iccad2019_boarddb.add_argument("--device-template", type=Path, required=True)
    iccad2019_boarddb.add_argument("--output", "-o", type=Path, required=True)
    iccad2019_boarddb.add_argument("--name", required=True)
    iccad2019_boarddb.add_argument("--template-fpga")
    iccad2019_boarddb.add_argument("--lane-scale", type=int, default=1)
    iccad2019_boarddb.add_argument("--fabric-clock-mhz", type=float, default=50.0)
    iccad2019_boarddb.add_argument("--latency-cycles", type=int, default=2)
    iccad2019_boarddb.add_argument(
        "--link-mode",
        choices=("abstract", "parallel", "serial", "source_synchronous"),
        default="abstract",
    )
    iccad2019_optimize = contest_subparsers.add_parser(
        "iccad2019-optimize",
        help="assign exact-harmonic TDM ratios to EmuFlow routes",
    )
    iccad2019_optimize.add_argument("--instance", type=Path, required=True)
    iccad2019_optimize.add_argument("--routes", type=Path, required=True)
    iccad2019_optimize.add_argument("--output", "-o", type=Path, required=True)
    iccad2019_optimize.add_argument("--optimizer")
    iccad2019_optimize.add_argument("--max-iterations", type=int, default=500)
    iccad2019_optimize.add_argument(
        "--post-refinement-iterations", type=int, default=20000
    )
    iccad2019_evaluate = contest_subparsers.add_parser(
        "iccad2019-evaluate",
        help="independently check an official-format ICCAD 2019 solution",
    )
    iccad2019_evaluate.add_argument("--instance", type=Path, required=True)
    iccad2019_evaluate.add_argument("--solution", type=Path, required=True)
    iccad2019_evaluate.add_argument("--runtime-seconds", type=float)
    iccad2019_evaluate.add_argument("--median-runtime-seconds", type=float)
    eda2025_import = contest_subparsers.add_parser(
        "eda2025-import",
        help="normalize a 2025 EDA Elite routing benchmark",
    )
    eda2025_import.add_argument("--info", type=Path, required=True)
    eda2025_import.add_argument("--net", type=Path, required=True)
    eda2025_import.add_argument("--topology", type=Path, required=True)
    eda2025_import.add_argument("--assignment", type=Path, required=True)
    eda2025_import.add_argument("--name", required=True)
    eda2025_import.add_argument("--out", type=Path, required=True)
    eda2025_import.add_argument("--alpha-ns", type=float, default=0.7)
    eda2025_import.add_argument("--beta-ns", type=float, default=30.0)
    eda2025_import.add_argument("--ratio-quantum", type=int, default=8)
    eda2025_import.add_argument("--max-ratio", type=int, default=512)
    eda2025_import.add_argument(
        "--topology-change-fraction", type=float, default=0.3
    )
    eda2025_evaluate = contest_subparsers.add_parser(
        "eda2025-evaluate",
        help="independently score EmuFlow routes with the 2025 model",
    )
    eda2025_evaluate.add_argument("--instance", type=Path, required=True)
    eda2025_evaluate.add_argument("--routes", type=Path, required=True)
    eda2025_evaluate.add_argument("--new-topology", type=Path)
    eda2025_evaluate.add_argument("--runtime-seconds", type=float, default=0.0)
    eda2025_evaluate.add_argument("--output", "-o", type=Path)
    eda2025_evaluate.add_argument(
        "--official-out",
        type=Path,
        help="also write design.route.out and design.newtopo",
    )
    eda2025_topology = contest_subparsers.add_parser(
        "eda2025-optimize-topology",
        help="optimize channel counts and emit Phase 4 rerouting contracts",
    )
    eda2025_topology.add_argument("--instance", type=Path, required=True)
    eda2025_topology.add_argument("--routes", type=Path, required=True)
    eda2025_topology.add_argument("--out", type=Path, required=True)
    eda2025_topology.add_argument("--optimizer")
    eda2025_topology.add_argument("--max-changes", type=int)
    eda2025_topology.add_argument(
        "--topology",
        type=Path,
        help="current design.newtopo for a subsequent optimization round",
    )
    eda2025_routing = contest_subparsers.add_parser(
        "eda2025-optimize-routing",
        help="select topology candidates using real Phase 4 rerouting",
    )
    eda2025_routing.add_argument("--instance", type=Path, required=True)
    eda2025_routing.add_argument("--routes", type=Path, required=True)
    eda2025_routing.add_argument("--out", type=Path, required=True)
    eda2025_routing.add_argument("--topology", type=Path)
    eda2025_routing.add_argument("--router")
    eda2025_routing.add_argument("--topology-optimizer")
    eda2025_routing.add_argument("--max-rounds", type=int, default=4)
    eda2025_routing.add_argument(
        "--capacity-only", action="store_true", help="disable shortcut candidates"
    )
    eda2025_boarddb = contest_subparsers.add_parser(
        "eda2025-materialize-boarddb",
        help="populate a contest topology with an RTL-capable FPGA template",
    )
    eda2025_boarddb.add_argument("--instance", type=Path, required=True)
    eda2025_boarddb.add_argument("--device-template", type=Path, required=True)
    eda2025_boarddb.add_argument("--output", "-o", type=Path, required=True)
    eda2025_boarddb.add_argument("--route-constraints-output", type=Path)
    eda2025_boarddb.add_argument("--name", required=True)
    eda2025_boarddb.add_argument("--topology", type=Path)
    eda2025_boarddb.add_argument("--template-fpga")
    eda2025_boarddb.add_argument("--lane-scale", type=int, default=1)
    eda2025_boarddb.add_argument("--fabric-clock-mhz", type=float, default=50.0)
    eda2025_boarddb.add_argument("--latency-cycles", type=int, default=2)
    eda2025_boarddb.add_argument(
        "--link-mode",
        choices=("abstract", "parallel", "serial", "source_synchronous"),
        default="abstract",
    )
    eda2025_topology.add_argument(
        "--enable-shortcuts",
        action="store_true",
        help="also propose direct links; candidates still require rerouting",
    )

    ir_parser = subparsers.add_parser("ir", help="EmuIR operations")
    ir_subparsers = ir_parser.add_subparsers(dest="ir_command", required=True)
    ir_validate = ir_subparsers.add_parser("validate", help="validate an EmuIR")
    ir_validate.add_argument("path", type=Path)
    ir_stats = ir_subparsers.add_parser("stats", help="show EmuIR statistics")
    ir_stats.add_argument("path", type=Path)

    combinational_cut = subparsers.add_parser(
        "combinational-cut",
        help="characterize and validate static exact combinational-cut eligibility",
    )
    combinational_cut_subparsers = combinational_cut.add_subparsers(
        dest="combinational_cut_command", required=True
    )
    combinational_cut_characterize = combinational_cut_subparsers.add_parser(
        "characterize",
        help="write a read-only SCC, eligibility, and depth report",
    )
    combinational_cut_characterize.add_argument("--ir", type=Path, required=True)
    combinational_cut_characterize.add_argument(
        "--depth-limit",
        type=int,
        choices=(1, 2),
        action="append",
        default=[],
        help="dependency-depth upper bound to characterize (default: 1 and 2)",
    )
    combinational_cut_characterize.add_argument(
        "--output", "-o", type=Path, required=True
    )
    combinational_cut_validate = combinational_cut_subparsers.add_parser(
        "validate",
        help="independently reconstruct and validate a characterization report",
    )
    combinational_cut_validate.add_argument("report", type=Path)
    combinational_cut_validate.add_argument("--ir", type=Path, required=True)

    importer = subparsers.add_parser(
        "import-yosys", help="convert Yosys JSON to EmuIR"
    )
    importer.add_argument("input", type=Path)
    importer.add_argument("--output", "-o", type=Path, required=True)
    importer.add_argument("--top")
    importer.add_argument("--clock", action="append", default=[])

    synthesis = subparsers.add_parser(
        "synth-yosys", help="synthesize RTL to mapped Xilinx Yosys JSON"
    )
    synthesis.add_argument("sources", nargs="+", type=Path)
    synthesis.add_argument("--top", required=True)
    synthesis.add_argument("--output", "-o", type=Path, required=True)
    synthesis.add_argument(
        "--family",
        choices=sorted(VALID_XILINX_FAMILIES),
        default="xcup",
    )
    synthesis.add_argument(
        "--yosys",
        help="explicit comparison override; defaults to the in-tree build",
    )
    synthesis.add_argument("--log", type=Path)
    synthesis.add_argument(
        "--include-dir",
        action="append",
        default=[],
        type=Path,
        help="Verilog include directory passed to Yosys (repeatable)",
    )
    synthesis.add_argument(
        "--define",
        action="append",
        default=[],
        help="Verilog preprocessor NAME or NAME=VALUE (repeatable)",
    )
    synthesis.add_argument(
        "--verilog-output",
        type=Path,
        help="optional flattened mapped Verilog for downstream physical tools",
    )
    synthesis.add_argument(
        "--policy",
        choices=sorted(VALID_SYNTHESIS_POLICIES),
        default="native",
    )

    vpr_parser = subparsers.add_parser(
        "vpr", help="open VTR/VPR per-FPGA physical backend"
    )
    vpr_subparsers = vpr_parser.add_subparsers(
        dest="vpr_command", required=True
    )
    vpr_synth = vpr_subparsers.add_parser(
        "synth",
        help="map RTL to VTR-compatible eBLIF for VPR",
    )
    vpr_synth.add_argument("sources", nargs="+", type=Path)
    vpr_synth.add_argument("--top", required=True)
    vpr_synth.add_argument("--output", "-o", type=Path, required=True)
    vpr_synth.add_argument(
        "--yosys",
        help="explicit comparison override; defaults to the in-tree build",
    )
    vpr_synth.add_argument("--log", type=Path)
    vpr_synth.add_argument(
        "--hard-blocks",
        action="store_true",
        help=(
            "map multipliers and RAMs to the public VTR flagship "
            "architecture modes"
        ),
    )
    vpr_full_open = vpr_subparsers.add_parser(
        "fpga-open",
        aliases=["full-open"],
        help=(
            "run one FPGA's checked RTL-to-routed open physical backend "
            "('full-open' is a deprecated alias)"
        ),
    )
    vpr_full_open.add_argument("sources", nargs="+", type=Path)
    vpr_full_open.add_argument("--top", required=True)
    vpr_full_open.add_argument("--out", type=Path, required=True)
    vpr_full_open.add_argument(
        "--architecture",
        type=Path,
        help="optional VTR XML; otherwise fetch the pinned flagship model",
    )
    vpr_full_open.add_argument(
        "--architecture-id", default="vtr-flagship-k6-n10-40nm"
    )
    vpr_full_open.add_argument(
        "--logic-only",
        action="store_true",
        help="disable the default flagship multiplier/RAM hard-block mapping",
    )
    vpr_full_open.add_argument("--yosys")
    vpr_full_open.add_argument("--vpr")
    vpr_full_open.add_argument("--architecture-importer")
    vpr_full_open.add_argument("--packed-importer")
    vpr_full_open.add_argument("--route-checker")
    vpr_full_open.add_argument("--openparf-install", type=Path)
    vpr_full_open.add_argument("--openparf-python", type=Path)
    vpr_full_open.add_argument("--seed", type=int, default=1)
    vpr_full_open.add_argument(
        "--route-channel-width", type=int, default=300
    )

    multi_fpga = subparsers.add_parser(
        "multi-fpga",
        help="board-independent multi-FPGA compilation",
    )
    multi_fpga_subparsers = multi_fpga.add_subparsers(
        dest="multi_fpga_command", required=True
    )
    multi_fpga_compile = multi_fpga_subparsers.add_parser(
        "compile",
        help=(
            "run generic synthesis, partitioning, system routing, TDM, "
            "and per-FPGA split generation"
        ),
    )
    multi_fpga_validate = multi_fpga_subparsers.add_parser(
        "validate",
        help="rehash and independently replay a complete flow directory",
    )
    multi_fpga_validate.add_argument("--flow", type=Path, required=True)
    multi_fpga_validate.add_argument(
        "--minimum-combinational-cut-nets", type=int, default=0
    )
    multi_fpga_validate.add_argument(
        "--require-physical", action="store_true"
    )
    multi_fpga_compare = multi_fpga_subparsers.add_parser(
        "compare-routing-tdm",
        help=(
            "revalidate and compare frozen baseline/upgrade complete "
            "Phase-7 routing/TDM flows"
        ),
    )
    multi_fpga_compare.add_argument("--baseline", type=Path, required=True)
    multi_fpga_compare.add_argument("--upgrade", type=Path, required=True)
    multi_fpga_compare.add_argument("--output", type=Path, required=True)
    multi_fpga_scale_compare = multi_fpga_subparsers.add_parser(
        "compare-routing-tdm-scale",
        help="independently replay and compare frozen large Phase 4/5 arms",
    )
    for name in (
        "assignment", "platform", "route-constraints", "timing-paths", "baseline-route",
        "baseline-tdm", "upgrade-route", "upgrade-tdm", "output",
    ):
        multi_fpga_scale_compare.add_argument(
            f"--{name}", type=Path, required=True
        )
    multi_fpga_scale_compare.add_argument(
        "--baseline-runtime-seconds", type=float, required=True
    )
    multi_fpga_scale_compare.add_argument(
        "--upgrade-runtime-seconds", type=float, required=True
    )
    multi_fpga_finalize = multi_fpga_subparsers.add_parser(
        "finalize-physical",
        help=(
            "finish Phase 7C and seal a complete flow from a checked "
            "independently resumed physical checkpoint"
        ),
    )
    multi_fpga_finalize.add_argument("--flow", type=Path, required=True)
    multi_fpga_finalize.add_argument("--physical", type=Path, required=True)
    multi_fpga_finalize.add_argument(
        "--runtime-directory", default="runtime-final"
    )
    multi_fpga_compile.add_argument("sources", nargs="*", type=Path)
    multi_fpga_compile.add_argument("--top")
    multi_fpga_compile.add_argument("--clock", action="append", default=[])
    multi_fpga_compile.add_argument(
        "--yosys-json",
        type=Path,
        help="use existing Yosys JSON instead of synthesizing RTL",
    )
    multi_fpga_compile.add_argument("--platform", type=Path, required=True)
    multi_fpga_compile.add_argument("--out", type=Path, required=True)
    multi_fpga_compile.add_argument(
        "--archive-out",
        type=Path,
        help="archive a successful full-flow run to this separate directory",
    )
    multi_fpga_compile.add_argument(
        "--archive-run-id",
        help="archive identity (defaults to the flow output directory name)",
    )
    multi_fpga_compile.add_argument("--archive-source-commit")
    multi_fpga_compile.add_argument(
        "--archive-max-copy-bytes", type=int, default=DEFAULT_MAX_COPY_BYTES
    )
    multi_fpga_compile.add_argument(
        "--archive-tool-version",
        action="append",
        default=[],
        metavar="NAME=VERSION",
    )
    multi_fpga_compile.add_argument(
        "--archive-cleanup",
        action="store_true",
        help="remove --out only after the new archive passes its cleanup gate",
    )
    multi_fpga_compile.add_argument("--yosys")
    multi_fpga_compile.add_argument(
        "--mapping-profile",
        choices=("vtr-hard-blocks", "generic-soft"),
        default="vtr-hard-blocks",
        help=(
            "RTL mapping profile; the default preserves public VTR "
            "multiplier/RAM hard blocks"
        ),
    )
    multi_fpga_compile.add_argument("--partition-constraints", type=Path)
    multi_fpga_compile.add_argument(
        "--partition-provider",
        choices=(
            "repart-replication",
            "repart",
            "tritonpart",
            "mfspart",
            "patron",
            "greedy",
        ),
        default="patron",
    )
    multi_fpga_compile.add_argument("--seed", type=int, default=0)
    multi_fpga_compile.add_argument("--min-used-fpgas", type=int)
    multi_fpga_compile.add_argument("--balance-tolerance", type=float)
    multi_fpga_compile.add_argument("--openroad")
    multi_fpga_compile.add_argument("--repart")
    multi_fpga_compile.add_argument("--patron-refiner")
    multi_fpga_compile.add_argument("--patron-max-moves", type=int)
    multi_fpga_compile.add_argument(
        "--patron-flow-refinement", action="store_true"
    )
    multi_fpga_compile.add_argument(
        "--patron-algorithm-version",
        type=int,
        choices=(6, 9, 10, 11, 12, 13, 14),
        default=6,
    )
    multi_fpga_compile.add_argument(
        "--partition-timeout-seconds", type=int, default=3600
    )
    multi_fpga_compile.add_argument(
        "--partition-seed-attempts", type=int, default=1
    )
    multi_fpga_compile.add_argument(
        "--partition-num-initial-solutions", type=int, default=50
    )
    multi_fpga_compile.add_argument(
        "--partition-num-best-initial-solutions", type=int, default=10
    )
    multi_fpga_compile.add_argument(
        "--partition-repair-min-used-fpgas",
        action="store_true",
        help=(
            "minimally move legal atomic clusters if the partitioner "
            "leaves required FPGAs empty"
        ),
    )
    multi_fpga_compile.add_argument(
        "--partition-repair-balance",
        action=_BooleanOptionalAction,
        default=True,
        help=(
            "legalize a best-effort assignment against independently "
            "checked multi-resource balance bounds (enabled by default)"
        ),
    )
    multi_fpga_compile.add_argument(
        "--cut-mode",
        choices=("sequential-only", "static-exact-combinational"),
        default="static-exact-combinational",
        help=(
            "partition boundary semantics; defaults to dependency-qualified "
            "generalized Static Exact through Phase 4--7"
        ),
    )
    multi_fpga_compile.add_argument(
        "--max-cross-fpga-dependency-depth",
        type=int,
        default=STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH,
    )
    multi_fpga_compile.add_argument(
        "--static-exact-candidate-policy",
        choices=(
            STATIC_EXACT_CANDIDATE_FRONTIER_V1,
            STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
        ),
        default=STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    )
    multi_fpga_compile.add_argument(
        "--mfspart-post-refinement-timing-path-beta",
        type=float,
        default=DEFAULT_TIMING_PATH_BETA,
        help=(
            "weight of each distinct pre-partition timing path crossed by "
            "Static Exact Phase 3; identical cluster paths are aggregated"
        ),
    )
    multi_fpga_compile.add_argument(
        "--timing-driven",
        action=_BooleanOptionalAction,
        default=True,
        help=(
            "use the always-generated TimingPathDB to optimize partitioning, "
            "system routing, and TDM (enabled by default); "
            "--no-timing-driven retains TimingPathDB and final Phase 7C "
            "WNS/TNS but uses timing-oblivious Phase 3-5 baselines"
        ),
    )
    multi_fpga_compile.add_argument(
        "--timing-backend",
        choices=("opensta", "vivado"),
        default="opensta",
        help="produce the common TimingPathDB with OpenSTA or Vivado",
    )
    multi_fpga_compile.add_argument(
        "--clock-period",
        action="append",
        default=[],
        metavar="CLOCK=PERIOD_NS",
        help=(
            "required analyzed clock target; TimingPathDB is generated in "
            "both timing-driven and --no-timing-driven modes"
        ),
    )
    multi_fpga_compile.add_argument(
        "--timing-model",
        type=Path,
        default=DEFAULT_TIMING_MODEL,
    )
    multi_fpga_compile.add_argument(
        "--architecture-timing-db",
        type=Path,
        help=(
            "public VTR TimingDB used to construct the pre-placement "
            "OpenSTA model"
        ),
    )
    multi_fpga_compile.add_argument(
        "--opensta",
        "--openroad-sta",
        dest="opensta",
        help="explicit OpenSTA executable override",
    )
    multi_fpga_compile.add_argument("--timing-vivado")
    multi_fpga_compile.add_argument(
        "--sta-max-paths", type=int, default=200000
    )
    multi_fpga_compile.add_argument(
        "--timing-criticality-scale", type=float, default=9.0
    )
    multi_fpga_compile.add_argument(
        "--timing-criticality-exponent", type=float, default=2.0
    )
    multi_fpga_compile.add_argument("--route-constraints", type=Path)
    multi_fpga_compile.add_argument(
        "--board-link-timing-db",
        type=Path,
        help=(
            "apply versioned board-link delay bounds to routing/TDM "
            "optimization when enabled and always to final system timing"
        ),
    )
    multi_fpga_compile.add_argument(
        "--timing-paths",
        type=Path,
        help=(
            "externally projected paths for non-physical algorithm tests; "
            "normal compile runs generate a complete TimingPathDB instead"
        ),
    )
    multi_fpga_compile.add_argument("--router")
    multi_fpga_compile.add_argument(
        "--route-provider",
        choices=(
            NATIVE_ROUTER_PROVIDER,
            NATIVE_TIMING_EVALUATED_PROVIDER,
            TLR_PROVIDER,
            ROUTE_TDM_PROVIDER,
            GLOBAL_CANDIDATE_PROVIDER,
        ),
        help=(
            f"explicit Phase 4 provider; timing-enabled flows default to "
            f"{GLOBAL_CANDIDATE_PROVIDER}"
        ),
    )
    multi_fpga_compile.add_argument(
        "--route-candidate-workers",
        type=int,
        default=1,
        help="parallel deterministic candidate generators for global routing",
    )
    multi_fpga_compile.add_argument("--frame-slots", type=int)
    multi_fpga_compile.add_argument(
        "--optimize-frame-slots",
        action="store_true",
        help=(
            "treat --frame-slots as an upper bound and search for the "
            "minimum independently feasible route/TDM frame"
        ),
    )
    multi_fpga_compile.add_argument("--route-max-iterations", type=int)
    multi_fpga_compile.add_argument(
        "--tdm-provider",
        choices=(
            TDM_RATIO_PROVIDER,
            TDM_TIMING_DAG_RATIO_PROVIDER,
            TDM_BASELINE_PROVIDER,
        ),
        help=(
            f"explicit Phase 5 provider; timing-enabled flows default to "
            f"{TDM_TIMING_DAG_RATIO_PROVIDER}"
        ),
    )
    multi_fpga_compile.add_argument("--ratio-optimizer")
    multi_fpga_compile.add_argument("--timing-dag-optimizer")
    multi_fpga_compile.add_argument("--slot-optimizer")
    multi_fpga_compile.add_argument(
        "--ratio-max-iterations", type=int, default=500
    )
    multi_fpga_compile.add_argument("--max-ratio", type=int)
    multi_fpga_compile.add_argument(
        "--ratio-quantum", type=int, default=8
    )
    multi_fpga_compile.add_argument(
        "--post-refinement-iterations", type=int, default=200
    )
    multi_fpga_compile.add_argument(
        "--slot-refinement-iterations",
        type=int,
        default=None,
        help=(
            "explicit slot-refinement iteration count; defaults to 0 for "
            "static exact combinational cuts and 200 otherwise"
        ),
    )
    multi_fpga_compile.add_argument(
        "--cross-stage-iterations",
        type=int,
        default=0,
        help=(
            "run checked Phase 3--5 TDM-feedback optimization and continue "
            "its selected candidate through split, physical, and runtime"
        ),
    )
    multi_fpga_compile.add_argument("--cross-stage-feedback-optimizer")
    multi_fpga_compile.add_argument(
        "--cross-stage-pair-pressure-weight", type=float, default=1.0
    )
    multi_fpga_compile.add_argument("--simulation-frames", type=int, default=16)
    multi_fpga_compile.add_argument("--equivalence-cycles", type=int, default=16)
    multi_fpga_compile.add_argument(
        "--equivalence-seed", type=int, default=20260727
    )
    multi_fpga_compile.add_argument(
        "--phase6-provider",
        choices=("auto", "chimew", "baseline"),
        default="baseline",
        help=(
            "Phase 6 algorithm; baseline is the stable default, while an "
            "explicit auto selects academic Chimew for an open physical "
            "run with scheduled crossings"
        ),
    )
    multi_fpga_compile.add_argument(
        "--phase6-chimew-region-count", type=int, default=4
    )
    multi_fpga_compile.add_argument("--phase6-chimew-grouper")
    multi_fpga_compile.add_argument("--phase6-chimew-refiner")
    multi_fpga_compile.add_argument("--phase6-chimew-rudy")
    multi_fpga_compile.add_argument("--phase6-chimew-assigner")
    multi_fpga_compile.add_argument(
        "--physical",
        action="store_true",
        help=(
            "continue every Phase-6 partition through the selected physical "
            "backend and the common physical-QoR gate"
        ),
    )
    multi_fpga_compile.add_argument(
        "--physical-backend",
        choices=("open", "vivado"),
        default="open",
        help="select the provider behind the common physical/timing contract",
    )
    multi_fpga_compile.add_argument("--physical-architecture", type=Path)
    multi_fpga_compile.add_argument(
        "--physical-architecture-id", default="vtr-flagship-k6-n10-40nm"
    )
    multi_fpga_compile.add_argument("--physical-vpr")
    multi_fpga_compile.add_argument("--physical-architecture-importer")
    multi_fpga_compile.add_argument("--physical-packed-importer")
    multi_fpga_compile.add_argument("--physical-route-checker")
    multi_fpga_compile.add_argument("--physical-openparf-install", type=Path)
    multi_fpga_compile.add_argument("--physical-openparf-python", type=Path)
    multi_fpga_compile.add_argument("--physical-seed", type=int, default=1)
    multi_fpga_compile.add_argument(
        "--physical-route-channel-width", type=int, default=300
    )
    multi_fpga_compile.add_argument("--physical-vivado")
    multi_fpga_compile.add_argument(
        "--physical-vivado-max-timing-paths", type=int, default=10000
    )
    multi_fpga_compile.add_argument(
        "--physical-vivado-place-directive", default="Default"
    )
    multi_fpga_compile.add_argument(
        "--physical-vivado-route-directive", default="Default"
    )
    multi_fpga_compile.add_argument(
        "--physical-workers",
        type=int,
        default=1,
        help="run independent per-FPGA physical backends concurrently",
    )
    multi_fpga_compile.add_argument(
        "--serial-bsp-phy-provider",
        type=Path,
        help=(
            "continue the completed compile through serial hardware-BSP "
            "generation using this provider manifest"
        ),
    )
    multi_fpga_compile.add_argument(
        "--serial-bsp-runtime-sync-provider", type=Path
    )
    multi_fpga_compile.add_argument("--serial-bsp-board-overlay", type=Path)
    multi_fpga_compile.add_argument("--serial-bsp-gt-site-map", type=Path)
    multi_fpga_compile.add_argument("--serial-bsp-vivado", type=Path)
    multi_fpga_compile.add_argument("--serial-bsp-yosys", type=Path)
    multi_fpga_compile.add_argument("--serial-bsp-runtime-sync-root")
    multi_fpga_compile.add_argument(
        "--serial-bsp-ready-stable-cycles", type=int, default=4
    )
    multi_fpga_physical = multi_fpga_subparsers.add_parser(
        "physical",
        help=(
            "implement every Phase-6 partition through the selected backend "
            "and emit checked common physical timing"
        ),
    )
    multi_fpga_physical.add_argument("--split", type=Path, required=True)
    multi_fpga_physical.add_argument("--platform", type=Path, required=True)
    multi_fpga_physical.add_argument("--schedule", type=Path, required=True)
    multi_fpga_physical.add_argument("--out", type=Path, required=True)
    multi_fpga_physical.add_argument(
        "--original-ir",
        type=Path,
        help="original EmuIR used to map routed DUT timing endpoints",
    )
    multi_fpga_physical.add_argument(
        "--assignment",
        type=Path,
        help="Phase 3 assignment used to reconstruct DUT timing paths",
    )
    multi_fpga_physical.add_argument(
        "--routes",
        type=Path,
        help="Phase 4 routes used to reconstruct DUT timing paths",
    )
    multi_fpga_physical.add_argument(
        "--path-database",
        type=Path,
        help="complete pre-partition STA path database for local paths",
    )
    multi_fpga_physical.add_argument(
        "--logic-path-database",
        type=Path,
        help=(
            "through-cut STA path database whose member identities feed "
            "Phase-4 logic-segment reconstruction"
        ),
    )
    multi_fpga_physical.add_argument(
        "--backend", choices=("open", "vivado"), default="open"
    )
    multi_fpga_physical.add_argument("--architecture", type=Path)
    multi_fpga_physical.add_argument(
        "--architecture-id", default="vtr-flagship-k6-n10-40nm"
    )
    multi_fpga_physical.add_argument("--yosys")
    multi_fpga_physical.add_argument("--vpr")
    multi_fpga_physical.add_argument("--architecture-importer")
    multi_fpga_physical.add_argument("--packed-importer")
    multi_fpga_physical.add_argument("--route-checker")
    multi_fpga_physical.add_argument("--openparf-install", type=Path)
    multi_fpga_physical.add_argument("--openparf-python", type=Path)
    multi_fpga_physical.add_argument("--seed", type=int, default=1)
    multi_fpga_physical.add_argument(
        "--route-channel-width", type=int, default=300
    )
    multi_fpga_physical.add_argument("--vivado")
    multi_fpga_physical.add_argument(
        "--vivado-max-timing-paths", type=int, default=10000
    )
    multi_fpga_physical.add_argument(
        "--vivado-place-directive", default="Default"
    )
    multi_fpga_physical.add_argument(
        "--vivado-route-directive", default="Default"
    )
    multi_fpga_physical.add_argument(
        "--workers",
        type=int,
        default=1,
        help="run independent per-FPGA physical backends concurrently",
    )
    multi_fpga_physical.add_argument(
        "--resume",
        action="store_true",
        help=(
            "reuse only independently hash-validated VPR pack/place "
            "checkpoints before continuing OpenPARF and detailed routing"
        ),
    )
    multi_fpga_bsp = multi_fpga_subparsers.add_parser(
        "bsp",
        help=(
            "continue a completed compile through serial Phase 6B/6C, "
            "runtime synchronization, and checked PHY elaboration"
        ),
    )
    multi_fpga_bsp.add_argument("--flow", type=Path, required=True)
    multi_fpga_bsp.add_argument("--platform", type=Path, required=True)
    multi_fpga_bsp.add_argument("--phy-provider", type=Path, required=True)
    multi_fpga_bsp.add_argument(
        "--runtime-sync-provider", type=Path, required=True
    )
    multi_fpga_bsp.add_argument("--board-overlay", type=Path)
    multi_fpga_bsp.add_argument("--gt-site-map", type=Path)
    multi_fpga_bsp.add_argument("--vivado", type=Path)
    multi_fpga_bsp.add_argument("--yosys", type=Path)
    multi_fpga_bsp.add_argument("--runtime-sync-root")
    multi_fpga_bsp.add_argument(
        "--ready-stable-cycles", type=int, default=4
    )
    multi_fpga_bsp.add_argument("--out", type=Path, required=True)
    multi_fpga_board = multi_fpga_subparsers.add_parser(
        "board-implement",
        help=(
            "place and route each Vivado DUT+transport partition together "
            "with its source-bound serial BSP"
        ),
    )
    multi_fpga_board.add_argument("--flow", type=Path, required=True)
    multi_fpga_board.add_argument("--bsp", type=Path, required=True)
    multi_fpga_board.add_argument("--platform", type=Path, required=True)
    multi_fpga_board.add_argument("--phy-provider", type=Path, required=True)
    multi_fpga_board.add_argument("--vivado", type=Path, required=True)
    multi_fpga_board.add_argument("--place-directive", default="Default")
    multi_fpga_board.add_argument("--route-directive", default="Default")
    multi_fpga_board.add_argument("--write-bitstream", action="store_true")
    multi_fpga_board.add_argument("--out", type=Path, required=True)
    multi_fpga_board_validate = multi_fpga_subparsers.add_parser(
        "board-validate",
        help="rehash and validate a relocatable Vivado board-flow bundle",
    )
    multi_fpga_board_validate.add_argument("board", type=Path)
    multi_fpga_board_timing = multi_fpga_subparsers.add_parser(
        "board-timing",
        help=(
            "export routed board-checkpoint logic/boundary timing and "
            "rebuild unified Phase 7C timing"
        ),
    )
    multi_fpga_board_timing.add_argument("--flow", type=Path, required=True)
    multi_fpga_board_timing.add_argument("--board", type=Path, required=True)
    multi_fpga_board_timing.add_argument("--platform", type=Path, required=True)
    multi_fpga_board_timing.add_argument("--vivado", type=Path, required=True)
    multi_fpga_board_timing.add_argument(
        "--hierarchy-prefix", default="mapped_partition"
    )
    multi_fpga_board_timing.add_argument("--workers", type=int, default=3)
    multi_fpga_board_timing.add_argument("--resume", action="store_true")
    multi_fpga_board_timing.add_argument("--link-timing-db", type=Path)
    multi_fpga_board_timing.add_argument("--out", type=Path, required=True)
    vpr_run = vpr_subparsers.add_parser(
        "run",
        help="run exact VPR pack, baseline place, route, and analysis",
    )
    vpr_run.add_argument("--architecture", type=Path, required=True)
    vpr_run.add_argument("--circuit", type=Path, required=True)
    vpr_run.add_argument("--out", type=Path, required=True)
    vpr_run.add_argument(
        "--vpr",
        help="explicit comparison override; defaults to the in-tree build",
    )
    vpr_run.add_argument("--seed", type=int, default=1)
    vpr_run.add_argument("--route-channel-width", type=int, default=300)
    vpr_import_packed = vpr_subparsers.add_parser(
        "import-packed",
        help="import VPR .net packing decisions into the versioned contract",
    )
    vpr_import_packed.add_argument("--input", type=Path, required=True)
    vpr_import_packed.add_argument("--output", "-o", type=Path, required=True)
    vpr_import_packed.add_argument("--architecture", type=Path)
    vpr_import_packed.add_argument("--circuit", type=Path)
    vpr_import_packed.add_argument(
        "--importer",
        help="explicit comparison override; defaults to the in-tree build",
    )
    vpr_validate_packed = vpr_subparsers.add_parser(
        "validate-packed",
        help="independently validate a VPR packed-netlist contract",
    )
    vpr_validate_packed.add_argument("--input", type=Path, required=True)
    vpr_validate_packed.add_argument("--architecture", type=Path)
    vpr_validate_packed.add_argument("--circuit", type=Path)
    vpr_place_openparf = vpr_subparsers.add_parser(
        "place-openparf",
        help="place VPR packed clusters with root-built OpenPARF",
    )
    vpr_place_openparf.add_argument("--packed", type=Path, required=True)
    vpr_place_openparf.add_argument(
        "--architecture-db", type=Path, required=True
    )
    vpr_place_openparf.add_argument("--seed-placement", type=Path)
    vpr_place_openparf.add_argument("--out", type=Path, required=True)
    vpr_place_openparf.add_argument("--openparf-install", type=Path)
    vpr_place_openparf.add_argument("--openparf-python", type=Path)
    vpr_route_packed = vpr_subparsers.add_parser(
        "route-packed",
        help="route a packed netlist and OpenPARF VPR placement",
    )
    vpr_route_packed.add_argument("--architecture", type=Path, required=True)
    vpr_route_packed.add_argument("--circuit", type=Path, required=True)
    vpr_route_packed.add_argument("--packed-netlist", type=Path, required=True)
    vpr_route_packed.add_argument(
        "--packed-contract", type=Path, required=True
    )
    vpr_route_packed.add_argument("--placement", type=Path, required=True)
    vpr_route_packed.add_argument("--out", type=Path, required=True)
    vpr_route_packed.add_argument(
        "--vpr",
        help="explicit comparison override; defaults to the in-tree build",
    )
    vpr_route_packed.add_argument(
        "--route-channel-width", type=int, default=300
    )
    vpr_route_packed.add_argument("--route-checker")
    vpr_validate_route = vpr_subparsers.add_parser(
        "validate-route",
        help="independently check VPR route and RR-graph artifacts",
    )
    vpr_validate_route.add_argument("--route", type=Path, required=True)
    vpr_validate_route.add_argument("--rr-graph", type=Path, required=True)
    vpr_validate_route.add_argument(
        "--packed-contract", type=Path, required=True
    )
    vpr_validate_route.add_argument("--placement", type=Path, required=True)
    vpr_validate_route.add_argument("--output", "-o", type=Path, required=True)
    vpr_validate_route.add_argument("--checker")

    benchmark = subparsers.add_parser(
        "benchmark", help="run a pinned RTL benchmark through Phase 1"
    )
    benchmark.add_argument("spec", type=Path)
    benchmark.add_argument("--source-root", type=Path, required=True)
    benchmark.add_argument("--out", type=Path, required=True)
    benchmark.add_argument(
        "--yosys",
        help="explicit comparison override; defaults to the in-tree build",
    )
    benchmark_matrix = subparsers.add_parser(
        "benchmark-matrix-validate",
        help=(
            "validate canonical real-RTL x contest-BoardDB Phase 1-7 cases"
        ),
    )
    benchmark_matrix.add_argument("matrix", type=Path)
    benchmark_experiment = subparsers.add_parser(
        "benchmark-experiment-compile",
        help="compile one canonical real-RTL Phase 1-7 experiment DAG",
    )
    benchmark_experiment.add_argument("--config", type=Path, required=True)
    benchmark_experiment.add_argument("--repository-root", type=Path, required=True)
    benchmark_experiment.add_argument("--out", type=Path, required=True)
    partition_qualification = subparsers.add_parser(
        "benchmark-partition-experiment-compile",
        help="compile one canonical real-RTL Phase 1-3 partition qualification DAG",
    )
    partition_qualification.add_argument("--config", type=Path, required=True)
    partition_qualification.add_argument(
        "--repository-root", type=Path, required=True
    )
    partition_qualification.add_argument("--out", type=Path, required=True)
    static_exact_experiment = subparsers.add_parser(
        "benchmark-static-exact-ab-compile",
        help=(
            "compile one content-addressed sequential/v1/v2 Static Exact "
            "Phase 1-7 comparison DAG"
        ),
    )
    static_exact_experiment.add_argument("--config", type=Path, required=True)
    static_exact_experiment.add_argument(
        "--repository-root", type=Path, required=True
    )
    static_exact_experiment.add_argument("--legacy-max-depth", type=int, default=2)
    static_exact_experiment.add_argument(
        "--generalized-max-depth", type=int, default=8
    )
    static_exact_experiment.add_argument(
        "--minimum-combinational-cut-nets", type=int, default=1
    )
    static_exact_experiment.add_argument(
        "--partition-seed",
        type=int,
        help=(
            "single controlled Phase 3 seed shared by all A/B arms; "
            "defaults to partition_seed in the canonical config"
        ),
    )
    static_exact_experiment.add_argument("--out", type=Path, required=True)

    phase1 = subparsers.add_parser(
        "phase1", help="run the board-independent Phase 1 pipeline"
    )
    phase1.add_argument("--yosys-json", type=Path, required=True)
    phase1.add_argument("--platform", type=Path, required=True)
    phase1.add_argument("--out", type=Path, required=True)
    phase1.add_argument("--top")
    phase1.add_argument("--clock", action="append", default=[])
    phase1.add_argument(
        "--require-no-fabric-clock",
        action="store_true",
        help="fail when a LUT output drives an FD*.C clock pin",
    )

    arch_parser = subparsers.add_parser(
        "arch", help="provider-neutral ArchitectureDB operations"
    )
    arch_subparsers = arch_parser.add_subparsers(
        dest="arch_command", required=True
    )
    arch_validate = arch_subparsers.add_parser(
        "validate", help="validate and summarize an ArchitectureDB"
    )
    arch_validate.add_argument("path", type=Path)
    arch_import = arch_subparsers.add_parser(
        "import-vivado-tsv", help="import Vivado Site/BEL inventory TSV"
    )
    arch_import.add_argument("input", type=Path)
    arch_import.add_argument("--output", "-o", type=Path, required=True)
    arch_import_fpgaif = arch_subparsers.add_parser(
        "import-fpga-interchange",
        help="import open FPGA Interchange DeviceResources",
    )
    arch_import_fpgaif.add_argument("input", type=Path)
    arch_import_fpgaif.add_argument("--part", required=True)
    arch_import_fpgaif.add_argument(
        "--generator",
        required=True,
        help="declared producer and version of the DeviceResources input",
    )
    arch_import_fpgaif.add_argument(
        "--output", "-o", type=Path, required=True
    )
    arch_import_fpgaif.add_argument(
        "--native",
        help="explicit comparison override; defaults to the in-tree build",
    )
    arch_import_fpgaif.add_argument("--log", type=Path)
    arch_import_vtr = arch_subparsers.add_parser(
        "import-vtr",
        help="import an open VTR academic architecture XML",
    )
    arch_import_vtr.add_argument("input", type=Path)
    arch_import_vtr.add_argument("--architecture-id", required=True)
    arch_import_vtr.add_argument("--width", type=int)
    arch_import_vtr.add_argument("--height", type=int)
    arch_import_vtr.add_argument(
        "--reference-placement",
        type=Path,
        help="derive exact auto-layout dimensions from a VPR .place file",
    )
    arch_import_vtr.add_argument(
        "--architecture-output", type=Path, required=True
    )
    arch_import_vtr.add_argument(
        "--timing-output", type=Path, required=True
    )
    arch_import_vtr.add_argument("--source-url")
    arch_import_vtr.add_argument(
        "--native",
        help="explicit comparison override; defaults to the in-tree build",
    )
    arch_fetch_vtr = arch_subparsers.add_parser(
        "fetch-default-vtr",
        help="fetch and verify the pinned open VTR flagship architecture",
    )
    arch_fetch_vtr.add_argument("--output", "-o", type=Path, required=True)
    arch_validate_vtr = arch_subparsers.add_parser(
        "validate-vtr",
        help="validate a VTR-sourced ArchitectureDB",
    )
    arch_validate_vtr.add_argument("path", type=Path)
    arch_validate_vtr_timing = arch_subparsers.add_parser(
        "validate-vtr-timing",
        help="validate a VTR academic TimingDB",
    )
    arch_validate_vtr_timing.add_argument("path", type=Path)
    arch_validate_fpgaif = arch_subparsers.add_parser(
        "validate-fpga-interchange",
        help="independently validate FPGA Interchange ArchitectureDB metadata",
    )
    arch_validate_fpgaif.add_argument("path", type=Path)
    arch_capacity_fpgaif = arch_subparsers.add_parser(
        "check-capacity",
        help="check EmuIR primitive support and BEL capacity",
    )
    arch_capacity_fpgaif.add_argument("--arch", type=Path, required=True)
    arch_capacity_fpgaif.add_argument("--ir", type=Path, required=True)
    arch_merge_regions = arch_subparsers.add_parser(
        "merge-physical-regions",
        help="merge a source-qualified physical-region sidecar",
    )
    arch_merge_regions.add_argument("--arch", type=Path, required=True)
    arch_merge_regions.add_argument("--sidecar", type=Path, required=True)
    arch_merge_regions.add_argument("--output", "-o", type=Path, required=True)
    arch_validate_regions = arch_subparsers.add_parser(
        "validate-physical-regions",
        help="validate merged SLR, clock-region, and I/O-bank metadata",
    )
    arch_validate_regions.add_argument("path", type=Path)

    placement_parser = subparsers.add_parser(
        "placement", help="physical placement operations"
    )
    placement_subparsers = placement_parser.add_subparsers(
        dest="placement_command", required=True
    )
    placement_validate = placement_subparsers.add_parser(
        "validate", help="validate a placement against ArchitectureDB and EmuIR"
    )
    placement_validate.add_argument("path", type=Path)
    placement_validate.add_argument("--arch", type=Path, required=True)
    placement_validate.add_argument("--ir", type=Path)
    placement_import = placement_subparsers.add_parser(
        "import-openparf", help="convert OpenPARF .pl to legal Site/BEL placement"
    )
    placement_import.add_argument("input", type=Path)
    placement_import.add_argument("--arch", type=Path, required=True)
    placement_import.add_argument("--ir", type=Path, required=True)
    placement_import.add_argument("--output", "-o", type=Path, required=True)
    placement_import.add_argument("--xdc", type=Path)

    phase2 = subparsers.add_parser(
        "phase2", help="run the UltraScale+ physical-backend risk spike"
    )
    phase2.add_argument("--ir", type=Path, required=True)
    phase2.add_argument("--arch", type=Path, required=True)
    phase2.add_argument("--out", type=Path, required=True)
    phase2.add_argument(
        "--openparf-result",
        type=Path,
        help="explicit comparison/import .pl; default runs root-built OpenPARF",
    )
    phase2.add_argument(
        "--openparf-global-result",
        action="store_true",
        help=(
            "treat --openparf-result as global coordinates and legalize them "
            "onto exact ArchitectureDB Site/BEL slots"
        ),
    )
    phase2.add_argument(
        "--site-utilization-limit",
        type=float,
        default=0.75,
        help="maximum fraction of compatible slots exposed per site",
    )
    phase2.add_argument(
        "--site-y-range",
        type=int,
        nargs=2,
        metavar=("MIN_Y", "MAX_Y"),
        help=(
            "affinely map OpenPARF y coordinates into this inclusive "
            "ArchitectureDB region (for example, one SLR)"
        ),
    )
    phase2.add_argument(
        "--openparf-install",
        type=Path,
        help="explicit comparison override for an OpenPARF install root",
    )
    phase2.add_argument(
        "--openparf-python",
        type=Path,
        help=(
            "Python used to load root-built OpenPARF; defaults to the "
            "interpreter recorded by the root CMake build"
        ),
    )
    phase2.add_argument(
        "--reference-placement",
        action="store_true",
        help="use the deterministic greedy adapter reference in tests only",
    )

    partition_parser = subparsers.add_parser(
        "partition", help="multi-FPGA partition artifact operations"
    )
    partition_subparsers = partition_parser.add_subparsers(
        dest="partition_command", required=True
    )
    partition_validate = partition_subparsers.add_parser(
        "validate", help="independently validate Phase 3 partition artifacts"
    )
    partition_validate.add_argument("assignment", type=Path)
    partition_validate.add_argument("--clusters", type=Path, required=True)
    partition_validate.add_argument("--ir", type=Path, required=True)
    partition_validate.add_argument("--platform", type=Path, required=True)

    phase3 = subparsers.add_parser(
        "phase3",
        help=(
            "run multi-FPGA partitioning with the default generalized "
            "Static Exact v2 + PATRON policy or an explicit comparison policy"
        ),
    )
    phase3.add_argument("--ir", type=Path, required=True)
    phase3.add_argument("--platform", type=Path, required=True)
    phase3.add_argument("--out", type=Path, required=True)
    phase3.add_argument("--constraints", type=Path)
    phase3.add_argument(
        "--route-constraints",
        type=Path,
        help=(
            "optional system-route constraints; max_route_hops activates "
            "topology-constrained Phase 3 refinement"
        ),
    )
    phase3.add_argument("--seed", type=int, default=0)
    phase3.add_argument(
        "--cut-mode",
        choices=("sequential-only", "static-exact-combinational"),
        default="static-exact-combinational",
        help=(
            "Phase 3 cut legality; defaults to generalized Static Exact v2"
        ),
    )
    phase3.add_argument(
        "--max-cross-fpga-dependency-depth",
        type=int,
        default=STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH,
        help="static exact mode dependency-depth limit",
    )
    phase3.add_argument(
        "--static-exact-candidate-policy",
        choices=(
            STATIC_EXACT_CANDIDATE_FRONTIER_V1,
            STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
        ),
        default=STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    )
    phase3.add_argument("--min-used-fpgas", type=int)
    phase3.add_argument("--balance-tolerance", type=float)
    phase3.add_argument(
        "--provider",
        choices=(
            "repart-replication",
            "repart",
            "tritonpart",
            "mfspart",
            "patron",
            "greedy",
        ),
        default="patron",
        help="partition provider (default: patron)",
    )
    phase3.add_argument(
        "--openroad",
        help=(
            "explicit comparison override; defaults to the in-tree "
            "OpenROAD/TritonPart build"
        ),
    )
    phase3.add_argument(
        "--tritonpart-solution",
        type=Path,
        help="import a precomputed TritonPart .part file instead of executing",
    )
    phase3.add_argument(
        "--net-weights",
        type=Path,
        help="optional emuflow.partition-net-weights/v1 JSON",
    )
    phase3.add_argument(
        "--tritonpart-timeout-seconds",
        type=int,
        default=3600,
    )
    phase3.add_argument(
        "--tritonpart-seed-attempts",
        type=int,
        default=1,
        help=(
            "evaluate consecutive deterministic seeds and select the "
            "lowest independently legal weighted-cut objective"
        ),
    )
    phase3.add_argument(
        "--tritonpart-num-initial-solutions", type=int, default=50
    )
    phase3.add_argument(
        "--tritonpart-num-best-initial-solutions", type=int, default=10
    )
    phase3.add_argument(
        "--tritonpart-repair-min-used-fpgas",
        action="store_true",
        help=(
            "minimally move the smallest legal atomic clusters when the "
            "provider leaves required partitions empty"
        ),
    )
    phase3.add_argument(
        "--tritonpart-repair-balance",
        action="store_true",
        help=(
            "legalize a best-effort TritonPart solution against EmuFlow's "
            "independently checked multi-resource upper bounds"
        ),
    )
    phase3.add_argument(
        "--repart",
        help=(
            "explicit comparison override; defaults to the in-tree "
            "RePart build"
        ),
    )
    phase3.add_argument(
        "--repart-solution",
        type=Path,
        help=(
            "import a precomputed RePart solution; '*' records are accepted "
            "only by --provider repart-replication"
        ),
    )
    phase3.add_argument(
        "--repart-timeout-seconds",
        type=int,
        default=3600,
    )
    phase3.add_argument(
        "--hop-refiner",
        help=(
            "explicit comparison override; defaults to the in-tree C++ "
            "topology-constrained FM refiner"
        ),
    )
    phase3.add_argument("--mfspart-coarsener")
    phase3.add_argument("--mfspart-initializer")
    phase3.add_argument("--mfspart-refiner")
    phase3.add_argument("--mfspart-refiner-checker")
    phase3.add_argument("--mfspart-legalizer")
    phase3.add_argument(
        "--timing-database",
        type=Path,
        help="complete TimingPathDB required by --provider patron",
    )
    phase3.add_argument("--patron-refiner")
    phase3.add_argument("--patron-max-moves", type=int)
    phase3.add_argument("--patron-flow-refinement", action="store_true")
    phase3.add_argument(
        "--patron-algorithm-version",
        type=int,
        choices=(6, 9, 10, 11, 12, 13, 14),
        default=6,
    )
    phase3.add_argument("--patron-initial-assignment", type=Path)
    phase3.add_argument("--patron-initial-clusters", type=Path)
    phase3.add_argument("--patron-physical-system-timing", type=Path)
    phase3.add_argument(
        "--patron-physical-feedback-scale", type=float, default=0.0
    )
    phase3.add_argument(
        "--mfspart-post-refinement",
        action=_BooleanOptionalAction,
        default=None,
    )
    phase3.add_argument(
        "--mfspart-post-refinement-early-stop", type=int, default=1000
    )
    phase3.add_argument(
        "--mfspart-post-refinement-bottleneck-beta",
        type=float,
        default=256.0,
    )
    phase3.add_argument("--timing-path-database", type=Path)
    phase3.add_argument(
        "--mfspart-post-refinement-timing-path-beta",
        type=float,
        default=DEFAULT_TIMING_PATH_BETA,
    )

    sta_parser = subparsers.add_parser(
        "sta", help="STA path extraction artifact operations"
    )
    sta_subparsers = sta_parser.add_subparsers(
        dest="sta_command", required=True
    )
    sta_map = sta_subparsers.add_parser(
        "emit-vivado-cut-map",
        help="map stable EmuIR cut nets to mapped-Verilog net names",
    )
    sta_map.add_argument("--ir", type=Path, required=True)
    sta_map.add_argument("--assignment", type=Path, required=True)
    sta_map.add_argument("--output", "-o", type=Path, required=True)
    sta_import = sta_subparsers.add_parser(
        "import-vivado-tsv",
        help="import export_cut_timing_paths.tcl output",
    )
    sta_import.add_argument("--input", type=Path, required=True)
    sta_import.add_argument("--assignment", type=Path, required=True)
    sta_import.add_argument("--output", "-o", type=Path, required=True)
    sta_net_map = sta_subparsers.add_parser(
        "emit-vivado-net-map",
        help="map every stable EmuIR net to its mapped-Verilog name",
    )
    sta_net_map.add_argument("--ir", type=Path, required=True)
    sta_net_map.add_argument("--output", "-o", type=Path, required=True)
    sta_database_import = sta_subparsers.add_parser(
        "import-vivado-path-database",
        help="import export_timing_path_database.tcl output",
    )
    sta_database_import.add_argument("--input", type=Path, required=True)
    sta_database_import.add_argument("--ir", type=Path, required=True)
    sta_database_import.add_argument(
        "--output", "-o", type=Path, required=True
    )
    sta_database_project = sta_subparsers.add_parser(
        "project-path-database",
        help="project partition-independent paths onto candidate cut nets",
    )
    sta_database_project.add_argument(
        "--database", type=Path, required=True
    )
    sta_database_project.add_argument(
        "--assignment", type=Path, required=True
    )
    sta_database_project.add_argument(
        "--output", "-o", type=Path, required=True
    )
    sta_database_validate = sta_subparsers.add_parser(
        "validate-path-database",
        help="independently validate a partition-independent path database",
    )
    sta_database_validate.add_argument(
        "--database", type=Path, required=True
    )
    sta_database_validate.add_argument("--ir", type=Path, required=True)
    sta_weights = sta_subparsers.add_parser(
        "derive-partition-net-weights",
        help="derive timing-driven hyperedge weights from OpenSTA paths",
    )
    sta_weights.add_argument("--database", type=Path, required=True)
    sta_weights.add_argument("--ir", type=Path, required=True)
    sta_weights.add_argument("--output", "-o", type=Path, required=True)
    sta_weights.add_argument("--criticality-scale", type=float, default=9.0)
    sta_weights.add_argument(
        "--criticality-exponent", type=float, default=2.0
    )
    sta_opensta = sta_subparsers.add_parser(
        "run-opensta",
        help="build a partition-independent path database with in-tree OpenSTA",
    )
    sta_opensta.add_argument("--ir", type=Path, required=True)
    sta_opensta.add_argument("--output", "-o", type=Path, required=True)
    sta_opensta.add_argument(
        "--clock-period",
        action="append",
        default=[],
        metavar="CLOCK=PERIOD_NS",
    )
    sta_opensta.add_argument(
        "--timing-model",
        type=Path,
        default=DEFAULT_TIMING_MODEL,
    )
    sta_opensta.add_argument(
        "--architecture-timing-db",
        type=Path,
        help=(
            "public VTR TimingDB; generates a design-specialized "
            "pre-placement OpenSTA model"
        ),
    )
    sta_opensta.add_argument(
        "--opensta",
        "--openroad",
        dest="opensta",
        help="explicit comparison override; defaults to the in-tree build",
    )
    sta_opensta.add_argument("--max-paths", type=int, default=200000)
    sta_opensta.add_argument("--log", type=Path)

    route_parser = subparsers.add_parser(
        "route", help="board-level system route artifact operations"
    )
    route_subparsers = route_parser.add_subparsers(
        dest="route_command", required=True
    )
    route_validate = route_subparsers.add_parser(
        "validate", help="independently validate Phase 4 system routes"
    )
    route_validate.add_argument("routes", type=Path)
    route_validate.add_argument("--assignment", type=Path, required=True)
    route_validate.add_argument("--platform", type=Path, required=True)
    route_validate.add_argument("--timing-paths", type=Path)

    phase4 = subparsers.add_parser(
        "phase4", help="route partition cut nets over BoardDB links"
    )
    phase4.add_argument("--assignment", type=Path, required=True)
    phase4.add_argument("--platform", type=Path, required=True)
    phase4.add_argument("--out", type=Path, required=True)
    phase4.add_argument("--constraints", type=Path)
    phase4.add_argument("--frame-slots", type=int)
    phase4.add_argument("--max-iterations", type=int)
    phase4.add_argument(
        "--candidate-workers",
        type=int,
        default=1,
        help="parallel global-candidate generators (default: 1)",
    )
    phase4.add_argument(
        "--tdm-feedback",
        type=Path,
        help=(
            "checked Phase 5 tdm-feedback/v1 used only by the global "
            "candidate provider"
        ),
    )
    phase4.add_argument(
        "--tdm-feedback-routes",
        type=Path,
        help="source routes used to independently reconstruct feedback",
    )
    phase4.add_argument(
        "--tdm-feedback-schedule",
        type=Path,
        help="source schedule used to independently reconstruct feedback",
    )
    phase4.add_argument(
        "--tdm-feedback-ratio-plan",
        type=Path,
        help="optional source ratio plan used by the feedback schedule",
    )
    phase4.add_argument(
        "--physical-feedback",
        type=Path,
        help="checked Phase 7 boundary-domain feedback to add to TDM prices",
    )
    phase4.add_argument(
        "--physical-feedback-runtime",
        type=Path,
        help="source runtime used to independently validate physical feedback",
    )
    phase4.add_argument(
        "--physical-feedback-summary",
        type=Path,
        help="source Phase 7 physical summary with boundary timing",
    )
    phase4.add_argument(
        "--physical-feedback-weight", type=float, default=1.0
    )
    phase4.add_argument(
        "--provider",
        choices=[
            NATIVE_ROUTER_PROVIDER,
            NATIVE_TIMING_EVALUATED_PROVIDER,
            TLR_PROVIDER,
            ROUTE_TDM_PROVIDER,
            GLOBAL_CANDIDATE_PROVIDER,
        ],
        default=None,
        help=(
            f"defaults to {GLOBAL_CANDIDATE_PROVIDER} when --timing-paths is "
            f"supplied, otherwise {NATIVE_ROUTER_PROVIDER}"
        ),
    )
    phase4.add_argument(
        "--timing-paths",
        type=Path,
        help="emuflow.sta-paths/v1 input required by the timing-aware provider",
    )
    phase4.add_argument(
        "--router",
        help=(
            "explicit comparison override; defaults to the in-tree "
            "emuflow_tlr_router build"
        ),
    )

    schedule_parser = subparsers.add_parser(
        "schedule", help="TDM schedule artifact operations"
    )
    schedule_subparsers = schedule_parser.add_subparsers(
        dest="schedule_command", required=True
    )
    schedule_validate = schedule_subparsers.add_parser(
        "validate", help="independently validate a Phase 5 TDM schedule"
    )
    schedule_validate.add_argument("schedule", type=Path)
    schedule_validate.add_argument("--routes", type=Path, required=True)
    schedule_validate.add_argument("--platform", type=Path, required=True)
    schedule_validate.add_argument("--ratio-plan", type=Path)

    phase5 = subparsers.add_parser(
        "phase5", help="schedule routed bit-hops into TDM lanes and slots"
    )
    phase5.add_argument("--routes", type=Path, required=True)
    phase5.add_argument("--platform", type=Path, required=True)
    phase5.add_argument("--out", type=Path, required=True)
    phase5.add_argument("--simulation-frames", type=int, default=16)
    phase5.add_argument(
        "--provider",
        choices=(
            TDM_RATIO_PROVIDER,
            TDM_TIMING_DAG_RATIO_PROVIDER,
            TDM_BASELINE_PROVIDER,
        ),
        default=None,
        help=(
            f"defaults to {TDM_TIMING_DAG_RATIO_PROVIDER} when routes "
            "contain timing, otherwise the deterministic baseline"
        ),
    )
    phase5.add_argument(
        "--ratio-optimizer",
        help=(
            "explicit comparison override; defaults to the in-tree "
            "emuflow_tdm_ratio_optimizer build"
        ),
    )
    phase5.add_argument(
        "--timing-dag-optimizer",
        help=(
            "explicit comparison override; defaults to the in-tree "
            "emuflow_tdm_timing_dag_optimizer build"
        ),
    )
    phase5.add_argument("--ratio-max-iterations", type=int, default=500)
    phase5.add_argument(
        "--slot-optimizer",
        help=(
            "explicit comparison override; defaults to the in-tree "
            "emuflow_tdm_slot_optimizer build"
        ),
    )
    phase5.add_argument("--max-ratio", type=int)
    phase5.add_argument(
        "--ratio-quantum",
        type=int,
        help=(
            "explicit comparison override; defaults to the frozen Phase 4 "
            "route constraint"
        ),
    )
    phase5.add_argument(
        "--post-refinement-iterations", type=int, default=200
    )
    phase5.add_argument(
        "--slot-refinement-iterations", type=int, default=200
    )
    phase5.add_argument("--ratio-convergence", type=float, default=1.0e-9)

    partition_feedback = subparsers.add_parser(
        "partition-feedback",
        help="derive channel-usage partition weights from routed TDM results",
    )
    partition_feedback.add_argument("--routes", type=Path, required=True)
    partition_feedback.add_argument("--ratio-plan", type=Path, required=True)
    partition_feedback.add_argument("--platform", type=Path, required=True)
    partition_feedback.add_argument("--output", "-o", type=Path, required=True)
    partition_feedback.add_argument("--optimizer")
    partition_feedback.add_argument(
        "--pair-pressure-weight", type=float, default=1.0
    )

    partition_pressure = subparsers.add_parser(
        "partition-pressure-reference",
        help="run the compact exhaustive path/TDM-aware partition oracle",
    )
    partition_pressure.add_argument("--ir", type=Path, required=True)
    partition_pressure.add_argument("--platform", type=Path, required=True)
    partition_pressure.add_argument("--clusters", type=Path, required=True)
    partition_pressure.add_argument("--constraints", type=Path, required=True)
    partition_pressure.add_argument(
        "--timing-database", type=Path, required=True
    )
    partition_pressure.add_argument(
        "--route-constraints", type=Path, required=True
    )
    partition_pressure.add_argument(
        "--initial-assignment", type=Path, required=True
    )
    partition_pressure.add_argument("--out", type=Path, required=True)
    partition_pressure.add_argument("--max-moves", type=int)

    cross_stage = subparsers.add_parser(
        "cross-stage",
        help="checked Phase 3--5 feedback optimization operations",
    )
    cross_stage_subparsers = cross_stage.add_subparsers(
        dest="cross_stage_command", required=True
    )
    cross_stage_evaluate = cross_stage_subparsers.add_parser(
        "evaluate", help="score one partition/route/schedule candidate"
    )
    cross_stage_evaluate.add_argument(
        "--database", type=Path, required=True
    )
    cross_stage_evaluate.add_argument(
        "--assignment", type=Path, required=True
    )
    cross_stage_evaluate.add_argument("--routes", type=Path, required=True)
    cross_stage_evaluate.add_argument("--schedule", type=Path, required=True)
    cross_stage_evaluate.add_argument(
        "--ratio-plan", type=Path, required=True
    )
    cross_stage_evaluate.add_argument(
        "--platform", type=Path, required=True
    )
    cross_stage_evaluate.add_argument(
        "--output", "-o", type=Path, required=True
    )
    cross_stage_validate = cross_stage_subparsers.add_parser(
        "validate-candidate",
        help="independently reconstruct one candidate score",
    )
    cross_stage_validate.add_argument("candidate", type=Path)
    cross_stage_validate.add_argument(
        "--database", type=Path, required=True
    )
    cross_stage_validate.add_argument(
        "--assignment", type=Path, required=True
    )
    cross_stage_validate.add_argument(
        "--routes", type=Path, required=True
    )
    cross_stage_validate.add_argument(
        "--schedule", type=Path, required=True
    )
    cross_stage_validate.add_argument(
        "--ratio-plan", type=Path, required=True
    )
    cross_stage_validate.add_argument(
        "--platform", type=Path, required=True
    )
    cross_stage_report_validate = cross_stage_subparsers.add_parser(
        "validate-report",
        help="independently reconstruct all successful candidates",
    )
    cross_stage_report_validate.add_argument("report", type=Path)
    cross_stage_report_validate.add_argument(
        "--ir", type=Path, required=True
    )
    cross_stage_report_validate.add_argument(
        "--database", type=Path, required=True
    )
    cross_stage_report_validate.add_argument(
        "--platform", type=Path, required=True
    )
    cross_stage_optimize = cross_stage_subparsers.add_parser(
        "optimize",
        help="iterate TDM feedback through partition, routing, and scheduling",
    )
    cross_stage_optimize.add_argument("--ir", type=Path, required=True)
    cross_stage_optimize.add_argument(
        "--platform", type=Path, required=True
    )
    cross_stage_optimize.add_argument(
        "--database", type=Path, required=True
    )
    cross_stage_optimize.add_argument(
        "--initial-assignment", type=Path, required=True
    )
    cross_stage_optimize.add_argument(
        "--seed-candidate-phase3-root", type=Path
    )
    cross_stage_optimize.add_argument("--out", type=Path, required=True)
    cross_stage_optimize.add_argument(
        "--phase3-constraints", type=Path
    )
    cross_stage_optimize.add_argument(
        "--route-constraints", type=Path
    )
    cross_stage_optimize.add_argument(
        "--board-link-timing-db",
        type=Path,
        help=(
            "apply direction-exact BoardLinkTimingDB bounds to every "
            "routing, TDM, and feedback candidate"
        ),
    )
    cross_stage_optimize.add_argument(
        "--phase3-provider",
        choices=(
            "repart-replication",
            "repart",
            "tritonpart",
            "mfspart",
            "patron",
        ),
        default="repart-replication",
    )
    cross_stage_optimize.add_argument(
        "--max-outer-iterations", type=int, default=1
    )
    cross_stage_optimize.add_argument("--seed", type=int, default=0)
    cross_stage_optimize.add_argument("--min-used-fpgas", type=int)
    cross_stage_optimize.add_argument(
        "--balance-tolerance", type=float
    )
    cross_stage_optimize.add_argument("--openroad")
    cross_stage_optimize.add_argument("--repart")
    cross_stage_optimize.add_argument("--patron-refiner")
    cross_stage_optimize.add_argument("--patron-max-moves", type=int)
    cross_stage_optimize.add_argument(
        "--patron-flow-refinement", action="store_true"
    )
    cross_stage_optimize.add_argument(
        "--patron-algorithm-version",
        type=int,
        choices=(6, 9, 10, 11, 12, 13, 14),
        default=6,
    )
    cross_stage_optimize.add_argument(
        "--partition-timeout-seconds", type=int, default=3600
    )
    cross_stage_optimize.add_argument(
        "--partition-seed-attempts", type=int, default=1
    )
    cross_stage_optimize.add_argument(
        "--partition-num-initial-solutions", type=int, default=50
    )
    cross_stage_optimize.add_argument(
        "--partition-num-best-initial-solutions", type=int, default=10
    )
    cross_stage_optimize.add_argument(
        "--partition-repair-min-used-fpgas", action="store_true"
    )
    cross_stage_optimize.add_argument(
        "--partition-repair-balance", action="store_true"
    )
    cross_stage_optimize.add_argument("--router")
    cross_stage_optimize.add_argument(
        "--route-provider",
        choices=(
            TLR_PROVIDER,
            ROUTE_TDM_PROVIDER,
            GLOBAL_CANDIDATE_PROVIDER,
        ),
        help=(
            f"defaults to {GLOBAL_CANDIDATE_PROVIDER}; the historical "
            f"{ROUTE_TDM_PROVIDER} remains available for rollback"
        ),
    )
    cross_stage_optimize.add_argument(
        "--route-candidate-workers",
        type=int,
        default=1,
        help="parallel deterministic candidate generators for global routing",
    )
    cross_stage_optimize.add_argument("--frame-slots", type=int)
    cross_stage_optimize.add_argument(
        "--optimize-frame-slots",
        action="store_true",
        help=(
            "treat --frame-slots as an upper bound and minimize the exact "
            "feasible frame for every partition candidate"
        ),
    )
    cross_stage_optimize.add_argument(
        "--route-max-iterations", type=int
    )
    cross_stage_optimize.add_argument(
        "--tdm-provider",
        choices=(
            TDM_RATIO_PROVIDER,
            TDM_TIMING_DAG_RATIO_PROVIDER,
            TDM_BASELINE_PROVIDER,
        ),
    )
    cross_stage_optimize.add_argument("--ratio-optimizer")
    cross_stage_optimize.add_argument("--timing-dag-optimizer")
    cross_stage_optimize.add_argument("--slot-optimizer")
    cross_stage_optimize.add_argument("--feedback-optimizer")
    cross_stage_optimize.add_argument(
        "--simulation-frames", type=int, default=4
    )
    cross_stage_optimize.add_argument(
        "--ratio-max-iterations", type=int, default=500
    )
    cross_stage_optimize.add_argument("--max-ratio", type=int)
    cross_stage_optimize.add_argument(
        "--ratio-quantum", type=int, default=8
    )
    cross_stage_optimize.add_argument(
        "--post-refinement-iterations", type=int, default=200
    )
    cross_stage_optimize.add_argument(
        "--slot-refinement-iterations", type=int, default=200
    )
    cross_stage_optimize.add_argument(
        "--ratio-convergence", type=float, default=1.0e-9
    )
    cross_stage_optimize.add_argument(
        "--pair-pressure-weight", type=float, default=1.0
    )
    cross_stage_optimize.add_argument(
        "--feedback-step",
        type=float,
        action="append",
        default=[],
        help=(
            "strictly decreasing proximal line-search step in (0,1]; "
            "repeat to override the default 1,0.5,0.25,0.125 sequence"
        ),
    )

    pin_plan_parser = subparsers.add_parser(
        "pin-plan",
        help="placement-aware TDM grouping and virtual pin planning",
    )
    pin_plan_subparsers = pin_plan_parser.add_subparsers(
        dest="pin_plan_command", required=True
    )
    pin_plan_build = pin_plan_subparsers.add_parser(
        "build", help="build a plan from OpenPARF lookahead placements"
    )
    pin_plan_build.add_argument("--ir", type=Path, required=True)
    pin_plan_build.add_argument("--schedule", type=Path, required=True)
    pin_plan_build.add_argument("--platform", type=Path, required=True)
    pin_plan_build.add_argument(
        "--placement",
        action="append",
        default=[],
        required=True,
        metavar="FPGA=PATH",
    )
    pin_plan_build.add_argument("--positions-out", type=Path, required=True)
    pin_plan_build.add_argument("--output", "-o", type=Path, required=True)
    pin_plan_build.add_argument("--planner")
    pin_plan_build.add_argument("--region-count", type=int, default=3)
    pin_plan_build.add_argument(
        "--refinement-iterations", type=int, default=100
    )
    pin_plan_build.add_argument("--crossing-weight", type=float, default=1.0)
    pin_plan_build.add_argument("--position-weight", type=float, default=1.0)
    pin_plan_chimew = pin_plan_subparsers.add_parser(
        "chimew-build",
        help="bind certified Chimew banks/channels to electrical concrete lanes",
    )
    pin_plan_chimew.add_argument("--schedule", type=Path, required=True)
    pin_plan_chimew.add_argument("--platform", type=Path, required=True)
    pin_plan_chimew.add_argument("--assignment-input", type=Path, required=True)
    pin_plan_chimew.add_argument(
        "--assignment-report",
        type=Path,
        help="precomputed certified report (requires --qualification)",
    )
    pin_plan_chimew.add_argument("--electrical-map", type=Path, required=True)
    pin_plan_chimew.add_argument(
        "--qualification",
        type=Path,
        help="complete chimew-qualify certificate (recommended for sign-off)",
    )
    pin_plan_chimew.add_argument("--assigner")
    pin_plan_chimew.add_argument("--region-count", type=int, default=31)
    pin_plan_chimew.add_argument("--out", type=Path, required=True)
    pin_plan_chimew_ratios = pin_plan_subparsers.add_parser(
        "chimew-materialize-ratios",
        help="explicitly derive Chimew group capacities from lane occupancy",
    )
    pin_plan_chimew_ratios.add_argument("--schedule", type=Path, required=True)
    pin_plan_chimew_ratios.add_argument("--output", "-o", type=Path, required=True)
    pin_plan_chimew_qualify = pin_plan_subparsers.add_parser(
        "chimew-qualify",
        help="seal a complete Chimew lookahead/RUDY/assignment artifact chain",
    )
    pin_plan_chimew_qualify.add_argument("--schedule", type=Path, required=True)
    pin_plan_chimew_qualify.add_argument("--crossings", type=Path, required=True)
    pin_plan_chimew_qualify.add_argument(
        "--initial-grouping", type=Path, required=True
    )
    pin_plan_chimew_qualify.add_argument("--positions", type=Path, required=True)
    pin_plan_chimew_qualify.add_argument(
        "--refined-grouping", type=Path, required=True
    )
    pin_plan_chimew_qualify.add_argument("--rudy-input", type=Path, required=True)
    pin_plan_chimew_qualify.add_argument("--rudy-report", type=Path, required=True)
    pin_plan_chimew_qualify.add_argument(
        "--assignment-input", type=Path, required=True
    )
    pin_plan_chimew_qualify.add_argument(
        "--assignment-report", type=Path, required=True
    )
    pin_plan_chimew_qualify.add_argument("--output", "-o", type=Path, required=True)
    pin_plan_chimew_run = pin_plan_subparsers.add_parser(
        "chimew-run",
        help="run and certify the complete source-qualified Chimew Phase 6 path",
    )
    pin_plan_chimew_run.add_argument("--schedule", type=Path, required=True)
    pin_plan_chimew_run.add_argument("--platform", type=Path, required=True)
    pin_plan_chimew_run.add_argument("--crossings", type=Path, required=True)
    pin_plan_chimew_run.add_argument("--positions", type=Path, required=True)
    pin_plan_chimew_run.add_argument("--rudy-input", type=Path, required=True)
    pin_plan_chimew_run.add_argument(
        "--assignment-input", type=Path, required=True
    )
    pin_plan_chimew_run.add_argument("--electrical-map", type=Path, required=True)
    pin_plan_chimew_run.add_argument("--routing-source", type=Path)
    pin_plan_chimew_run.add_argument("--placement-source", type=Path)
    pin_plan_chimew_run.add_argument("--netlist-source", type=Path)
    pin_plan_chimew_run.add_argument("--architecture-source", type=Path)
    pin_plan_chimew_run.add_argument("--package-pins-source", type=Path)
    pin_plan_chimew_run.add_argument("--grouper")
    pin_plan_chimew_run.add_argument("--refiner")
    pin_plan_chimew_run.add_argument("--rudy")
    pin_plan_chimew_run.add_argument("--assigner")
    pin_plan_chimew_run.add_argument("--region-count", type=int, default=31)
    pin_plan_chimew_run.add_argument("--out", type=Path, required=True)
    pin_plan_chimew_validate = pin_plan_subparsers.add_parser(
        "chimew-validate",
        help="independently validate a frozen Chimew Phase 6 bundle",
    )
    pin_plan_chimew_validate.add_argument("bundle", type=Path)
    pin_plan_chimew_correlate = pin_plan_subparsers.add_parser(
        "chimew-correlate",
        help="rank-correlate source-bound Chimew candidates with Vivado evidence",
    )
    pin_plan_chimew_correlate.add_argument("--input", type=Path, required=True)
    pin_plan_chimew_correlate.add_argument("--output", "-o", type=Path, required=True)
    pin_plan_chimew_correlation_validate = pin_plan_subparsers.add_parser(
        "chimew-correlation-validate",
        help="independently replay a Chimew/Vivado correlation report",
    )
    pin_plan_chimew_correlation_validate.add_argument(
        "--input", type=Path, required=True
    )
    pin_plan_chimew_correlation_validate.add_argument("report", type=Path)
    pin_plan_validate = pin_plan_subparsers.add_parser(
        "validate", help="independently validate a pin plan"
    )
    pin_plan_validate.add_argument("plan", type=Path)
    pin_plan_validate.add_argument("--schedule", type=Path, required=True)
    pin_plan_validate.add_argument("--platform", type=Path, required=True)
    pin_plan_validate.add_argument("--positions", type=Path, required=True)

    split_parser = subparsers.add_parser(
        "split", help="per-FPGA netlist split artifact operations"
    )
    split_subparsers = split_parser.add_subparsers(
        dest="split_command", required=True
    )
    split_validate = split_subparsers.add_parser(
        "validate", help="independently validate Phase 6 split artifacts"
    )
    split_validate.add_argument("manifest", type=Path)
    split_validate.add_argument("--ir", type=Path, required=True)
    split_validate.add_argument("--assignment", type=Path, required=True)
    split_validate.add_argument("--schedule", type=Path, required=True)
    split_validate.add_argument("--platform", type=Path, required=True)
    split_validate.add_argument("--pin-plan", type=Path)
    split_validate.add_argument("--position-hints", type=Path)
    split_validate.add_argument("--electrical-binding", type=Path)

    phase6 = subparsers.add_parser(
        "phase6",
        help="split EmuIR and bind cut signals to logical TDM lanes",
    )
    phase6.add_argument("--ir", type=Path, required=True)
    phase6.add_argument("--assignment", type=Path, required=True)
    phase6.add_argument("--schedule", type=Path, required=True)
    phase6.add_argument("--platform", type=Path, required=True)
    phase6.add_argument("--out", type=Path, required=True)
    phase6.add_argument("--pin-plan", type=Path)
    phase6.add_argument("--position-hints", type=Path)
    phase6.add_argument("--electrical-binding", type=Path)
    phase6.add_argument("--equivalence-cycles", type=int, default=16)
    phase6.add_argument("--equivalence-seed", type=int, default=20260727)

    phase6b = subparsers.add_parser(
        "phase6b",
        help="bind virtual link anchors to electrical BSP package pins",
    )
    phase6b.add_argument("--schedule", type=Path, required=True)
    phase6b.add_argument("--platform", type=Path, required=True)
    phase6b.add_argument(
        "--position-hints",
        type=Path,
        help="required with --pin-plan or for a parallel-I/O BSP",
    )
    phase6b.add_argument(
        "--pin-plan",
        type=Path,
        help="required with --position-hints or for a parallel-I/O BSP",
    )
    phase6b.add_argument(
        "--anchor", action="append", default=[], metavar="FPGA=PATH"
    )
    phase6b.add_argument(
        "--bsp",
        type=Path,
        help=(
            "parallel-I/O hardware BSP; omit for source-backed serial "
            "endpoint bindings embedded in BoardDB"
        ),
    )
    phase6b.add_argument("--solver")
    phase6b.add_argument("--iostandard", default="LVCMOS18")
    phase6b.add_argument("--placement-weight", type=float, default=1.0)
    phase6b.add_argument("--skew-weight", type=float, default=1.0)
    phase6b.add_argument("--out", type=Path, required=True)

    phase6c = subparsers.add_parser(
        "phase6c",
        help="generate serial-PHY wrapper RTL and its unresolved provider contract",
    )
    phase6c.add_argument("--platform", type=Path, required=True)
    phase6c.add_argument("--binding", type=Path, required=True)
    phase6c.add_argument(
        "--transport", action="append", default=[], metavar="FPGA=PATH"
    )
    phase6c.add_argument(
        "--phy-provider",
        type=Path,
        help="source-visible or materialized vendor serial PHY provider manifest",
    )
    phase6c.add_argument(
        "--board-overlay",
        type=Path,
        help="validated board-specific GT site, reference-clock, and reset bindings",
    )
    phase6c.add_argument(
        "--gt-site-map",
        type=Path,
        help="Vivado-derived package-pin to GT channel site map",
    )
    phase6c.add_argument(
        "--runtime-sync-topology",
        type=Path,
        help="validated rooted-tree runtime synchronization topology",
    )
    phase6c.add_argument(
        "--runtime-sync-provider",
        type=Path,
        help="source-visible runtime synchronization provider manifest",
    )
    phase6c.add_argument("--out", type=Path, required=True)

    package_pin = subparsers.add_parser(
        "package-pin",
        help="physical package-pin binding artifact operations",
    )
    package_pin_subparsers = package_pin.add_subparsers(
        dest="package_pin_command", required=True
    )
    package_pin_validate = package_pin_subparsers.add_parser(
        "validate",
        help="independently validate a package-pin binding",
    )
    package_pin_validate.add_argument("binding", type=Path)
    package_pin_validate.add_argument("--schedule", type=Path, required=True)
    package_pin_validate.add_argument("--platform", type=Path, required=True)
    package_pin_validate.add_argument("--position-hints", type=Path)
    package_pin_validate.add_argument("--pin-plan", type=Path)
    package_pin_validate.add_argument(
        "--anchor", action="append", default=[], metavar="FPGA=PATH"
    )
    package_pin_validate.add_argument("--bsp", type=Path)

    lower = subparsers.add_parser(
        "lower-placement-ir",
        help="merge one partition with its synthesized transport EmuIR",
    )
    lower.add_argument("--netlist", type=Path, required=True)
    lower.add_argument("--transport", type=Path, required=True)
    lower.add_argument("--transport-ir", type=Path, required=True)
    lower.add_argument("--output", "-o", type=Path, required=True)
    lower.add_argument("--report", type=Path)
    lower.add_argument(
        "--boundary-identities",
        type=Path,
        help="provider-neutral physical boundary identity database",
    )

    emit_verilog = subparsers.add_parser(
        "emit-mapped-verilog",
        help="emit a structural Xilinx primitive netlist from EmuIR",
    )
    emit_verilog.add_argument("--ir", type=Path, required=True)
    emit_verilog.add_argument("--output", "-o", type=Path, required=True)
    emit_verilog.add_argument("--report", type=Path)

    phase7c = subparsers.add_parser(
        "phase7c",
        help="build and validate the virtual runtime/timing/QoR contract",
    )
    phase7c.add_argument("--schedule", type=Path, required=True)
    phase7c.add_argument("--platform", type=Path, required=True)
    phase7c.add_argument("--phase3-report", type=Path, required=True)
    phase7c.add_argument("--phase4-report", type=Path, required=True)
    phase7c.add_argument("--phase5-report", type=Path, required=True)
    phase7c.add_argument("--phase6-report", type=Path, required=True)
    phase7c.add_argument("--physical-summary", type=Path)
    phase7c.add_argument(
        "--routes",
        type=Path,
        help="Phase 4 routes.json required for unified physical timing",
    )
    phase7c.add_argument("--simulation-frames", type=int, default=12)
    phase7c.add_argument("--out", type=Path, required=True)

    phase7d = subparsers.add_parser(
        "phase7d",
        help="audit and hash a complete board-independent G0-G9 release",
    )
    phase7d.add_argument("--benchmark-report", type=Path, required=True)
    phase7d.add_argument("--phase3-report", type=Path, required=True)
    phase7d.add_argument("--phase4-report", type=Path, required=True)
    phase7d.add_argument("--phase5-report", type=Path, required=True)
    phase7d.add_argument("--phase6-report", type=Path, required=True)
    phase7d.add_argument("--phase7c-report", type=Path, required=True)
    phase7d.add_argument("--runtime-contract", type=Path, required=True)
    phase7d.add_argument("--qor-report", type=Path, required=True)
    phase7d.add_argument("--physical-summary", type=Path, required=True)
    phase7d.add_argument("--platform", type=Path, required=True)
    phase7d.add_argument(
        "--lowering-report", action="append", default=[], metavar="FPGA=PATH"
    )
    phase7d.add_argument(
        "--placement-report", action="append", default=[], metavar="FPGA=PATH"
    )
    phase7d.add_argument(
        "--emission-report", action="append", default=[], metavar="FPGA=PATH"
    )
    phase7d.add_argument(
        "--artifact", action="append", default=[], metavar="LABEL=PATH"
    )
    phase7d.add_argument("--source-commit", required=True)
    phase7d.add_argument("--out", type=Path, required=True)

    phase8a = subparsers.add_parser(
        "phase8a",
        help="seal the hardware-BSP requirements for a G0-G9 release",
    )
    phase8a.add_argument("--release-manifest", type=Path, required=True)
    phase8a.add_argument("--phase6-report", type=Path, required=True)
    phase8a.add_argument("--platform", type=Path, required=True)
    phase8a.add_argument(
        "--anchor", action="append", default=[], metavar="FPGA=PATH"
    )
    phase8a.add_argument("--out", type=Path, required=True)
    return parser


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "experiment-stage":
        if args.experiment_stage_command == "frontend-run":
            with json_write_policy(durable=not args.managed_dag_node):
                report = run_frontend_checkpoint(
                    args.platform,
                    args.out,
                    sources=args.source,
                    top=args.top,
                    clocks=args.clock,
                    yosys_json=args.yosys_json,
                    yosys=args.yosys,
                    mapping_profile=args.mapping_profile,
                    require_no_fabric_clock=not args.allow_fabric_clock,
                    managed_dag_node=args.managed_dag_node,
                )
        elif args.experiment_stage_command == "frontend-validate":
            report = validate_frontend_checkpoint(
                args.root,
                args.platform,
                managed_dag_node=args.managed_dag_node,
            )
        elif args.experiment_stage_command == "timing-run":
            with json_write_policy(durable=not args.managed_dag_node):
                report = run_timing_checkpoint(
                    args.frontend,
                    args.out,
                    clocks=parse_clock_definitions(args.clock_period),
                    timing_model_path=args.timing_model,
                    architecture_timing_db_path=args.architecture_timing_db,
                    opensta=args.opensta,
                    max_paths=args.max_paths,
                    criticality_scale=args.criticality_scale,
                    criticality_exponent=args.criticality_exponent,
                    managed_dag_node=args.managed_dag_node,
                )
        elif args.experiment_stage_command == "timing-validate":
            report = validate_timing_checkpoint(
                args.frontend,
                args.root,
                managed_dag_node=args.managed_dag_node,
            )
        elif args.experiment_stage_command == "partition-run":
            with json_write_policy(durable=not args.managed_dag_node):
                report = run_partition_checkpoint(
                    args.frontend,
                    args.timing,
                    args.platform,
                    args.out,
                    provider=args.provider,
                    seed=args.seed,
                    constraints_path=args.constraints,
                    route_constraints_path=args.route_constraints,
                    min_used_fpgas=args.min_used_fpgas,
                    balance_tolerance=args.balance_tolerance,
                    openroad=args.openroad,
                    tritonpart_solution=args.tritonpart_solution,
                    hop_refiner=args.hop_refiner,
                    mfspart_coarsener=args.mfspart_coarsener,
                    mfspart_initializer=args.mfspart_initializer,
                    mfspart_refiner=args.mfspart_refiner,
                    mfspart_refiner_checker=args.mfspart_refiner_checker,
                    mfspart_legalizer=args.mfspart_legalizer,
                    mfspart_post_refinement=args.mfspart_post_refinement,
                    mfspart_post_refinement_early_stop=(
                        args.mfspart_post_refinement_early_stop
                    ),
                    mfspart_post_refinement_bottleneck_beta=(
                        args.mfspart_post_refinement_bottleneck_beta
                    ),
                    mfspart_post_refinement_timing_path_beta=(
                        args.mfspart_post_refinement_timing_path_beta
                    ),
                    timeout_seconds=args.timeout_seconds,
                    seed_attempts=args.seed_attempts,
                    repair_balance=args.repair_balance,
                    num_initial_solutions=args.num_initial_solutions,
                    num_best_initial_solutions=args.num_best_initial_solutions,
                    cut_mode=args.cut_mode,
                    max_cross_fpga_dependency_depth=(
                        args.max_cross_fpga_dependency_depth
                    ),
                    static_exact_candidate_policy=(
                        args.static_exact_candidate_policy
                    ),
                    minimum_combinational_cut_nets=(
                        args.minimum_combinational_cut_nets
                    ),
                    patron_refiner=args.patron_refiner,
                    patron_max_moves=args.patron_max_moves,
                    patron_flow_refinement=args.patron_flow_refinement,
                    patron_algorithm_version=args.patron_algorithm_version,
                    patron_initial_assignment_path=(
                        args.patron_initial_assignment
                    ),
                    patron_initial_clusters_path=(
                        args.patron_initial_clusters
                    ),
                    patron_physical_system_timing_path=(
                        args.patron_physical_system_timing
                    ),
                    patron_physical_feedback_scale=(
                        args.patron_physical_feedback_scale
                    ),
                    managed_dag_node=args.managed_dag_node,
                )
        elif args.experiment_stage_command == "partition-validate":
            report = validate_partition_checkpoint(
                args.frontend,
                args.timing,
                args.platform,
                args.root,
                constraints_path=args.constraints,
                route_constraints_path=args.route_constraints,
                tritonpart_solution=args.tritonpart_solution,
                expected_provider=args.provider,
                expected_seed=args.seed,
                expected_seed_attempts=args.seed_attempts,
                expected_repair_balance=args.repair_balance,
                expected_mfspart_post_refinement=(
                    args.mfspart_post_refinement
                ),
                expected_mfspart_post_refinement_early_stop=(
                    args.mfspart_post_refinement_early_stop
                ),
                expected_mfspart_post_refinement_bottleneck_beta=(
                    args.mfspart_post_refinement_bottleneck_beta
                ),
                expected_mfspart_post_refinement_timing_path_beta=(
                    args.mfspart_post_refinement_timing_path_beta
                ),
                expected_cut_mode=args.cut_mode,
                expected_max_cross_fpga_dependency_depth=(
                    args.max_cross_fpga_dependency_depth
                ),
                expected_static_exact_candidate_policy=(
                    args.static_exact_candidate_policy
                ),
                expected_minimum_combinational_cut_nets=(
                    args.minimum_combinational_cut_nets
                ),
                patron_initial_assignment_path=(
                    args.patron_initial_assignment
                ),
                patron_initial_clusters_path=(
                    args.patron_initial_clusters
                ),
                expected_patron_flow_refinement=(
                    args.patron_flow_refinement
                ),
                expected_patron_algorithm_version=(
                    args.patron_algorithm_version
                ),
                expected_patron_max_moves=args.patron_max_moves,
                patron_physical_system_timing_path=(
                    args.patron_physical_system_timing
                ),
                expected_patron_physical_feedback_scale=(
                    args.patron_physical_feedback_scale
                ),
                online_validation=args.online_validation,
            )
        elif args.experiment_stage_command == "cut-timing-run":
            with json_write_policy(durable=not args.managed_dag_node):
                report = run_cut_timing_checkpoint(
                    args.frontend,
                    args.timing,
                    args.partition,
                    args.out,
                    clocks=parse_clock_definitions(args.clock_period),
                    timing_model_path=args.timing_model,
                    architecture_timing_db_path=args.architecture_timing_db,
                    opensta=args.opensta,
                    max_paths=args.max_paths,
                    managed_dag_node=args.managed_dag_node,
                )
        elif args.experiment_stage_command == "cut-timing-validate":
            report = validate_cut_timing_checkpoint(
                args.frontend,
                args.timing,
                args.partition,
                args.root,
                timing_model_path=args.timing_model,
                architecture_timing_db_path=args.architecture_timing_db,
                managed_dag_node=args.managed_dag_node,
            )
        elif args.experiment_stage_command == "route-run":
            with json_write_policy(durable=not args.managed_dag_node):
                report = run_route_checkpoint(
                    args.partition,
                    args.cut_timing,
                    args.platform,
                    args.out,
                    constraints_path=args.constraints,
                    frame_slots=args.frame_slots,
                    max_iterations=args.max_iterations,
                    provider=args.provider,
                    candidate_workers=args.candidate_workers,
                    router=args.router,
                    managed_storage=args.managed_storage,
                    managed_dag_node=args.managed_dag_node,
                )
        elif args.experiment_stage_command == "route-validate":
            report = validate_route_checkpoint(
                args.partition,
                args.cut_timing,
                args.platform,
                args.root,
                constraints_path=args.constraints,
                expected_provider=args.provider,
                expected_candidate_workers=args.candidate_workers,
                managed_dag_node=args.managed_dag_node,
            )
        elif args.experiment_stage_command == "tdm-run":
            with json_write_policy(durable=not args.managed_dag_node):
                report = run_tdm_checkpoint(
                    args.route,
                    args.platform,
                    args.out,
                    simulation_frames=args.simulation_frames,
                    provider=args.provider,
                    ratio_max_iterations=args.ratio_max_iterations,
                    max_ratio=args.max_ratio,
                    ratio_quantum=args.ratio_quantum,
                    post_refinement_iterations=args.post_refinement_iterations,
                    slot_refinement_iterations=args.slot_refinement_iterations,
                    ratio_optimizer=args.ratio_optimizer,
                    timing_dag_optimizer=args.timing_dag_optimizer,
                    slot_optimizer=args.slot_optimizer,
                    managed_storage=args.managed_storage,
                    managed_dag_node=args.managed_dag_node,
                )
        elif args.experiment_stage_command == "tdm-validate":
            report = validate_tdm_checkpoint(
                args.route,
                args.platform,
                args.root,
                constraints_path=args.constraints,
                expected_provider=args.provider,
                managed_dag_node=args.managed_dag_node,
            )
        elif args.experiment_stage_command == "shared-materialize":
            report = materialize_shared_phase1_5(
                args.frontend,
                args.timing,
                args.partition,
                args.cut_timing,
                args.route,
                args.tdm,
                args.platform,
                args.out,
                timing_model_path=args.timing_model,
                architecture_timing_db_path=args.architecture_timing_db,
                managed_dag_node=args.managed_dag_node,
            )
        elif args.experiment_stage_command == "shared-validate":
            report = validate_materialized_shared_phase1_5(
                args.shared,
                args.platform,
                managed_dag_node=args.managed_dag_node,
            )
        elif args.experiment_stage_command == "lookahead-run":
            with json_write_policy(durable=not args.managed_dag_node):
                report = run_physical_lookahead(
                    args.shared,
                    args.baseline_phase6,
                    args.platform,
                    args.out,
                    seed=args.seed,
                    workers=args.workers,
                    region_count=args.region_count,
                    architecture=args.architecture,
                    architecture_id=args.architecture_id,
                    yosys=args.yosys,
                    vpr=args.vpr,
                    architecture_importer=args.architecture_importer,
                    packed_importer=args.packed_importer,
                    route_checker=args.route_checker,
                    openparf_install=args.openparf_install,
                    openparf_python=args.openparf_python,
                    route_channel_width=args.route_channel_width,
                    reuse_validated_phase6_equivalence=(
                        args.reuse_validated_phase6_equivalence
                    ),
                    managed_dag_node=args.managed_dag_node,
                )
        elif args.experiment_stage_command == "lookahead-resume":
            report = resume_physical_lookahead(
                args.shared,
                args.baseline_phase6,
                args.platform,
                args.out,
                seed=args.seed,
                workers=args.workers,
                region_count=args.region_count,
                architecture=args.architecture,
                architecture_id=args.architecture_id,
                route_channel_width=args.route_channel_width,
                reuse_validated_phase6_equivalence=(
                    args.reuse_validated_phase6_equivalence
                ),
                managed_dag_node=args.managed_dag_node,
            )
        elif args.experiment_stage_command == "lookahead-validate":
            report = validate_physical_lookahead(
                args.root,
                args.shared,
                args.baseline_phase6,
                args.platform,
                expected_seed=args.seed,
                expected_workers=args.workers,
                expected_region_count=args.region_count,
                expected_architecture=args.architecture,
                expected_route_channel_width=args.route_channel_width,
                reuse_validated_phase6_equivalence=(
                    args.reuse_validated_phase6_equivalence
                ),
                managed_dag_node=args.managed_dag_node,
            )
        elif args.experiment_stage_command == "phase6-run":
            with json_write_policy(durable=not args.managed_dag_node):
                report = run_phase6_checkpoint(
                    args.shared,
                    args.lookahead,
                    args.platform,
                    args.out,
                    provider=args.provider,
                    equivalence_cycles=args.equivalence_cycles,
                    equivalence_seed=args.equivalence_seed,
                    pin_planner=args.pin_planner,
                    chimew_grouper=args.chimew_grouper,
                    chimew_refiner=args.chimew_refiner,
                    chimew_rudy=args.chimew_rudy,
                    chimew_assigner=args.chimew_assigner,
                    managed_storage=args.managed_storage,
                    managed_dag_node=args.managed_dag_node,
                )
        elif args.experiment_stage_command == "phase6-validate":
            report = validate_phase6_checkpoint(
                args.root,
                args.shared,
                args.lookahead,
                args.platform,
                expected_provider=args.provider,
                managed_dag_node=args.managed_dag_node,
            )
        elif args.experiment_stage_command == "phase7-run":
            with json_write_policy(durable=not args.managed_dag_node):
                report = run_phase7_checkpoint(
                    args.shared,
                    args.lookahead,
                    args.phase6,
                    args.platform,
                    args.out,
                    seed=args.seed,
                    workers=args.workers,
                    yosys=args.yosys,
                    vpr=args.vpr,
                    architecture_importer=args.architecture_importer,
                    packed_importer=args.packed_importer,
                    route_checker=args.route_checker,
                    openparf_install=args.openparf_install,
                    openparf_python=args.openparf_python,
                    route_channel_width=args.route_channel_width,
                    reuse_validated_phase6_equivalence=(
                        args.reuse_validated_phase6_equivalence
                    ),
                    managed_dag_node=args.managed_dag_node,
                )
        elif args.experiment_stage_command == "phase7-validate":
            report = validate_phase7_checkpoint(
                args.root,
                args.shared,
                args.lookahead,
                args.phase6,
                args.platform,
                expected_seed=args.seed,
                expected_workers=args.workers,
                expected_route_channel_width=args.route_channel_width,
                reuse_validated_phase6_equivalence=(
                    args.reuse_validated_phase6_equivalence
                ),
                managed_dag_node=args.managed_dag_node,
            )
        elif args.experiment_stage_command == "qor-compare-run":
            report = run_canonical_qor_comparison(
                args.shared,
                parse_canonical_qor_arms(args.arm),
                args.out,
            )
        elif args.experiment_stage_command == "qor-compare-validate":
            report = validate_canonical_qor_comparison(
                args.root,
                args.shared,
                parse_canonical_qor_arms(args.arm),
            )
        elif args.experiment_stage_command == "static-exact-qor-compare-run":
            report = run_static_exact_qor_comparison(
                args.platform,
                parse_static_exact_qor_arms(args.arm),
                args.out,
                reuse_validated_phase6_equivalence=(
                    args.reuse_validated_phase6_equivalence
                ),
            )
        else:
            report = validate_static_exact_qor_comparison(
                args.root,
                args.platform,
                parse_static_exact_qor_arms(args.arm),
                reuse_validated_phase6_equivalence=(
                    args.reuse_validated_phase6_equivalence
                ),
            )
        _print_json(report)
        return 0

    if args.command == "experiment-cache":
        if args.experiment_command == "implementation-closure":
            report = build_implementation_closure(args.root, args.component)
            write_json(args.out, report)
        elif args.experiment_command == "implementation-validate":
            report = validate_implementation_closure(
                read_json(args.manifest), root=args.root
            )
        elif args.experiment_command == "inventory":
            report = inventory_experiment_store(args.cache)
            if args.out is not None:
                write_json(args.out, report)
        elif args.experiment_command == "evidence-create":
            report = create_experiment_evidence_bundle(
                args.plan, args.terminal, args.out
            )
        elif args.experiment_command == "evidence-validate":
            report = validate_experiment_evidence_bundle(args.bundle)
        elif args.experiment_command == "gc-plan":
            report = plan_experiment_gc(
                args.cache,
                args.root_plan,
                args.out,
                minimum_age_seconds=args.minimum_age_seconds,
            )
        elif args.experiment_command == "gc-apply":
            report = apply_experiment_gc(
                args.plan, args.expected_plan_sha256
            )
        elif args.experiment_command == "migration-plan":
            report = plan_legacy_run_migration(args.root, args.out)
        elif args.experiment_command == "retirement-plan":
            report = plan_legacy_run_retirement(
                args.migration_plan,
                args.name,
                args.out,
                reason=args.reason,
            )
        elif args.experiment_command == "retirement-apply":
            report = apply_legacy_run_retirement(
                args.plan,
                args.expected_plan_sha256,
                args.receipt_root,
            )
        elif args.experiment_command == "retirement-resume":
            report = resume_legacy_run_retirement(
                args.receipt_root,
                args.expected_receipt_sha256,
            )
        elif args.experiment_command == "plan":
            report = plan_experiment(args.spec, args.cache, args.out)
        elif args.experiment_command == "import":
            report = import_experiment_checkpoint(
                args.plan,
                args.node,
                args.artifact_root,
                expected_plan_sha256=args.expected_plan_sha256,
            )
        elif args.experiment_command == "farm-spec":
            report = build_experiment_farm_spec(
                args.plan,
                args.install_dir,
                args.node,
                args.farm_id,
                args.out,
                args.experiment_node,
                worker_launcher=args.worker_launcher,
                worker_argv=args.worker_arg or None,
            )
        else:
            report = run_experiment_node(
                args.plan,
                args.node,
                args.run_dir,
                expected_plan_sha256=args.expected_plan_sha256,
            )
        _print_json(report)
        return 0 if report.get("status") != "failed" else 2

    if args.command == "archive":
        if args.archive_command == "create":
            report = create_validation_archive(
                args.flow,
                args.out,
                run_id=args.run_id,
                source_commit=args.source_commit,
                max_copy_bytes=args.max_copy_bytes,
                tool_versions=_keyed_values(
                    args.tool_version, "--tool-version"
                ),
            )
        elif args.archive_command == "validate":
            report = validate_validation_archive(args.archive)
        else:
            report = cleanup_validation_source(args.archive, args.flow)
        _print_json(report)
        return 0

    if args.command == "validation-farm":
        if args.farm_command == "prepare":
            report = prepare_validation_farm(
                args.spec,
                args.out,
                ssh_known_hosts_file=args.ssh_known_hosts,
            )
        elif args.farm_command == "validate":
            report = validate_validation_farm(args.farm)
        elif args.farm_command == "launch":
            report = launch_validation_farm(
                args.farm, submit_workers=args.submit_workers
            )
        elif args.farm_command == "status":
            report = validation_farm_status(args.farm)
        elif args.farm_command == "reconcile":
            report = reconcile_validation_farm(args.farm)
        elif args.detach:
            report = detach_validation_farm_task(args.task)
        else:
            report = run_validation_farm_task(args.task)
        _print_json(report)
        return 0 if report.get("status") not in {"failed", "submit_failed"} else 2

    if args.command == "platform":
        if args.platform_command == "arm-mps4-materialize":
            report = materialize_arm_mps4_boarddb(
                output_path=args.output,
                name=args.name,
                fabric_clock_mhz=args.fabric_clock_mhz,
                payload_bits_per_lane_per_cycle=(
                    args.payload_bits_per_lane_per_cycle
                ),
                latency_cycles=args.latency_cycles,
                utilization_limit=args.utilization_limit,
            )
            _print_json(report)
            return 0
        if args.platform_command == "overlay-validate":
            report = validate_board_support_overlay_file(
                platform_path=args.platform,
                overlay_path=args.overlay,
                normalized_out=args.normalized_out,
            )
            _print_json(report)
            return 0
        if args.platform_command == "vivado-derive-gt-sites":
            report = derive_vivado_pin_sites(
                platform_path=args.platform,
                vivado_executable=args.vivado,
                output_dir=args.out,
            )
            _print_json(report)
            return 0
        if args.platform_command == "link-timing-model":
            platform = Platform.load(args.platform)
            database = build_board_link_timing_model(platform)
            write_json(args.output, database)
            report = validate_board_link_timing(database, platform)
            report["output"] = str(args.output)
            _print_json(report)
            return 0
        if args.platform_command == "link-timing-validate":
            platform = Platform.load(args.platform)
            database = read_json(args.input)
            _print_json(validate_board_link_timing(database, platform))
            return 0
        platform = Platform.load(args.path)
        if args.normalized_out is not None:
            write_json(args.normalized_out, platform.to_dict())
        _print_json(platform.summary())
        return 0

    if args.command == "phy-provider":
        if args.phy_provider_command == "materialize-recipe":
            report = materialize_serial_phy_recipe(
                manifest_path=args.manifest,
                part=args.part,
                vivado_executable=args.vivado,
                output_dir=args.out,
                platform_path=args.platform,
            )
            _print_json(report)
            return 0
        if args.phy_provider_command == "elaborate":
            report = run_serial_phy_elaboration(
                platform_path=args.platform,
                provider_manifest_path=args.manifest,
                phase6c_dir=args.phase6c_dir,
                runtime_controller_path=args.runtime_controller,
                transport_rtl_paths=_keyed_paths(
                    args.transport, "--transport"
                ),
                yosys_executable=args.yosys,
                vivado_executable=args.vivado,
                output_dir=args.out,
            )
            _print_json(report)
            return 0
        report = validate_serial_phy_provider_file(
            manifest_path=args.manifest,
            platform_path=args.platform,
            normalized_out=args.normalized_out,
        )
        _print_json(report)
        return 0

    if args.command == "runtime-sync":
        if args.runtime_sync_command == "validate-provider":
            report = validate_runtime_sync_provider(
                read_json(args.provider), args.provider
            )
            report = {key: value for key, value in report.items() if key != "normalized"}
        else:
            report = run_runtime_sync_materialization(
                platform_path=args.platform,
                provider_path=args.provider,
                output_dir=args.out,
                root=args.root,
                ready_stable_cycles=args.ready_stable_cycles,
            )
        _print_json(report)
        return 0

    if args.command == "contest":
        if args.contest_command == "matrix-validate":
            _, report = load_contest_validation_matrix(args.matrix)
        elif args.contest_command == "fetch-public":
            report = fetch_public_contest_case(
                args.matrix, args.case_id, args.out
            )
        elif args.contest_command == "import-public":
            report = import_public_contest_case(
                args.matrix, args.case_id, args.source_dir, args.out
            )
        elif args.contest_command == "evaluate-public":
            report = evaluate_public_contest_case(
                args.matrix,
                args.case_id,
                args.source_dir,
                args.import_dir,
                args.routes,
                args.out,
                tdm_plan_path=args.tdm_plan,
                solution_path=args.solution,
                new_topology_path=args.new_topology,
                runtime_seconds=args.runtime_seconds,
                expected_routes_sha256=args.expected_routes_sha256,
                expected_tdm_plan_sha256=args.expected_tdm_plan_sha256,
                expected_solution_sha256=args.expected_solution_sha256,
                expected_topology_sha256=args.expected_topology_sha256,
            )
        elif args.contest_command == "validate-public-evaluation":
            report = validate_public_contest_evaluation(args.matrix, args.bundle)
        elif args.contest_command == "materialize-public-boarddb":
            report = materialize_public_contest_boarddb(
                args.matrix,
                args.case_id,
                args.source_dir,
                args.import_dir,
                args.device_template,
                args.out,
                lane_scale=args.lane_scale,
                unweighted_link_lanes=args.unweighted_link_lanes,
                fabric_clock_mhz=args.fabric_clock_mhz,
                latency_cycles=args.latency_cycles,
            )
        elif args.contest_command == "matrix-fetch-farm-spec":
            report = build_contest_fetch_farm_spec(
                args.matrix,
                source_commit=args.source_commit,
                install_dir=args.install_dir,
                nodes=args.node,
                output_path=args.output,
                farm_id=args.farm_id,
                tiers=args.tier or ("smoke",),
                suites=args.suite,
                slots_per_node=args.slots_per_node,
                ssl_cert_file=args.ssl_cert_file,
            )
        elif args.contest_command == "matrix-import-farm-spec":
            report = build_contest_import_farm_spec(
                args.matrix,
                args.fetch_farm,
                source_commit=args.source_commit,
                install_dir=args.install_dir,
                nodes=args.node,
                output_path=args.output,
                farm_id=args.farm_id,
                tiers=args.tier or ("smoke",),
                suites=args.suite,
                slots_per_node=args.slots_per_node,
            )
        elif args.contest_command == "matrix-boarddb-farm-spec":
            report = build_contest_boarddb_farm_spec(
                args.matrix,
                args.fetch_farm,
                args.import_farm,
                source_commit=args.source_commit,
                install_dir=args.install_dir,
                nodes=args.node,
                output_path=args.output,
                farm_id=args.farm_id,
                tiers=args.tier or ("smoke",),
                suites=args.suite,
                slots_per_node=args.slots_per_node,
                lane_scale=args.lane_scale,
                unweighted_link_lanes=args.unweighted_link_lanes,
            )
        elif args.contest_command == "matrix-evaluate-farm-spec":
            report = build_contest_evaluation_farm_spec(
                args.matrix,
                args.fetch_farm,
                args.import_farm,
                args.candidates_root,
                source_commit=args.source_commit,
                install_dir=args.install_dir,
                nodes=args.node,
                output_path=args.output,
                farm_id=args.farm_id,
                tiers=args.tier or ("smoke",),
                suites=args.suite,
                slots_per_node=args.slots_per_node,
                runtime_seconds=args.runtime_seconds,
            )
        elif args.contest_command == "eda2023-import":
            report = import_eda2023_case(
                case_dir=args.case_dir,
                output_dir=args.out,
                name=args.name,
            )
        elif args.contest_command == "eda2023-materialize-boarddb":
            report = materialize_eda2023_rtl_boarddb(
                instance_path=args.instance,
                device_template_path=args.device_template,
                output_path=args.output,
                name=args.name,
                template_fpga_id=args.template_fpga,
                lane_scale=args.lane_scale,
                fabric_clock_mhz=args.fabric_clock_mhz,
                latency_cycles=args.latency_cycles,
                link_mode=args.link_mode,
                route_constraints_path=args.route_constraints_output,
            )
        elif args.contest_command == "eda2023-optimize":
            report = optimize_eda2023_tdm(
                instance_path=args.instance,
                routes_path=args.routes,
                output_dir=args.out,
                optimizer=args.optimizer,
                max_iterations=args.max_iterations,
                post_refinement_iterations=args.post_refinement_iterations,
                exact_domain_limit=args.exact_domain_limit,
            )
        elif args.contest_command == "eda2023-evaluate":
            report = evaluate_eda2023_solution(
                instance_path=args.instance,
                routes_path=args.routes,
                tdm_plan_path=args.tdm_plan,
            )
            if args.output is not None:
                write_json(args.output, report)
        elif args.contest_command == "eda2023-chimew-ab":
            report = run_eda2023_contest_chimew_ab(
                import_dir=args.import_dir,
                routes_path=args.routes,
                tdm_plan_path=args.tdm_plan,
                output_dir=args.out,
                grouper=args.grouper,
                refiner=args.refiner,
                rudy=args.rudy,
                assigner=args.assigner,
                pin_planner=args.pin_planner,
            )
        elif args.contest_command == "eda2024-evaluate":
            solution = args.solution or args.case_dir / "design.fpga.out"
            report = evaluate_eda2024_solution(
                info_path=args.case_dir / "design.info",
                area_path=args.case_dir / "design.are",
                net_path=args.case_dir / "design.net",
                topology_path=args.case_dir / "design.topo",
                solution_path=solution,
                runtime_seconds=args.runtime_seconds,
                output_path=args.output,
            )
        elif args.contest_command == "eda2024-materialize-boarddb":
            report = materialize_eda2024_rtl_boarddb(
                case_dir=args.case_dir,
                device_template_path=args.device_template,
                output_path=args.output,
                name=args.name,
                lanes_per_edge=args.lanes_per_edge,
                template_fpga_id=args.template_fpga,
                fabric_clock_mhz=args.fabric_clock_mhz,
                latency_cycles=args.latency_cycles,
                link_mode=args.link_mode,
                route_constraints_path=args.route_constraints_output,
            )
        elif args.contest_command == "iccad2019-import":
            report = import_iccad2019_instance(
                input_path=args.input,
                output_dir=args.out,
                name=args.name,
            )
        elif args.contest_command == "iccad2019-materialize-boarddb":
            report = materialize_iccad2019_rtl_boarddb(
                instance_path=args.instance,
                device_template_path=args.device_template,
                output_path=args.output,
                name=args.name,
                template_fpga_id=args.template_fpga,
                lane_scale=args.lane_scale,
                fabric_clock_mhz=args.fabric_clock_mhz,
                latency_cycles=args.latency_cycles,
                link_mode=args.link_mode,
            )
        elif args.contest_command == "iccad2019-optimize":
            report = optimize_iccad2019_ratios(
                instance_path=args.instance,
                routes_path=args.routes,
                output_path=args.output,
                optimizer=args.optimizer,
                max_iterations=args.max_iterations,
                post_refinement_iterations=args.post_refinement_iterations,
            )
        elif args.contest_command == "iccad2019-evaluate":
            report = evaluate_iccad2019_solution(
                instance_path=args.instance,
                solution_path=args.solution,
                runtime_seconds=args.runtime_seconds,
                median_runtime_seconds=args.median_runtime_seconds,
            )
        elif args.contest_command == "eda2025-import":
            report = import_eda2025_instance(
                info_path=args.info,
                net_path=args.net,
                topology_path=args.topology,
                assignment_path=args.assignment,
                output_dir=args.out,
                name=args.name,
                alpha_ns=args.alpha_ns,
                beta_ns=args.beta_ns,
                ratio_quantum=args.ratio_quantum,
                max_ratio=args.max_ratio,
                topology_change_fraction=args.topology_change_fraction,
            )
        elif args.contest_command == "eda2025-evaluate":
            report = evaluate_eda2025_routes(
                instance_path=args.instance,
                routes_path=args.routes,
                output_path=args.output,
                new_topology_path=args.new_topology,
                runtime_seconds=args.runtime_seconds,
                official_output_dir=args.official_out,
            )
        elif args.contest_command == "eda2025-optimize-topology":
            report = optimize_eda2025_topology(
                instance_path=args.instance,
                routes_path=args.routes,
                output_dir=args.out,
                executable=args.optimizer,
                max_changes=args.max_changes,
                current_topology_path=args.topology,
                enable_shortcuts=args.enable_shortcuts,
            )
        elif args.contest_command == "eda2025-optimize-routing":
            report = optimize_eda2025_routing(
                instance_path=args.instance,
                routes_path=args.routes,
                output_dir=args.out,
                router=args.router,
                topology_optimizer=args.topology_optimizer,
                current_topology_path=args.topology,
                enable_shortcut_portfolio=not args.capacity_only,
                max_rounds=args.max_rounds,
            )
        elif args.contest_command == "eda2025-materialize-boarddb":
            report = materialize_eda2025_rtl_boarddb(
                instance_path=args.instance,
                device_template_path=args.device_template,
                output_path=args.output,
                name=args.name,
                topology_path=args.topology,
                template_fpga_id=args.template_fpga,
                lane_scale=args.lane_scale,
                fabric_clock_mhz=args.fabric_clock_mhz,
                latency_cycles=args.latency_cycles,
                link_mode=args.link_mode,
                route_constraints_path=args.route_constraints_output,
            )
        else:
            raise AssertionError(f"unhandled contest command {args.contest_command!r}")
        _print_json(report)
        return 0

    if args.command == "ir":
        ir = EmuIR.load(args.path)
        if args.ir_command == "validate":
            _print_json(
                {
                    "schema": ir.value["schema"],
                    "design": ir.value["design"]["name"],
                    "status": "valid",
                }
            )
        else:
            _print_json(ir.stats())
        return 0

    if args.command == "combinational-cut":
        ir = EmuIR.load(args.ir)
        if args.combinational_cut_command == "characterize":
            report = characterize_combinational_cuts(
                ir, args.depth_limit or (1, 2)
            )
            write_json(args.output, report)
        else:
            report = validate_combinational_cut_characterization(
                ir, read_json(args.report)
            )
        _print_json(report)
        return 0

    if args.command == "import-yosys":
        ir = import_yosys_json(args.input, top=args.top, clocks=args.clock)
        write_json(args.output, ir.to_dict())
        _print_json(ir.stats())
        return 0

    if args.command == "synth-yosys":
        run_yosys(
            sources=args.sources,
            top=args.top,
            output=args.output,
            family=args.family,
            policy=args.policy,
            verilog_output=args.verilog_output,
            executable=args.yosys,
            log_path=args.log,
            include_dirs=args.include_dir,
            defines=args.define,
        )
        _print_json(
            {
                "family": args.family,
                "policy": args.policy,
                "output": str(args.output),
                "verilog_output": (
                    str(args.verilog_output)
                    if args.verilog_output is not None
                    else None
                ),
                "sources": [str(source) for source in args.sources],
                "include_dirs": [str(path) for path in args.include_dir],
                "defines": list(args.define),
                "status": "pass",
                "top": args.top,
            }
        )
        return 0

    if args.command == "vpr":
        if args.vpr_command in {"fpga-open", "full-open"}:
            report = run_open_physical_flow(
                sources=args.sources,
                top=args.top,
                output_dir=args.out,
                architecture=args.architecture,
                architecture_id=args.architecture_id,
                hard_blocks=not args.logic_only,
                yosys=args.yosys,
                vpr=args.vpr,
                architecture_importer=args.architecture_importer,
                packed_importer=args.packed_importer,
                route_checker=args.route_checker,
                openparf_install=args.openparf_install,
                openparf_python=args.openparf_python,
                seed=args.seed,
                route_channel_width=args.route_channel_width,
            )
        elif args.vpr_command == "synth":
            report = run_vtr_yosys(
                sources=args.sources,
                top=args.top,
                output=args.output,
                executable=args.yosys,
                log_path=args.log,
                hard_blocks=args.hard_blocks,
            )
        elif args.vpr_command == "run":
            report = run_vpr(
                architecture=args.architecture,
                circuit=args.circuit,
                output_dir=args.out,
                executable=args.vpr,
                seed=args.seed,
                route_channel_width=args.route_channel_width,
            )
        elif args.vpr_command == "import-packed":
            report = run_packed_netlist_import(
                packed_netlist_path=args.input,
                output_path=args.output,
                architecture_path=args.architecture,
                circuit_path=args.circuit,
                executable=args.importer,
            )
        elif args.vpr_command == "validate-packed":
            report = validate_packed_netlist_file(
                args.input,
                architecture_path=args.architecture,
                circuit_path=args.circuit,
            )
        elif args.vpr_command == "place-openparf":
            report = run_packed_openparf_placement(
                args.packed,
                args.architecture_db,
                args.out,
                seed_placement_path=args.seed_placement,
                openparf_install=args.openparf_install,
                openparf_python=args.openparf_python,
            )
        elif args.vpr_command == "route-packed":
            report = run_vpr_route_packed(
                architecture=args.architecture,
                circuit=args.circuit,
                packed_netlist=args.packed_netlist,
                packed_contract=args.packed_contract,
                placement=args.placement,
                output_dir=args.out,
                executable=args.vpr,
                route_checker=args.route_checker,
                route_channel_width=args.route_channel_width,
            )
        else:
            report = validate_vpr_route_artifacts(
                args.route,
                args.rr_graph,
                args.packed_contract,
                args.placement,
                args.output,
                executable=args.checker,
            )
        _print_json(report)
        return 0 if report["status"] == "pass" else 2

    if args.command == "benchmark":
        report = run_benchmark(
            spec_path=args.spec,
            source_root=args.source_root,
            output_dir=args.out,
            yosys=args.yosys,
        )
        _print_json(report)
        return 0 if report["status"] == "pass" else 2

    if args.command == "benchmark-matrix-validate":
        _, report = load_end_to_end_validation_matrix(args.matrix)
        _print_json(report)
        return 0

    if args.command == "benchmark-experiment-compile":
        report = compile_canonical_experiment_spec(
            args.config, args.repository_root, args.out
        )
        _print_json(report)
        return 0

    if args.command == "benchmark-partition-experiment-compile":
        report = compile_partition_qualification_spec(
            args.config, args.repository_root, args.out
        )
        _print_json(report)
        return 0

    if args.command == "benchmark-static-exact-ab-compile":
        report = compile_static_exact_ab_experiment_spec(
            args.config,
            args.repository_root,
            args.out,
            legacy_max_depth=args.legacy_max_depth,
            generalized_max_depth=args.generalized_max_depth,
            minimum_combinational_cut_nets=(
                args.minimum_combinational_cut_nets
            ),
            partition_seed=args.partition_seed,
        )
        _print_json(report)
        return 0

    if args.command == "phase1":
        report = run_phase1(
            yosys_json=args.yosys_json,
            platform_path=args.platform,
            output_dir=args.out,
            top=args.top,
            clocks=args.clock,
            require_no_fabric_clock=args.require_no_fabric_clock,
        )
        _print_json(report)
        return 0 if report["status"] == "pass" else 2

    if args.command == "arch":
        if args.arch_command == "import-vivado-tsv":
            architecture = ArchitectureDB.from_vivado_tsv(args.input)
            write_json(args.output, architecture.to_dict())
            report = architecture.summary()
        elif args.arch_command == "import-fpga-interchange":
            report = run_fpga_interchange_architecture_import(
                input_path=args.input,
                part=args.part,
                generator=args.generator,
                output_path=args.output,
                executable=args.native,
                log_path=args.log,
            )
        elif args.arch_command == "import-vtr":
            if args.reference_placement is not None:
                if args.width is not None or args.height is not None:
                    raise EmuFlowError(
                        "--reference-placement cannot be combined with "
                        "--width or --height"
                    )
                width, height = read_vpr_placement_dimensions(
                    args.reference_placement
                )
            elif args.width is None or args.height is None:
                raise EmuFlowError(
                    "import-vtr requires either --reference-placement or "
                    "both --width and --height"
                )
            else:
                width, height = args.width, args.height
            report = run_vtr_architecture_import(
                input_path=args.input,
                architecture_output_path=args.architecture_output,
                timing_output_path=args.timing_output,
                architecture_id=args.architecture_id,
                width=width,
                height=height,
                source_url=args.source_url,
                executable=args.native,
            )
        elif args.arch_command == "fetch-default-vtr":
            report = fetch_pinned_vtr_architecture(args.output)
        elif args.arch_command == "validate-vtr":
            report = validate_vtr_architecture_db(
                ArchitectureDB.load(args.path)
            )
        elif args.arch_command == "validate-vtr-timing":
            report = validate_vtr_timing_db_file(args.path)
        elif args.arch_command == "validate-fpga-interchange":
            architecture = ArchitectureDB.load(args.path)
            report = validate_fpga_interchange_architecture(architecture)
        elif args.arch_command == "check-capacity":
            architecture = ArchitectureDB.load(args.arch)
            report = check_ir_architecture_capacity(
                architecture, EmuIR.load(args.ir)
            )
        elif args.arch_command == "merge-physical-regions":
            report = run_physical_region_merge(
                architecture_path=args.arch,
                sidecar_path=args.sidecar,
                output_path=args.output,
            )
        elif args.arch_command == "validate-physical-regions":
            report = validate_fpga_interchange_architecture_regions(
                ArchitectureDB.load(args.path)
            )
        else:
            architecture = ArchitectureDB.load(args.path)
            report = architecture.summary()
        _print_json(report)
        return 0 if report.get("status", "pass") == "pass" else 2

    if args.command == "placement":
        architecture = ArchitectureDB.load(args.arch)
        ir = EmuIR.load(args.ir) if args.ir is not None else None
        if args.placement_command == "import-openparf":
            assert ir is not None
            placement = Placement.from_openparf_pl(
                args.input, architecture, ir
            )
            write_json(args.output, placement.to_dict())
            if args.xdc is not None:
                args.xdc.parent.mkdir(parents=True, exist_ok=True)
                args.xdc.write_text(placement.to_xdc(), encoding="utf-8")
        else:
            placement = Placement.load(args.path, architecture, ir)
        _print_json(placement.summary())
        return 0

    if args.command == "phase2":
        report = run_phase2(
            ir_path=args.ir,
            architecture_path=args.arch,
            output_dir=args.out,
            openparf_result=args.openparf_result,
            openparf_global_result=args.openparf_global_result,
            site_utilization_limit=args.site_utilization_limit,
            site_y_range=(
                tuple(args.site_y_range)
                if args.site_y_range is not None
                else None
            ),
            openparf_install=args.openparf_install,
            openparf_python=args.openparf_python,
            reference_placement=args.reference_placement,
        )
        _print_json(report)
        return 0

    if args.command == "multi-fpga":
        if args.multi_fpga_command == "validate":
            _print_json(
                validate_multi_fpga_flow_bundle(
                    args.flow,
                    minimum_combinational_cut_nets=(
                        args.minimum_combinational_cut_nets
                    ),
                    require_physical=args.require_physical,
                )
            )
            return 0
        if args.multi_fpga_command == "compare-routing-tdm-scale":
            report = build_system_route_tdm_scale_comparison(
                args.assignment,
                args.platform,
                args.route_constraints,
                args.timing_paths,
                args.baseline_route,
                args.baseline_tdm,
                args.upgrade_route,
                args.upgrade_tdm,
                args.output,
                baseline_runtime_seconds=args.baseline_runtime_seconds,
                upgrade_runtime_seconds=args.upgrade_runtime_seconds,
            )
            _print_json(report["validation"])
            return 0
        if args.multi_fpga_command == "finalize-physical":
            report = finalize_multi_fpga_physical_checkpoint(
                args.flow,
                args.physical,
                runtime_directory=args.runtime_directory,
            )
            _print_json(report["summary"])
            return 0
        if args.multi_fpga_command == "compare-routing-tdm":
            report = build_system_route_tdm_ab_comparison(
                args.baseline,
                args.upgrade,
                args.output,
            )
            _print_json(report["validation"])
            return 0
        if args.multi_fpga_command == "board-validate":
            _print_json(validate_vivado_board_flow_bundle(args.board))
            return 0
        if args.multi_fpga_command == "board-timing":
            report = run_vivado_board_timing(
                flow_root=args.flow,
                board_root=args.board,
                platform_path=args.platform,
                vivado_executable=args.vivado,
                output_dir=args.out,
                hierarchy_prefix=args.hierarchy_prefix,
                workers=args.workers,
                resume=args.resume,
                link_timing_path=args.link_timing_db,
            )
            _print_json(report["summary"])
            return 0
        if args.multi_fpga_command == "board-implement":
            report = run_vivado_board_flow(
                flow_root=args.flow,
                bsp_root=args.bsp,
                platform_path=args.platform,
                phy_provider_path=args.phy_provider,
                vivado_executable=args.vivado,
                output_dir=args.out,
                place_directive=args.place_directive,
                route_directive=args.route_directive,
                write_bitstream=args.write_bitstream,
            )
            _print_json(report["summary"])
            return 0
        if args.multi_fpga_command == "bsp":
            report = run_multi_fpga_bsp_flow(
                flow_root=args.flow,
                platform_path=args.platform,
                phy_provider_path=args.phy_provider,
                runtime_sync_provider_path=args.runtime_sync_provider,
                output_dir=args.out,
                board_overlay_path=args.board_overlay,
                gt_site_map_path=args.gt_site_map,
                vivado_executable=args.vivado,
                yosys_executable=args.yosys,
                runtime_sync_root=args.runtime_sync_root,
                ready_stable_cycles=args.ready_stable_cycles,
            )
            _print_json(report["summary"])
            return 0
        if args.multi_fpga_command == "physical":
            report = run_multi_fpga_physical_flow(
                split_root=args.split,
                platform_path=args.platform,
                schedule_path=args.schedule,
                output_dir=args.out,
                backend=args.backend,
                architecture=args.architecture,
                architecture_id=args.architecture_id,
                yosys=args.yosys,
                vpr=args.vpr,
                architecture_importer=args.architecture_importer,
                packed_importer=args.packed_importer,
                route_checker=args.route_checker,
                openparf_install=args.openparf_install,
                openparf_python=args.openparf_python,
                seed=args.seed,
                route_channel_width=args.route_channel_width,
                vivado=args.vivado,
                vivado_max_timing_paths=args.vivado_max_timing_paths,
                vivado_place_directive=args.vivado_place_directive,
                vivado_route_directive=args.vivado_route_directive,
                original_ir_path=args.original_ir,
                assignment_path=args.assignment,
                routes_path=args.routes,
                path_database_path=args.path_database,
                logic_path_database_path=args.logic_path_database,
                workers=args.workers,
                resume=args.resume,
            )
            _print_json(report["summary"])
            return 0
        if args.archive_cleanup and args.archive_out is None:
            raise EmuFlowError("--archive-cleanup requires --archive-out")
        # Exact-mode list scheduling is already the concrete dependency-aware
        # realization.  Do not silently opt it into the ordinary CLI's slot
        # optimizer default.  An explicitly supplied nonzero value remains
        # visible and is rejected by run_multi_fpga_flow's exact-mode gate.
        if args.slot_refinement_iterations is None:
            args.slot_refinement_iterations = (
                0
                if args.cut_mode == "static-exact-combinational"
                else 200
            )
        report = run_multi_fpga_flow(
            platform_path=args.platform,
            output_dir=args.out,
            sources=args.sources,
            top=args.top,
            clocks=args.clock,
            yosys_json=args.yosys_json,
            yosys=args.yosys,
            mapping_profile=args.mapping_profile,
            partition_constraints=args.partition_constraints,
            partition_provider=args.partition_provider,
            seed=args.seed,
            min_used_fpgas=args.min_used_fpgas,
            balance_tolerance=args.balance_tolerance,
            openroad=args.openroad,
            repart=args.repart,
            patron_refiner=args.patron_refiner,
            patron_max_moves=args.patron_max_moves,
            patron_flow_refinement=args.patron_flow_refinement,
            patron_algorithm_version=args.patron_algorithm_version,
            partition_timeout_seconds=args.partition_timeout_seconds,
            partition_seed_attempts=args.partition_seed_attempts,
            partition_num_initial_solutions=(
                args.partition_num_initial_solutions
            ),
            partition_num_best_initial_solutions=(
                args.partition_num_best_initial_solutions
            ),
            partition_repair_min_used_fpgas=(
                args.partition_repair_min_used_fpgas
            ),
            partition_repair_balance=args.partition_repair_balance,
            cut_mode=args.cut_mode,
            max_cross_fpga_dependency_depth=(
                args.max_cross_fpga_dependency_depth
            ),
            static_exact_candidate_policy=(
                args.static_exact_candidate_policy
            ),
            mfspart_post_refinement_timing_path_beta=(
                args.mfspart_post_refinement_timing_path_beta
            ),
            timing_driven=args.timing_driven,
            timing_backend=args.timing_backend,
            clock_periods=(
                parse_clock_definitions(args.clock_period)
                if args.clock_period
                else None
            ),
            timing_model=args.timing_model,
            architecture_timing_db=args.architecture_timing_db,
            opensta=args.opensta,
            timing_vivado=args.timing_vivado,
            sta_max_paths=args.sta_max_paths,
            timing_criticality_scale=args.timing_criticality_scale,
            timing_criticality_exponent=(
                args.timing_criticality_exponent
            ),
            route_constraints=args.route_constraints,
            board_link_timing_db=args.board_link_timing_db,
            timing_paths=args.timing_paths,
            router=args.router,
            route_provider=args.route_provider,
            route_candidate_workers=args.route_candidate_workers,
            frame_slots=args.frame_slots,
            optimize_frame_slots=args.optimize_frame_slots,
            route_max_iterations=args.route_max_iterations,
            tdm_provider=args.tdm_provider,
            ratio_optimizer=args.ratio_optimizer,
            timing_dag_optimizer=args.timing_dag_optimizer,
            slot_optimizer=args.slot_optimizer,
            ratio_max_iterations=args.ratio_max_iterations,
            max_ratio=args.max_ratio,
            ratio_quantum=args.ratio_quantum,
            post_refinement_iterations=(
                args.post_refinement_iterations
            ),
            slot_refinement_iterations=args.slot_refinement_iterations,
            cross_stage_iterations=args.cross_stage_iterations,
            cross_stage_feedback_optimizer=(
                args.cross_stage_feedback_optimizer
            ),
            cross_stage_pair_pressure_weight=(
                args.cross_stage_pair_pressure_weight
            ),
            simulation_frames=args.simulation_frames,
            equivalence_cycles=args.equivalence_cycles,
            equivalence_seed=args.equivalence_seed,
            phase6_provider=args.phase6_provider,
            phase6_chimew_region_count=args.phase6_chimew_region_count,
            phase6_chimew_grouper=args.phase6_chimew_grouper,
            phase6_chimew_refiner=args.phase6_chimew_refiner,
            phase6_chimew_rudy=args.phase6_chimew_rudy,
            phase6_chimew_assigner=args.phase6_chimew_assigner,
            physical=args.physical,
            physical_backend=args.physical_backend,
            physical_architecture=args.physical_architecture,
            physical_architecture_id=args.physical_architecture_id,
            physical_vpr=args.physical_vpr,
            physical_architecture_importer=(
                args.physical_architecture_importer
            ),
            physical_packed_importer=args.physical_packed_importer,
            physical_route_checker=args.physical_route_checker,
            physical_openparf_install=args.physical_openparf_install,
            physical_openparf_python=args.physical_openparf_python,
            physical_seed=args.physical_seed,
            physical_route_channel_width=(
                args.physical_route_channel_width
            ),
            physical_vivado=args.physical_vivado,
            physical_vivado_max_timing_paths=(
                args.physical_vivado_max_timing_paths
            ),
            physical_vivado_place_directive=(
                args.physical_vivado_place_directive
            ),
            physical_vivado_route_directive=(
                args.physical_vivado_route_directive
            ),
            physical_workers=args.physical_workers,
            serial_bsp_phy_provider=args.serial_bsp_phy_provider,
            serial_bsp_runtime_sync_provider=(
                args.serial_bsp_runtime_sync_provider
            ),
            serial_bsp_board_overlay=args.serial_bsp_board_overlay,
            serial_bsp_gt_site_map=args.serial_bsp_gt_site_map,
            serial_bsp_vivado=args.serial_bsp_vivado,
            serial_bsp_yosys=args.serial_bsp_yosys,
            serial_bsp_runtime_sync_root=(
                args.serial_bsp_runtime_sync_root
            ),
            serial_bsp_ready_stable_cycles=(
                args.serial_bsp_ready_stable_cycles
            ),
        )
        if args.archive_out is not None:
            archive_report = create_validation_archive(
                args.out,
                args.archive_out,
                run_id=args.archive_run_id or args.out.resolve().name,
                source_commit=args.archive_source_commit,
                max_copy_bytes=args.archive_max_copy_bytes,
                tool_versions=_keyed_values(
                    args.archive_tool_version, "--archive-tool-version"
                ),
                run_configuration=_jsonable_cli_configuration(args),
            )
            result: Dict[str, Any] = {
                "flow": report,
                "archive": archive_report,
            }
            if args.archive_cleanup:
                result["cleanup"] = cleanup_validation_source(
                    args.archive_out, args.out
                )
            _print_json(result)
        else:
            _print_json(report)
        return 0 if report["status"] == "pass" else 2

    if args.command == "partition":
        report = validate_phase3(
            ir_path=args.ir,
            platform_path=args.platform,
            clusters_path=args.clusters,
            assignment_path=args.assignment,
        )
        _print_json(report)
        return 0

    if args.command == "phase3":
        report = run_phase3(
            ir_path=args.ir,
            platform_path=args.platform,
            output_dir=args.out,
            constraints_path=args.constraints,
            seed=args.seed,
            min_used_fpgas=args.min_used_fpgas,
            balance_tolerance=args.balance_tolerance,
            provider=args.provider,
            openroad=args.openroad,
            tritonpart_solution=args.tritonpart_solution,
            net_weights_path=args.net_weights,
            tritonpart_timeout_seconds=args.tritonpart_timeout_seconds,
            tritonpart_seed_attempts=args.tritonpart_seed_attempts,
            tritonpart_num_initial_solutions=(
                args.tritonpart_num_initial_solutions
            ),
            tritonpart_num_best_initial_solutions=(
                args.tritonpart_num_best_initial_solutions
            ),
            tritonpart_repair_min_used_fpgas=(
                args.tritonpart_repair_min_used_fpgas
            ),
            tritonpart_repair_balance=args.tritonpart_repair_balance,
            repart=args.repart,
            repart_solution=args.repart_solution,
            repart_timeout_seconds=args.repart_timeout_seconds,
            route_constraints_path=args.route_constraints,
            hop_refiner=args.hop_refiner,
            mfspart_coarsener=args.mfspart_coarsener,
            mfspart_initializer=args.mfspart_initializer,
            mfspart_refiner=args.mfspart_refiner,
            mfspart_refiner_checker=args.mfspart_refiner_checker,
            mfspart_legalizer=args.mfspart_legalizer,
            mfspart_post_refinement=args.mfspart_post_refinement,
            mfspart_post_refinement_early_stop=(
                args.mfspart_post_refinement_early_stop
            ),
            mfspart_post_refinement_bottleneck_beta=(
                args.mfspart_post_refinement_bottleneck_beta
            ),
            timing_path_database_path=args.timing_path_database,
            mfspart_post_refinement_timing_path_beta=(
                args.mfspart_post_refinement_timing_path_beta
            ),
            cut_mode=args.cut_mode,
            max_cross_fpga_dependency_depth=(
                args.max_cross_fpga_dependency_depth
            ),
            timing_database_path=args.timing_database,
            patron_refiner=args.patron_refiner,
            patron_max_moves=args.patron_max_moves,
            patron_flow_refinement=args.patron_flow_refinement,
            patron_algorithm_version=args.patron_algorithm_version,
            patron_initial_assignment_path=(
                args.patron_initial_assignment
            ),
            patron_initial_clusters_path=args.patron_initial_clusters,
            patron_physical_system_timing_path=(
                args.patron_physical_system_timing
            ),
            patron_physical_feedback_scale=(
                args.patron_physical_feedback_scale
            ),
            static_exact_candidate_policy=(
                args.static_exact_candidate_policy
            ),
        )
        _print_json(report)
        return 0 if report["status"] == "pass" else 2

    if args.command == "sta":
        if args.sta_command == "emit-vivado-cut-map":
            report = write_vivado_cut_net_map(
                args.ir, args.assignment, args.output
            )
        elif args.sta_command == "import-vivado-tsv":
            report = import_vivado_sta_tsv(
                args.input, args.assignment, args.output
            )
        elif args.sta_command == "emit-vivado-net-map":
            report = write_vivado_net_map(args.ir, args.output)
        elif args.sta_command == "import-vivado-path-database":
            report = import_vivado_path_database_tsv(
                args.input, args.ir, args.output
            )
        elif args.sta_command == "project-path-database":
            report = project_sta_path_database(
                args.database, args.assignment, args.output
            )
        elif args.sta_command == "validate-path-database":
            report = validate_sta_path_database(args.database, args.ir)
        elif args.sta_command == "derive-partition-net-weights":
            report = derive_partition_net_weights(
                args.database,
                args.ir,
                args.output,
                criticality_scale=args.criticality_scale,
                criticality_exponent=args.criticality_exponent,
            )
        else:
            clock_definitions = parse_clock_definitions(
                args.clock_period
            )
            report = run_opensta_path_database(
                ir_path=args.ir,
                output_path=args.output,
                clocks=clock_definitions or None,
                timing_model_path=args.timing_model,
                architecture_timing_db_path=args.architecture_timing_db,
                executable=args.opensta,
                max_paths=args.max_paths,
                log_path=args.log,
            )
        _print_json(report)
        return 0

    if args.command == "route":
        report = validate_phase4(
            assignment_path=args.assignment,
            platform_path=args.platform,
            routes_path=args.routes,
            timing_paths_path=args.timing_paths,
        )
        _print_json(report)
        return 0

    if args.command == "phase4":
        report = run_phase4(
            assignment_path=args.assignment,
            platform_path=args.platform,
            output_dir=args.out,
            constraints_path=args.constraints,
            frame_slots=args.frame_slots,
            max_iterations=args.max_iterations,
            provider=args.provider,
            timing_paths_path=args.timing_paths,
            router=args.router,
            tdm_feedback_path=args.tdm_feedback,
            tdm_feedback_routes_path=args.tdm_feedback_routes,
            tdm_feedback_schedule_path=args.tdm_feedback_schedule,
            tdm_feedback_ratio_plan_path=(
                args.tdm_feedback_ratio_plan
            ),
            physical_feedback_path=args.physical_feedback,
            physical_feedback_runtime_path=(
                args.physical_feedback_runtime
            ),
            physical_feedback_summary_path=(
                args.physical_feedback_summary
            ),
            physical_feedback_weight=args.physical_feedback_weight,
            candidate_workers=args.candidate_workers,
        )
        _print_json(report)
        return 0 if report["status"] == "pass" else 2

    if args.command == "schedule":
        report = validate_phase5(
            routes_path=args.routes,
            platform_path=args.platform,
            schedule_path=args.schedule,
            ratio_plan_path=args.ratio_plan,
        )
        _print_json(report)
        return 0

    if args.command == "phase5":
        phase5_routes = read_json(args.routes)
        phase5_slot_refinement_iterations = (
            0
            if phase5_routes.get("semantic_contract") is not None
            else args.slot_refinement_iterations
        )
        report = run_phase5(
            routes_path=args.routes,
            platform_path=args.platform,
            output_dir=args.out,
            simulation_frames=args.simulation_frames,
            provider=args.provider,
            ratio_optimizer=args.ratio_optimizer,
            timing_dag_optimizer=args.timing_dag_optimizer,
            slot_optimizer=args.slot_optimizer,
            ratio_max_iterations=args.ratio_max_iterations,
            max_ratio=args.max_ratio,
            ratio_quantum=args.ratio_quantum,
            post_refinement_iterations=args.post_refinement_iterations,
            slot_refinement_iterations=phase5_slot_refinement_iterations,
            convergence=args.ratio_convergence,
        )
        _print_json(report)
        return 0 if report["status"] == "pass" else 2

    if args.command == "partition-feedback":
        report = run_partition_feedback(
            routes_path=args.routes,
            ratio_plan_path=args.ratio_plan,
            platform_path=args.platform,
            output_path=args.output,
            executable=args.optimizer,
            pair_pressure_weight=args.pair_pressure_weight,
        )
        _print_json(report)
        return 0

    if args.command == "partition-pressure-reference":
        report = run_partition_pressure_reference(
            args.ir,
            args.platform,
            args.clusters,
            args.constraints,
            args.timing_database,
            args.route_constraints,
            args.initial_assignment,
            args.out,
            max_moves=args.max_moves,
        )
        _print_json(report)
        return 0

    if args.command == "cross-stage":
        if args.cross_stage_command == "evaluate":
            report = evaluate_cross_stage_candidate(
                args.database,
                args.assignment,
                args.routes,
                args.schedule,
                args.ratio_plan,
                args.platform,
                args.output,
            )
        elif args.cross_stage_command == "validate-candidate":
            report = validate_cross_stage_candidate(
                args.candidate,
                args.database,
                args.assignment,
                args.routes,
                args.schedule,
                args.ratio_plan,
                args.platform,
            )
        elif args.cross_stage_command == "validate-report":
            report = validate_cross_stage_report(
                args.report,
                args.ir,
                args.database,
                args.platform,
            )
        else:
            report = run_cross_stage_optimization(
                ir_path=args.ir,
                platform_path=args.platform,
                database_path=args.database,
                initial_assignment_path=args.initial_assignment,
                output_dir=args.out,
                seed_candidate_phase3_root=(
                    args.seed_candidate_phase3_root
                ),
                phase3_constraints_path=args.phase3_constraints,
                route_constraints_path=args.route_constraints,
                board_link_timing_path=args.board_link_timing_db,
                phase3_provider=args.phase3_provider,
                max_outer_iterations=args.max_outer_iterations,
                seed=args.seed,
                min_used_fpgas=args.min_used_fpgas,
                balance_tolerance=args.balance_tolerance,
                openroad=args.openroad,
                repart=args.repart,
                patron_refiner=args.patron_refiner,
                patron_max_moves=args.patron_max_moves,
                patron_flow_refinement=args.patron_flow_refinement,
                patron_algorithm_version=args.patron_algorithm_version,
                partition_timeout_seconds=(
                    args.partition_timeout_seconds
                ),
                partition_seed_attempts=args.partition_seed_attempts,
                partition_num_initial_solutions=(
                    args.partition_num_initial_solutions
                ),
                partition_num_best_initial_solutions=(
                    args.partition_num_best_initial_solutions
                ),
                partition_repair_min_used_fpgas=(
                    args.partition_repair_min_used_fpgas
                ),
                partition_repair_balance=(
                    args.partition_repair_balance
                ),
                router=args.router,
                route_provider=args.route_provider,
                route_candidate_workers=args.route_candidate_workers,
                frame_slots=args.frame_slots,
                optimize_frame_slots=args.optimize_frame_slots,
                route_max_iterations=args.route_max_iterations,
                tdm_provider=args.tdm_provider,
                ratio_optimizer=args.ratio_optimizer,
                timing_dag_optimizer=args.timing_dag_optimizer,
                slot_optimizer=args.slot_optimizer,
                feedback_optimizer=args.feedback_optimizer,
                simulation_frames=args.simulation_frames,
                ratio_max_iterations=args.ratio_max_iterations,
                max_ratio=args.max_ratio,
                ratio_quantum=args.ratio_quantum,
                post_refinement_iterations=(
                    args.post_refinement_iterations
                ),
                slot_refinement_iterations=(
                    args.slot_refinement_iterations
                ),
                ratio_convergence=args.ratio_convergence,
                pair_pressure_weight=args.pair_pressure_weight,
                feedback_steps=(
                    tuple(args.feedback_step)
                    if args.feedback_step
                    else None
                ),
            )
        _print_json(report)
        return 0

    if args.command == "pin-plan":
        if args.pin_plan_command == "chimew-materialize-ratios":
            report = materialize_chimew_schedule_ratios(read_json(args.schedule))
            write_json(args.output, report)
            _print_json(report["chimew_ratio_materialization"])
            return 0
        if args.pin_plan_command == "chimew-build":
            report = run_chimew_phase6_adapter(
                schedule_path=args.schedule,
                platform_path=args.platform,
                bank_channel_input_path=args.assignment_input,
                electrical_map_path=args.electrical_map,
                output_dir=args.out,
                qualification_path=args.qualification,
                bank_channel_report_path=args.assignment_report,
                executable=args.assigner,
                region_count=args.region_count,
            )
            _print_json(report)
            return 0
        if args.pin_plan_command == "chimew-qualify":
            report = build_chimew_phase6_qualification(
                read_json(args.schedule),
                read_json(args.crossings),
                read_json(args.initial_grouping),
                read_json(args.positions),
                read_json(args.refined_grouping),
                read_json(args.rudy_input),
                read_json(args.rudy_report),
                read_json(args.assignment_input),
                read_json(args.assignment_report),
            )
            write_json(args.output, report)
            _print_json(report)
            return 0
        if args.pin_plan_command == "chimew-run":
            source_options = {
                "routing": args.routing_source,
                "placement": args.placement_source,
                "netlist": args.netlist_source,
                "architecture": args.architecture_source,
                "package_pins": args.package_pins_source,
            }
            present_sources = {
                label: path
                for label, path in source_options.items()
                if path is not None
            }
            if present_sources and len(present_sources) != len(source_options):
                raise EmuFlowError(
                    "Chimew source binding requires all five source artifacts"
                )
            report = run_chimew_phase6_pipeline(
                schedule_path=args.schedule,
                platform_path=args.platform,
                crossings_path=args.crossings,
                positions_path=args.positions,
                rudy_input_path=args.rudy_input,
                bank_channel_input_path=args.assignment_input,
                electrical_map_path=args.electrical_map,
                output_dir=args.out,
                source_paths=present_sources or None,
                grouper=args.grouper,
                refiner=args.refiner,
                rudy=args.rudy,
                assigner=args.assigner,
                region_count=args.region_count,
            )
            _print_json(report)
            return 0
        if args.pin_plan_command == "chimew-validate":
            report = validate_chimew_phase6_pipeline(args.bundle)
            _print_json(report)
            return 0
        if args.pin_plan_command == "chimew-correlate":
            report = build_chimew_vivado_correlation(args.input, args.output)
            _print_json(report)
            return 0
        if args.pin_plan_command == "chimew-correlation-validate":
            report = validate_chimew_vivado_correlation(args.input, args.report)
            _print_json(report)
            return 0
        schedule = read_json(args.schedule)
        platform = Platform.load(args.platform)
        if args.pin_plan_command == "build":
            ir = EmuIR.load(args.ir)
            placements = {
                fpga: read_json(path)
                for fpga, path in _keyed_paths(
                    args.placement, "--placement"
                ).items()
            }
            positions = build_signal_position_hints(
                ir.value,
                schedule,
                placements,
                region_count=args.region_count,
            )
            plan = build_pin_plan(
                schedule,
                platform,
                positions,
                executable=args.planner,
                refinement_iterations=args.refinement_iterations,
                crossing_weight=args.crossing_weight,
                position_weight=args.position_weight,
            )
            write_json(args.positions_out, positions)
            write_json(args.output, plan)
            _print_json(
                {
                    "status": "pass",
                    "positions": positions["metrics"],
                    "plan": plan["metrics"],
                }
            )
        else:
            report = validate_pin_plan(
                schedule,
                platform,
                read_json(args.positions),
                read_json(args.plan),
            )
            _print_json(report)
        return 0

    if args.command == "split":
        report = validate_phase6(
            ir_path=args.ir,
            assignment_path=args.assignment,
            schedule_path=args.schedule,
            platform_path=args.platform,
            manifest_path=args.manifest,
            pin_plan_path=args.pin_plan,
            position_hints_path=args.position_hints,
            electrical_binding_path=args.electrical_binding,
        )
        _print_json(report)
        return 0

    if args.command == "phase6":
        report = run_phase6(
            ir_path=args.ir,
            assignment_path=args.assignment,
            schedule_path=args.schedule,
            platform_path=args.platform,
            output_dir=args.out,
            pin_plan_path=args.pin_plan,
            position_hints_path=args.position_hints,
            electrical_binding_path=args.electrical_binding,
            equivalence_cycles=args.equivalence_cycles,
            equivalence_seed=args.equivalence_seed,
        )
        _print_json(report)
        return 0 if report["status"] == "pass" else 2

    if args.command == "phase6b":
        report = run_phase6b(
            schedule_path=args.schedule,
            platform_path=args.platform,
            positions_path=args.position_hints,
            pin_plan_path=args.pin_plan,
            anchor_paths=_keyed_paths(args.anchor, "--anchor"),
            bsp_path=args.bsp,
            output_dir=args.out,
            executable=args.solver,
            iostandard=args.iostandard,
            placement_weight=args.placement_weight,
            skew_weight=args.skew_weight,
        )
        _print_json(report)
        return 0 if report["status"] == "pass" else 2

    if args.command == "phase6c":
        report = run_phase6c(
            platform_path=args.platform,
            binding_path=args.binding,
            output_dir=args.out,
            transport_paths=(
                _keyed_paths(args.transport, "--transport")
                if args.transport
                else None
            ),
            board_overlay_path=args.board_overlay,
            phy_provider_path=args.phy_provider,
            gt_site_map_path=args.gt_site_map,
            runtime_sync_topology_path=args.runtime_sync_topology,
            runtime_sync_provider_path=args.runtime_sync_provider,
        )
        _print_json(report)
        return 0 if report["status"] == "pass" else 2

    if args.command == "package-pin":
        platform = Platform.load(args.platform)
        schedule = read_json(args.schedule)
        positions = (
            read_json(args.position_hints)
            if args.position_hints is not None
            else None
        )
        plan = read_json(args.pin_plan) if args.pin_plan is not None else None
        anchors = {
            fpga: read_json(path)
            for fpga, path in _keyed_paths(
                args.anchor, "--anchor"
            ).items()
        }
        binding = read_json(args.binding)
        if binding.get("provider") == SERIAL_TRANSCEIVER_PROVIDER:
            if args.bsp is not None:
                parser.error(
                    "--bsp must be omitted for a BoardDB serial binding"
                )
            report = validate_serial_transceiver_binding(
                schedule, platform, positions, plan, anchors, binding
            )
        else:
            if args.bsp is None or positions is None or plan is None:
                parser.error(
                    "--bsp, --position-hints, and --pin-plan are required "
                    "for a parallel package-pin binding"
                )
            report = validate_package_pin_binding(
                schedule,
                platform,
                positions,
                plan,
                anchors,
                read_json(args.bsp),
                binding,
            )
        _print_json(report)
        return 0

    if args.command == "lower-placement-ir":
        report = run_placement_ir_lowering(
            netlist_path=args.netlist,
            transport_path=args.transport,
            transport_ir_path=args.transport_ir,
            output_path=args.output,
            report_path=args.report,
            boundary_identity_path=args.boundary_identities,
        )
        _print_json(report)
        return 0

    if args.command == "emit-mapped-verilog":
        report = emit_mapped_verilog(
            ir_path=args.ir,
            output_path=args.output,
            report_path=args.report,
        )
        _print_json(dict(report))
        return 0

    if args.command == "phase7c":
        report = run_phase7c(
            schedule_path=args.schedule,
            platform_path=args.platform,
            phase3_report_path=args.phase3_report,
            phase4_report_path=args.phase4_report,
            phase5_report_path=args.phase5_report,
            phase6_report_path=args.phase6_report,
            physical_summary_path=args.physical_summary,
            routes_path=args.routes,
            simulation_frames=args.simulation_frames,
            output_dir=args.out,
        )
        _print_json(report)
        return 0 if report["status"] in {"generated", "pass"} else 2

    if args.command == "phase7d":
        report = run_phase7d(
            benchmark_report_path=args.benchmark_report,
            phase3_report_path=args.phase3_report,
            phase4_report_path=args.phase4_report,
            phase5_report_path=args.phase5_report,
            phase6_report_path=args.phase6_report,
            phase7c_report_path=args.phase7c_report,
            runtime_contract_path=args.runtime_contract,
            qor_report_path=args.qor_report,
            physical_summary_path=args.physical_summary,
            platform_path=args.platform,
            lowering_report_paths=_keyed_paths(
                args.lowering_report, "--lowering-report"
            ),
            placement_report_paths=_keyed_paths(
                args.placement_report, "--placement-report"
            ),
            emission_report_paths=_keyed_paths(
                args.emission_report, "--emission-report"
            ),
            artifact_paths=_keyed_paths(args.artifact, "--artifact"),
            source_commit=args.source_commit,
            output_dir=args.out,
        )
        _print_json(report)
        return 0

    if args.command == "phase8a":
        report = run_phase8a(
            release_manifest_path=args.release_manifest,
            phase6_report_path=args.phase6_report,
            platform_path=args.platform,
            anchor_paths=_keyed_paths(args.anchor, "--anchor"),
            output_dir=args.out,
        )
        _print_json(report)
        return 0

    raise AssertionError(f"unhandled command {args.command!r}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except (EmuFlowError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f"emuflow: error: {error}", file=sys.stderr)
        return 1
