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

from .partition import DEFAULT_PARTITION_CUT_MODE, DEFAULT_PATRON_ALGORITHM_VERSION

from .architecture import ArchitectureDB
from .benchmark import BenchmarkRun, run_benchmark
from .board_arm_mps4 import materialize_arm_mps4_boarddb
from .board_link_timing import (
    build_board_link_timing_model,
    validate_board_link_timing,
)
from .board_support import validate_board_support_overlay_file
from .default_platform import (
    DEFAULT_PLATFORM_PATH,
    DEFAULT_WIRES_PER_BANK,
    RESEARCH_TRANSPORT_FORMATS,
    materialize_research_platform,
)
from .chimew_phase6 import run_chimew_phase6_adapter
from .chimew_bank_channel_storage import read_chimew_bank_channel_input
from .chimew_bank_channel_report_storage import (
    BankChannelReportView,
    read_chimew_bank_channel_report,
)
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
)
from .contest_iccad2019 import (
    evaluate_iccad2019_solution,
    import_iccad2019_instance,
    materialize_iccad2019_rtl_boarddb,
)
from .contest_public import (
    evaluate_public_contest_case,
    fetch_public_contest_case,
    import_public_contest_case,
    materialize_public_contest_boarddb,
    validate_public_contest_evaluation,
)
from .contest_validation_matrix import load_contest_validation_matrix
from .combinational_cut import (
    STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
    STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH,
    characterize_combinational_cuts,
    validate_combinational_cut_characterization,
)
from .rapidwright_provider import (
    load_rapidwright_provider_manifest,
    run_rapidwright_device_import,
    validate_rapidwright_architecture,
    validate_rapidwright_provider_manifest,
)
from .xilinx_packing import validate_xilinx_packing
from .xilinx_placement import (
    validate_xilinx_placement,
)
from .xilinx_rwroute import (
    export_rwroute_input,
    run_rwroute,
    validate_xilinx_route_db,
)
from .xilinx_timing import (
    build_xilinx_routed_timing,
    validate_xilinx_routed_timing,
)
from .xilinx_opensta import (
    run_xilinx_routed_opensta,
    validate_xilinx_routed_opensta_summary,
)
from .xilinx_preplacement_timing import (
    build_xilinx_preplacement_timing_db,
    import_vivado_ff_control_timing,
    validate_xilinx_preplacement_timing_db,
)
from .xilinx_primitives import XILINX_ULTRASCALEPLUS_OPEN_PROFILE
from .errors import EmuFlowError
from .fpga_interchange import (
    check_ir_architecture_capacity,
    run_fpga_interchange_architecture_import,
    validate_fpga_interchange_architecture,
)
from .io import read_json, write_json
from .ir import EmuIR
from .lowering import run_placement_ir_lowering
from .multi_fpga_flow import (
    finalize_multi_fpga_physical_checkpoint,
    run_multi_fpga_flow,
    validate_multi_fpga_flow_bundle,
)
from .multi_fpga_bsp_flow import run_multi_fpga_bsp_flow
from .multi_fpga_physical_flow import (
    run_multi_fpga_physical_flow,
)
from .physical_batch_assembly import assemble_vivado_physical_batches
from .opensta import (
    DEFAULT_TIMING_MODEL,
    parse_clock_definitions,
    run_opensta_path_database,
)
from .phase1 import run_phase1
from .phase3 import run_phase3, validate_phase3
from .phase4 import run_phase4, validate_phase4
from .phase5 import run_phase5, validate_phase5
from .phase6 import run_phase6, validate_phase6
from .phase7c import run_phase7c
from .physical_pins import (
    SERIAL_TRANSCEIVER_PROVIDER,
    run_phase6b,
    validate_package_pin_binding,
    validate_serial_transceiver_binding,
)
from .physical_regions import (
    run_physical_region_merge,
    run_physical_region_rebind,
    validate_fpga_interchange_architecture_regions,
)
from .platform import Platform
from .pin_planning import (
    validate_pin_plan,
)
from .pin_plan_storage import read_pin_plan
from .position_hints_storage import read_position_hints

from .runtime_sync import (
    run_runtime_sync_materialization,
    validate_runtime_sync_provider,
)
from .synthesis import (
    VALID_SYNTHESIS_POLICIES,
    VALID_XILINX_FAMILIES,
    VALID_XILINX_MAPPING_STRATEGIES,
    run_yosys,
    run_xilinx_ultrascaleplus_yosys,
)
from .sta import (
    derive_partition_net_weights,
    project_sta_path_database,
    validate_sta_path_database,
)
from .serial_wrapper import run_phase6c
from .serial_phy_provider import validate_serial_phy_provider_file
from .serial_phy_elaboration import run_serial_phy_elaboration
from .serial_phy_recipe import materialize_serial_phy_recipe
from .tdm import TDM_EXACT_DEPENDENCY_PROVIDER
from .tdm_schedule_storage import read_tdm_schedule
from .timing_routing import GLOBAL_CANDIDATE_PROVIDER
from .yosys import import_yosys_json
from .verilog import emit_mapped_verilog
from .vivado_board_flow import (
    run_vivado_board_flow,
    validate_vivado_board_flow_bundle,
)
from .vivado_board_timing import run_vivado_board_timing
from .vivado_pin_sites import derive_vivado_pin_sites

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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="emuflow",
        description="Open multi-FPGA emulation flow frontend",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    platform_parser = subparsers.add_parser("platform", help="BoardDB operations")
    platform_subparsers = platform_parser.add_subparsers(
        dest="platform_command", required=True
    )
    platform_validate = platform_subparsers.add_parser(
        "validate", help="validate and summarize a BoardDB"
    )
    platform_validate.add_argument("path", type=Path)
    platform_validate.add_argument("--normalized-out", type=Path)
    platform_utilization = platform_subparsers.add_parser(
        "derive-utilization-profile",
        help="derive a BoardDB with a uniform per-FPGA utilization limit",
    )
    platform_utilization.add_argument("--input", type=Path, required=True)
    platform_utilization.add_argument(
        "--output", "-o", type=Path, required=True
    )
    platform_utilization.add_argument(
        "--utilization-limit", type=float, required=True
    )
    platform_utilization.add_argument("--name", required=True)
    platform_research = platform_subparsers.add_parser(
        "research-model", help="materialize the shared-bank research platform"
    )
    platform_research.add_argument("--fpga-count", type=int, choices=(2, 4), default=4)
    platform_research.add_argument("--transport-format", choices=RESEARCH_TRANSPORT_FORMATS, default="abstract-bit-v1",
        help="explicit link format; native word physical integration remains under qualification")
    platform_research.add_argument(
        "--wires-per-bank", type=int, default=DEFAULT_WIRES_PER_BANK,
        help="differential data pairs per HP bank (default: 20; two pins per pair)",
    )
    platform_research.add_argument("--output", "-o", type=Path, required=True)
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
    eda2023_evaluate = contest_subparsers.add_parser(
        "eda2023-evaluate",
        help="independently check routed trees and a per-Wire TDM plan",
    )
    eda2023_evaluate.add_argument("--instance", type=Path, required=True)
    eda2023_evaluate.add_argument("--routes", type=Path, required=True)
    eda2023_evaluate.add_argument("--tdm-plan", type=Path, required=True)
    eda2023_evaluate.add_argument("--output", "-o", type=Path)
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
    synthesis.add_argument(
        "--mapping-profile",
        choices=[XILINX_ULTRASCALEPLUS_OPEN_PROFILE],
        help="enable the fail-closed Route A UltraScale+ primitive contract",
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
    multi_fpga_compile.add_argument(
        "--compile-implementation",
        choices=("python-control-v1", "native-control-v1"),
        default="python-control-v1",
        help="select orchestration; native control uses isolated shared phase adapters",
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
    multi_fpga_finalize.add_argument("--global-sta-executable", help="override the in-tree OpenSTA executable")
    multi_fpga_finalize.add_argument("--global-sta-results-dir", type=Path,
        help="recheck source-matched saved standalone OpenSTA measurements")
    multi_fpga_finalize.add_argument("--native-endpoint-vivado", help="Vivado for native endpoint budget observations")
    multi_fpga_finalize.add_argument("--native-endpoint-results-dir", type=Path,
        help="recheck saved per-FPGA native endpoint measurements without invoking Vivado")
    multi_fpga_compile.add_argument("sources", nargs="*", type=Path)
    multi_fpga_compile.add_argument(
        "--benchmark-run",
        type=Path,
        help=(
            "consume sources, top, clocks, clock periods, compilation context, "
            "and the Xilinx physical mapping profile from a checked calibration "
            "holdout contract"
        ),
    )
    multi_fpga_compile.add_argument(
        "--source-root",
        type=Path,
        help="source root used to resolve --benchmark-run",
    )
    multi_fpga_compile.add_argument("--top")
    multi_fpga_compile.add_argument("--prepared-frontend", type=Path, help="completed indexed Phase 1 boundary; skips synthesis")
    multi_fpga_compile.add_argument("--prepared-timing", type=Path, help="validated indexed Phase 2 boundary; requires --prepared-frontend")
    multi_fpga_compile.add_argument("--clock", action="append", default=[])
    multi_fpga_compile.add_argument(
        "--yosys-json",
        type=Path,
        help="use existing Yosys JSON instead of synthesizing RTL",
    )
    multi_fpga_compile.add_argument(
        "--platform", type=Path, default=DEFAULT_PLATFORM_PATH,
        help="BoardDB (default: bundled four-XCVU19P shared-bank research model)",
    )
    multi_fpga_compile.add_argument("--out", type=Path, required=True)
    multi_fpga_compile.add_argument("--yosys")
    multi_fpga_compile.add_argument(
        "--include-dir",
        action="append",
        default=[],
        type=Path,
        help="Verilog include directory passed to the selected Yosys frontend",
    )
    multi_fpga_compile.add_argument(
        "--define",
        action="append",
        default=[],
        help="Verilog preprocessor NAME or NAME=VALUE (repeatable)",
    )
    multi_fpga_compile.add_argument(
        "--mapping-profile",
        choices=(
            "xilinx-ultrascaleplus-open-v1",
        ),
        default=XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
        help=(
            "Xilinx UltraScale+ primitive mapping; the only supported compile profile"
        ),
    )
    multi_fpga_compile.add_argument("--partition-constraints", type=Path)
    multi_fpga_compile.add_argument(
        "--xilinx-mapping-strategy",
        choices=sorted(VALID_XILINX_MAPPING_STRATEGIES),
        help="Explicit native Xilinx mapper A/B selection; defaults to hierarchical ABC9",
    )
    multi_fpga_compile.add_argument(
        "--partition-provider",
        choices=(
            "tritonpart",
            "patron",
        ),
        default="patron",
    )
    multi_fpga_compile.add_argument("--seed", type=int, default=0)
    multi_fpga_compile.add_argument("--min-used-fpgas", type=int)
    multi_fpga_compile.add_argument(
        "--balance-tolerance", type=float,
        help=("Relative tolerance for configured balance_dimensions (default: "
              "cells, LUT, and FF); omitted dimensions impose only device "
              "capacity limits"),
    )
    multi_fpga_compile.add_argument("--openroad")
    multi_fpga_compile.add_argument(
        "--tritonpart-solution",
        type=Path,
        help=(
            "import a precomputed TritonPart .part initializer; useful for "
            "resuming Phase 3 after a validated provider boundary"
        ),
    )
    multi_fpga_compile.add_argument("--patron-refiner")
    multi_fpga_compile.add_argument("--patron-max-moves", type=int)
    multi_fpga_compile.add_argument(
        "--patron-algorithm-version",
        type=int,
        choices=(14,15),
        default=DEFAULT_PATRON_ALGORITHM_VERSION,
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
        action=_BooleanOptionalAction, default=True,
        help=(
            "minimally move legal atomic clusters if the partitioner "
            "leaves required FPGAs empty"
        ),
    )
    multi_fpga_compile.add_argument(
        "--cut-mode",
        choices=("static-exact-combinational",),
        default=DEFAULT_PARTITION_CUT_MODE,
        help=(
            "partition boundary semantics; generalized Static Exact is the default"
        ),
    )
    multi_fpga_compile.add_argument(
        "--max-cross-fpga-dependency-depth",
        type=int,
        default=STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH,
        help=(
            "optional Static Exact policy guard; zero (the default) leaves "
            "depth to schedule feasibility and final timing"
        ),
    )
    multi_fpga_compile.add_argument(
        "--static-exact-candidate-policy",
        choices=(STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,),
        default=STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
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
            "provider-matched Architecture TimingDB used to construct the "
            "pre-placement OpenSTA model"
        ),
    )
    multi_fpga_compile.add_argument(
        "--opensta",
        "--openroad-sta",
        dest="opensta",
        help="explicit OpenSTA executable override",
    )
    multi_fpga_compile.add_argument(
        "--sta-max-paths", type=int, default=None,
        help="optional per-group path cap for non-physical diagnostics; default analyzes all endpoints",
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
        "--transport-cost-db",
        type=Path,
        help=(
            "bind a calibrated open transport resource model to Phase 6 "
            "and seal its per-FPGA prediction"
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
            TDM_EXACT_DEPENDENCY_PROVIDER,
        ),
        help=(
            f"Phase 5 dependency scheduler (default: {TDM_EXACT_DEPENDENCY_PROVIDER})"
        ),
    )
    multi_fpga_compile.add_argument("--simulation-frames", type=int, default=16)
    multi_fpga_compile.add_argument("--equivalence-cycles", type=int, default=1)
    multi_fpga_compile.add_argument(
        "--equivalence-seed", type=int, default=20260727
    )
    multi_fpga_compile.add_argument(
        "--phase6-provider",
        choices=("chimew", "baseline"),
        default="baseline",
        help=(
            "Phase 6 algorithm; baseline is the stable default; explicit "
            "Chimew consumes indexed Xilinx placement and fixed LVDS wiring "
            "from the selected RapidWright or Vivado backend"
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
        choices=("rapidwright", "vivado"),
        default="rapidwright",
        help="physical/timing provider (default: rapidwright)",
    )
    multi_fpga_compile.add_argument("--physical-architecture", type=Path)
    multi_fpga_compile.add_argument("--physical-openparf-install", type=Path)
    multi_fpga_compile.add_argument("--physical-openparf-python", type=Path)
    multi_fpga_compile.add_argument("--physical-seed", type=int, default=1)
    multi_fpga_compile.add_argument("--physical-vivado")
    multi_fpga_compile.add_argument("--physical-vivado-dut-frequency-mhz", type=float,
        help="fastest DUT root implementation target; scale all root periods together")

    multi_fpga_compile.add_argument(
        "--physical-vivado-place-directive", default="Default"
    )
    multi_fpga_compile.add_argument(
        "--physical-vivado-route-directive", default="Default"
    )
    multi_fpga_compile.add_argument("--physical-rapidwright-jar", type=Path)
    multi_fpga_compile.add_argument("--physical-rapidwright-java", type=Path)
    multi_fpga_compile.add_argument("--physical-rapidwright-classes", type=Path)
    multi_fpga_compile.add_argument(
        "--physical-rapidwright-java-source", type=Path
    )
    multi_fpga_compile.add_argument(
        "--physical-rapidwright-device-data", type=Path
    )
    multi_fpga_compile.add_argument(
        "--physical-rapidwright-timing-data", type=Path
    )
    multi_fpga_compile.add_argument("--physical-rapidwright-opensta")

    multi_fpga_compile.add_argument(
        "--physical-rapidwright-native-constraints", type=Path,
        help="source-sealed RapidWright native device constraints",
    )
    multi_fpga_compile.add_argument(
        "--physical-rapidwright-provider-manifest", type=Path,
        help="provider manifest sealing the native device constraints",
    )
    multi_fpga_compile.add_argument(
        "--physical-workers",
        type=int,
        default=1,
        help="run independent per-FPGA physical backends concurrently",
    )
    multi_fpga_compile.add_argument(
        "--global-sta-executable",
        help="override the default in-tree OpenSTA executable",
    )
    multi_fpga_compile.add_argument("--global-timing-engine", choices=("opensta",),
                                   default="opensta", help="global physical timing engine (default: opensta)")
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
        "--fpga", dest="fpga_ids", action="append",
        help=(
            "implement only this FPGA as an explicit physical worker; repeat "
            "for more FPGAs; emits partition records, not a whole-design summary"
        ),
    )
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
        "--backend", choices=("rapidwright", "vivado"), default="rapidwright"
    )
    multi_fpga_physical.add_argument("--architecture", type=Path)
    multi_fpga_physical.add_argument("--yosys")
    multi_fpga_physical.add_argument("--openparf-install", type=Path)
    multi_fpga_physical.add_argument("--openparf-python", type=Path)
    multi_fpga_physical.add_argument("--seed", type=int, default=1)
    multi_fpga_physical.add_argument("--vivado")
    multi_fpga_physical.add_argument("--vivado-dut-frequency-mhz", type=float,
        help="fastest DUT root implementation target; scale all root periods together")

    multi_fpga_physical.add_argument(
        "--vivado-place-directive", default="Default"
    )
    multi_fpga_physical.add_argument(
        "--vivado-route-directive", default="Default"
    )
    multi_fpga_physical.add_argument("--rapidwright-jar", type=Path)
    multi_fpga_physical.add_argument("--rapidwright-java", type=Path)
    multi_fpga_physical.add_argument("--rapidwright-classes", type=Path)
    multi_fpga_physical.add_argument("--rapidwright-java-source", type=Path)
    multi_fpga_physical.add_argument("--rapidwright-device-data", type=Path)
    multi_fpga_physical.add_argument("--rapidwright-timing-data", type=Path)
    multi_fpga_physical.add_argument("--rapidwright-opensta")

    multi_fpga_physical.add_argument(
        "--rapidwright-native-constraints", type=Path,
    )
    multi_fpga_physical.add_argument(
        "--rapidwright-provider-manifest", type=Path,
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
            "reuse only independently validated physical "
            "checkpoints before continuing OpenPARF and detailed routing"
        ),
    )
    physical_assembly = multi_fpga_subparsers.add_parser(
        "assemble-vivado-physical",
        help="validate and join completed Vivado worker batches in an active run",
    )
    for option in ("split", "platform", "schedule", "out"):
        physical_assembly.add_argument(f"--{option}", type=Path, required=True)
    physical_assembly.add_argument(
        "--batch", dest="batches", type=Path, action="append", required=True,
        help="physical-partition-batch.json from a completed worker; repeat for all FPGAs",
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
    arch_import_rapidwright = arch_subparsers.add_parser(
        "import-rapidwright-device",
        help="import a RapidWright DeviceResources using a pinned provider manifest",
    )
    arch_import_rapidwright.add_argument("input", type=Path)
    arch_import_rapidwright.add_argument(
        "--provider-manifest", type=Path, required=True
    )
    arch_import_rapidwright.add_argument(
        "--route-certificate", type=Path, required=True
    )
    arch_import_rapidwright.add_argument(
        "--output", "-o", type=Path, required=True
    )
    arch_import_rapidwright.add_argument(
        "--native",
        help="explicit comparison override; defaults to the in-tree build",
    )
    arch_import_rapidwright.add_argument("--log", type=Path)
    arch_build_xilinx_preplacement_timing = arch_subparsers.add_parser(
        "build-xilinx-preplacement-timing",
        help=(
            "extract a compact pre-placement TimingDB from pinned "
            "RapidWright UltraScale+ timing data"
        ),
    )
    arch_build_xilinx_preplacement_timing.add_argument(
        "--timing-data-dir", type=Path, required=True
    )
    arch_build_xilinx_preplacement_timing.add_argument(
        "--output", "-o", type=Path, required=True
    )
    arch_build_xilinx_preplacement_timing.add_argument(
        "--ff-control-timing", type=Path,
        help="device-check supplement for CE, synchronous reset and recovery checks",
    )
    arch_import_ff_control_timing = arch_subparsers.add_parser(
        "import-ff-control-timing", help="import complete Vivado FF control check envelopes"
    )
    arch_import_ff_control_timing.add_argument("path", type=Path)
    arch_import_ff_control_timing.add_argument("--output", "-o", type=Path, required=True)
    arch_validate_xilinx_preplacement_timing = arch_subparsers.add_parser(
        "validate-xilinx-preplacement-timing",
        help="validate a source-sealed RapidWright pre-placement TimingDB",
    )
    arch_validate_xilinx_preplacement_timing.add_argument(
        "path", type=Path
    )
    arch_validate_fpgaif = arch_subparsers.add_parser(
        "validate-fpga-interchange",
        help="independently validate FPGA Interchange ArchitectureDB metadata",
    )
    arch_validate_fpgaif.add_argument("path", type=Path)
    arch_validate_rapidwright_provider = arch_subparsers.add_parser(
        "validate-rapidwright-provider",
        help="validate an exact RapidWright release and device contract",
    )
    arch_validate_rapidwright_provider.add_argument("path", type=Path)
    arch_validate_rapidwright_device = arch_subparsers.add_parser(
        "validate-rapidwright-device",
        help="validate an imported device against its RapidWright provider contract",
    )
    arch_validate_rapidwright_device.add_argument(
        "--arch", type=Path, required=True
    )
    arch_validate_rapidwright_device.add_argument(
        "--provider-manifest", type=Path, required=True
    )
    arch_validate_rapidwright_device.add_argument(
        "--require-physical-regions", action="store_true"
    )
    arch_validate_xilinx_packing = arch_subparsers.add_parser(
        "validate-xilinx-packing",
        help="independently validate UltraScale+ site packing and cascade chains",
    )
    arch_validate_xilinx_packing.add_argument(
        "--mapped-json", type=Path, required=True
    )
    arch_validate_xilinx_packing.add_argument("--packed", type=Path, required=True)
    arch_validate_xilinx_packing.add_argument("--architecture", type=Path)
    arch_validate_xilinx_packing.add_argument("--top")
    arch_validate_xilinx_placement = arch_subparsers.add_parser(
        "validate-xilinx-placement",
        help="independently validate exact UltraScale+ site/BEL placement",
    )
    arch_validate_xilinx_placement.add_argument(
        "--packed", type=Path, required=True
    )
    arch_validate_xilinx_placement.add_argument(
        "--architecture", type=Path, required=True
    )
    arch_validate_xilinx_placement.add_argument(
        "--placement", type=Path, required=True
    )
    arch_validate_xilinx_placement.add_argument("--constraints", type=Path)
    arch_export_rwroute = arch_subparsers.add_parser(
        "export-xilinx-rwroute-input",
        help="export a sealed placed UltraScale+ netlist for RWRoute",
    )
    arch_export_rwroute.add_argument("--mapped-index", type=Path, required=True)
    arch_export_rwroute.add_argument("--physical-handoff", type=Path, required=True)
    arch_export_rwroute.add_argument("--output", "-o", type=Path, required=True)
    arch_run_rwroute = arch_subparsers.add_parser(
        "run-xilinx-rwroute",
        help="route a sealed placed UltraScale+ design with RapidWright RWRoute",
    )
    arch_run_rwroute.add_argument("--input", type=Path, required=True)
    arch_run_rwroute.add_argument("--output", "-o", type=Path, required=True)
    arch_run_rwroute.add_argument("--rapidwright-jar", type=Path, required=True)
    arch_run_rwroute.add_argument("--java", type=Path, required=True)
    arch_run_rwroute.add_argument("--classes", type=Path, required=True)
    arch_run_rwroute.add_argument("--java-source", type=Path, required=True)
    arch_run_rwroute.add_argument(
        "--device-data-root", type=Path, required=True,
        help="external pinned RapidWright root containing data/parts.db",
    )
    arch_run_rwroute.add_argument(
        "--timing-data-dir", type=Path, required=True,
        help=(
            "directory containing the two pinned RapidWright UltraScale+ "
            "timing-data files"
        ),
    )
    arch_run_rwroute.add_argument("--log", type=Path)
    arch_validate_rwroute = arch_subparsers.add_parser(
        "validate-xilinx-route",
        help="independently validate a sealed RWRoute route certificate",
    )
    arch_validate_rwroute.add_argument("--route", type=Path, required=True)
    arch_validate_rwroute.add_argument("--mapped-json", type=Path, required=True)
    arch_validate_rwroute.add_argument("--packed", type=Path, required=True)
    arch_validate_rwroute.add_argument("--placement", type=Path, required=True)
    arch_build_xilinx_timing = arch_subparsers.add_parser(
        "build-xilinx-routed-timing",
        help="bind RapidWright site-pin delays to mapped logical endpoints",
    )
    arch_build_xilinx_timing.add_argument("--mapped-json", type=Path, required=True)
    arch_build_xilinx_timing.add_argument("--packed", type=Path, required=True)
    arch_build_xilinx_timing.add_argument("--placement", type=Path, required=True)
    arch_build_xilinx_timing.add_argument("--route", type=Path, required=True)
    arch_build_xilinx_timing.add_argument("--output", "-o", type=Path, required=True)
    arch_validate_xilinx_timing = arch_subparsers.add_parser(
        "validate-xilinx-routed-timing",
        help="independently validate logical endpoint timing bindings",
    )
    for option in (arch_validate_xilinx_timing,):
        option.add_argument("--timing", type=Path, required=True)
        option.add_argument("--mapped-json", type=Path, required=True)
        option.add_argument("--packed", type=Path, required=True)
        option.add_argument("--placement", type=Path, required=True)
        option.add_argument("--route", type=Path, required=True)
    arch_run_xilinx_opensta = arch_subparsers.add_parser(
        "run-xilinx-opensta",
        help="run OpenSTA over RapidWright-routed logical endpoints",
    )
    arch_run_xilinx_opensta.add_argument("--mapped-json", type=Path, required=True)
    arch_run_xilinx_opensta.add_argument("--routed-timing", type=Path, required=True)
    arch_run_xilinx_opensta.add_argument("--output", "-o", type=Path, required=True)
    arch_run_xilinx_opensta.add_argument("--summary", type=Path, required=True)
    arch_run_xilinx_opensta.add_argument(
        "--clock-period", action="append", default=[], required=True,
        metavar="CLOCK=PERIOD_NS",
    )
    arch_run_xilinx_opensta.add_argument("--opensta")
    arch_run_xilinx_opensta.add_argument("--max-paths", type=int, default=None)
    arch_run_xilinx_opensta.add_argument("--log", type=Path)
    arch_validate_xilinx_opensta = arch_subparsers.add_parser(
        "validate-xilinx-opensta",
        help="independently recompute RapidWright/OpenSTA WNS and TNS",
    )
    arch_validate_xilinx_opensta.add_argument("--summary", type=Path, required=True)
    arch_validate_xilinx_opensta.add_argument("--output", type=Path, required=True)
    arch_validate_xilinx_opensta.add_argument("--mapped-json", type=Path)
    arch_validate_xilinx_opensta.add_argument("--routed-timing", type=Path)
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
    arch_rebind_regions = arch_subparsers.add_parser(
        "rebind-physical-regions",
        help="rebind a sidecar across physically identical ArchitectureDBs",
    )
    arch_rebind_regions.add_argument(
        "--old-arch", type=Path, required=True
    )
    arch_rebind_regions.add_argument(
        "--new-arch", type=Path, required=True
    )
    arch_rebind_regions.add_argument("--sidecar", type=Path, required=True)
    arch_rebind_regions.add_argument(
        "--output", "-o", type=Path, required=True
    )
    arch_validate_regions = arch_subparsers.add_parser(
        "validate-physical-regions",
        help="validate merged SLR, clock-region, and I/O-bank metadata",
    )
    arch_validate_regions.add_argument("path", type=Path)

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
            "run multi-FPGA partitioning with the default register-boundary "
            "PATRON policy or an explicit generalized Static Exact comparison"
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
        default=DEFAULT_PARTITION_CUT_MODE,
        help=(
            "Phase 3 cut legality; generalized Static Exact v2 is the default"
        ),
    )
    phase3.add_argument(
        "--max-cross-fpga-dependency-depth",
        type=int,
        default=STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH,
        help=(
            "optional Static Exact policy guard; zero (the default) leaves "
            "depth to schedule feasibility and final timing"
        ),
    )
    phase3.add_argument(
        "--static-exact-candidate-policy",
        choices=(STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,),
        default=STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    )
    phase3.add_argument("--min-used-fpgas", type=int)
    phase3.add_argument(
        "--balance-tolerance", type=float,
        help=("Relative tolerance for configured balance_dimensions (default: "
              "cells, LUT, and FF); omitted dimensions impose only device "
              "capacity limits"),
    )
    phase3.add_argument(
        "--provider",
        choices=(
            "tritonpart",
            "patron",
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
        help="optional emuflow.partition-net-weights/v2 indexed manifest",
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
        action=_BooleanOptionalAction, default=True,
        help=(
            "minimally move the smallest legal atomic clusters when the "
            "provider leaves required partitions empty"
        ),
    )
    phase3.add_argument(
        "--hop-refiner",
        help=(
            "explicit comparison override; defaults to the in-tree C++ "
            "topology-constrained FM refiner"
        ),
    )
    phase3.add_argument(
        "--timing-database",
        type=Path,
        help="complete TimingPathDB required by --provider patron",
    )
    phase3.add_argument("--patron-refiner")
    phase3.add_argument("--patron-max-moves", type=int)
    phase3.add_argument(
        "--patron-algorithm-version",
        type=int,
        choices=(14,),
        default=DEFAULT_PATRON_ALGORITHM_VERSION,
    )
    phase3.add_argument("--patron-initial-assignment", type=Path)
    phase3.add_argument("--patron-initial-clusters", type=Path)

    sta_parser = subparsers.add_parser(
        "sta", help="STA path extraction artifact operations"
    )
    sta_subparsers = sta_parser.add_subparsers(
        dest="sta_command", required=True
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
            "provider-matched Architecture TimingDB; generates a "
            "design-specialized pre-placement OpenSTA model"
        ),
    )
    sta_opensta.add_argument(
        "--opensta",
        "--openroad",
        dest="opensta",
        help="explicit comparison override; defaults to the in-tree build",
    )
    sta_opensta.add_argument("--max-paths", type=int, default=None)
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
        "--provider",
        choices=[
            GLOBAL_CANDIDATE_PROVIDER,
        ],
        default=None,
        help=(
            f"system routing provider (default: {GLOBAL_CANDIDATE_PROVIDER})"
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
            "explicit emuflow-native executable; defaults to the selected native installation"
        ),
    )
    phase4.add_argument(
        "--retain-candidate-pool",
        action="store_true",
        help=(
            "write the complete diagnostic candidate-tree pool; the default "
            "production path validates candidates online"
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
    schedule_validate.add_argument("--assignment", type=Path)

    phase5 = subparsers.add_parser(
        "phase5", help="schedule routed bit-hops into TDM lanes and slots"
    )
    phase5.add_argument("--routes", type=Path, required=True)
    phase5.add_argument("--assignment", type=Path)
    phase5.add_argument("--platform", type=Path, required=True)
    phase5.add_argument("--out", type=Path, required=True)
    phase5.add_argument("--simulation-frames", type=int, default=16)
    phase5.add_argument(
        "--provider",
        choices=(
            TDM_EXACT_DEPENDENCY_PROVIDER,
        ),
        default=None,
        help=(
            f"Phase 5 dependency scheduler (default: {TDM_EXACT_DEPENDENCY_PROVIDER})"
        ),
    )


    pin_plan_parser = subparsers.add_parser(
        "pin-plan",
        help="Chimew pin planning and independent physical contract checks",
    )
    pin_plan_subparsers = pin_plan_parser.add_subparsers(
        dest="pin_plan_command", required=True
    )
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
    phase6.add_argument("--transport-cost-db", type=Path)
    phase6.add_argument("--equivalence-cycles", type=int, default=1)
    phase6.add_argument("--equivalence-seed", type=int, default=20260727)
    phase6.add_argument(
        "--clock-period",
        action="append",
        default=[],
        metavar="CLOCK=PERIOD_NS",
        help="required for multi-clock Phase 6; repeat once per DUT clock",
    )

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
    phase7c.add_argument(
        "--assignment",
        type=Path,
        help="Phase 3 assignment required for sampled virtual-wire timing",
    )
    phase7c.add_argument("--physical-summary", type=Path)
    phase7c.add_argument("--original-ir", type=Path, help="original indexed DUT IR for native physical timing member records")
    phase7c.add_argument("--original-path-database", type=Path, help="complete original timing selector for native physical timing")
    phase7c.add_argument("--original-endpoint-index", type=Path, help="reuse the existing complete original endpoint index")
    phase7c.add_argument(
        "--routes",
        type=Path,
        help="Phase 4 routes.json required for unified physical timing",
    )
    phase7c.add_argument("--simulation-frames", type=int, default=12)
    phase7c.add_argument("--global-sta-executable", help="override the default in-tree OpenSTA executable")
    phase7c.add_argument("--global-timing-engine", choices=("opensta",),
                        default="opensta", help="global physical timing engine (default: opensta)")
    phase7c.add_argument("--out", type=Path, required=True)

    return parser

def _dispatch(args: argparse.Namespace) -> int:




    if args.command == "platform":
        if args.platform_command == "derive-utilization-profile":
            source = Platform.load(args.input)
            derived = source.with_utilization_limit(
                args.utilization_limit, name=args.name
            )
            write_json(args.output, derived.to_dict())
            report = derived.summary()
            report.update(
                {
                    "status": "pass",
                    "output": str(args.output),
                    "source_platform": source.name,
                    "utilization_limit": args.utilization_limit,
                }
            )
            _print_json(report)
            return 0
        if args.platform_command == "research-model":
            _print_json(materialize_research_platform(
                args.output, fpga_count=args.fpga_count,
                wires_per_bank=args.wires_per_bank,
                transport_format=args.transport_format,
            ))
            return 0
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
        elif args.contest_command == "eda2023-evaluate":
            report = evaluate_eda2023_solution(
                instance_path=args.instance,
                routes_path=args.routes,
                tdm_plan_path=args.tdm_plan,
            )
            if args.output is not None:
                write_json(args.output, report)
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
        if args.mapping_profile == XILINX_ULTRASCALEPLUS_OPEN_PROFILE:
            if args.verilog_output is not None:
                raise EmuFlowError(
                    "Route A normalized synthesis does not yet emit mapped Verilog"
                )
            report = run_xilinx_ultrascaleplus_yosys(
                sources=args.sources,
                top=args.top,
                output=args.output,
                executable=args.yosys,
                log_path=args.log,
                include_dirs=args.include_dir,
                defines=args.define,
            )
        else:
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
            report = {"status": "pass"}
        _print_json(
            {
                **report,
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
                "top": args.top,
            }
        )
        return 0

    if args.command == "benchmark":
        report = run_benchmark(
            spec_path=args.spec,
            source_root=args.source_root,
            output_dir=args.out,
            yosys=args.yosys,
        )
        _print_json(report)
        return 0 if report["status"] == "pass" else 2

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
        elif args.arch_command == "import-rapidwright-device":
            report = run_rapidwright_device_import(
                input_path=args.input,
                provider_manifest_path=args.provider_manifest,
                route_certificate_path=args.route_certificate,
                output_path=args.output,
                executable=args.native,
                log_path=args.log,
            )
        elif args.arch_command == "validate-fpga-interchange":
            architecture = ArchitectureDB.load(args.path)
            report = validate_fpga_interchange_architecture(architecture)
        elif args.arch_command == "validate-rapidwright-provider":
            manifest = load_rapidwright_provider_manifest(args.path)
            report = validate_rapidwright_provider_manifest(manifest)
        elif args.arch_command == "validate-rapidwright-device":
            manifest = load_rapidwright_provider_manifest(
                args.provider_manifest
            )
            report = validate_rapidwright_architecture(
                ArchitectureDB.load(args.arch),
                manifest,
                manifest_path=args.provider_manifest,
                require_physical_regions=args.require_physical_regions,
            )
        elif args.arch_command == "validate-xilinx-packing":
            report = validate_xilinx_packing(
                args.mapped_json,
                args.packed,
                top=args.top,
                architecture_path=args.architecture,
            )
        elif args.arch_command == "validate-xilinx-placement":
            report = validate_xilinx_placement(
                args.packed,
                args.architecture,
                args.placement,
                constraints_path=args.constraints,
            )
        elif args.arch_command == "export-xilinx-rwroute-input":
            report = export_rwroute_input(
                args.mapped_index, args.physical_handoff, args.output
            )
        elif args.arch_command == "run-xilinx-rwroute":
            report = run_rwroute(
                args.input, args.output,
                rapidwright_jar=args.rapidwright_jar,
                java=args.java,
                classes_dir=args.classes,
                java_source=args.java_source,
                device_data_root=args.device_data_root,
                timing_data_dir=args.timing_data_dir,
                log_path=args.log,
            )
        elif args.arch_command == "validate-xilinx-route":
            report = validate_xilinx_route_db(
                args.route,
                mapped_path=args.mapped_json,
                packed_path=args.packed,
                placement_path=args.placement,
            )
        elif args.arch_command == "build-xilinx-preplacement-timing":
            report = build_xilinx_preplacement_timing_db(
                args.timing_data_dir, args.output, ff_control_timing_path=args.ff_control_timing
            )
        elif args.arch_command == "import-ff-control-timing":
            report = import_vivado_ff_control_timing(args.path, args.output)
        elif args.arch_command == "validate-xilinx-preplacement-timing":
            report = validate_xilinx_preplacement_timing_db(args.path)
        elif args.arch_command == "build-xilinx-routed-timing":
            report = build_xilinx_routed_timing(
                args.mapped_json, args.packed, args.placement,
                args.route, args.output,
            )
        elif args.arch_command == "validate-xilinx-routed-timing":
            report = validate_xilinx_routed_timing(
                args.timing,
                mapped_path=args.mapped_json,
                packed_path=args.packed,
                placement_path=args.placement,
                route_path=args.route,
            )
        elif args.arch_command == "run-xilinx-opensta":
            report = run_xilinx_routed_opensta(
                args.mapped_json,
                args.routed_timing,
                args.output,
                args.summary,
                clocks=parse_clock_definitions(args.clock_period),
                executable=args.opensta,
                max_paths=args.max_paths,
                log_path=args.log,
            )
        elif args.arch_command == "validate-xilinx-opensta":
            report = validate_xilinx_routed_opensta_summary(
                args.summary,
                output_path=args.output,
                mapped_path=args.mapped_json,
                timing_path=args.routed_timing,
            )
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
        elif args.arch_command == "rebind-physical-regions":
            report = run_physical_region_rebind(
                old_architecture_path=args.old_arch,
                new_architecture_path=args.new_arch,
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

        if args.multi_fpga_command == "finalize-physical":
            report = finalize_multi_fpga_physical_checkpoint(
                args.flow,
                args.physical,
                runtime_directory=args.runtime_directory,
                global_sta_executable=args.global_sta_executable,
                global_sta_results_dir=args.global_sta_results_dir,
                native_endpoint_vivado=args.native_endpoint_vivado,
                native_endpoint_results_dir=args.native_endpoint_results_dir,
            )
            _print_json(report["summary"])
            return 0 if report["status"] == "pass" else 2

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
        if args.multi_fpga_command == "assemble-vivado-physical":
            report = assemble_vivado_physical_batches(
                args.split, args.platform, args.schedule, args.batches, args.out,
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
                yosys=args.yosys,
                openparf_install=args.openparf_install,
                openparf_python=args.openparf_python,
                seed=args.seed,
                vivado=args.vivado,
                vivado_dut_frequency_mhz=args.vivado_dut_frequency_mhz,
                vivado_place_directive=args.vivado_place_directive,
                vivado_route_directive=args.vivado_route_directive,
                rapidwright_jar=args.rapidwright_jar,
                rapidwright_java=args.rapidwright_java,
                rapidwright_classes=args.rapidwright_classes,
                rapidwright_java_source=args.rapidwright_java_source,
                rapidwright_device_data=args.rapidwright_device_data,
                rapidwright_timing_data=args.rapidwright_timing_data,
                rapidwright_opensta=args.rapidwright_opensta,
                rapidwright_native_constraints=(
                    args.rapidwright_native_constraints
                ),
                rapidwright_provider_manifest=(
                    args.rapidwright_provider_manifest
                ),
                original_ir_path=args.original_ir,
                assignment_path=args.assignment,
                routes_path=args.routes,
                path_database_path=args.path_database,
                logic_path_database_path=args.logic_path_database,
                workers=args.workers,
                resume=args.resume,
                fpga_ids=args.fpga_ids,
            )
            _print_json(report["summary"])
            return 0
        if args.benchmark_run is not None:
            if args.source_root is None:
                raise EmuFlowError("--benchmark-run requires --source-root")
            manual_context = {
                "positional sources": args.sources,
                "--top": args.top,
                "--clock": args.clock,
                "--clock-period": args.clock_period,
                "--include-dir": args.include_dir,
                "--define": args.define,
                "--mapping-profile": (None if args.mapping_profile == XILINX_ULTRASCALEPLUS_OPEN_PROFILE else args.mapping_profile),
                "--yosys-json": args.yosys_json,
            }
            conflicts = [
                name
                for name, value in manual_context.items()
                if value not in (None, [], ())
            ]
            if conflicts:
                raise EmuFlowError(
                    "--benchmark-run owns its frontend contract and cannot be "
                    "combined with " + ", ".join(conflicts)
                )
            benchmark = BenchmarkRun.load(args.benchmark_run)
            mapping_profile = benchmark.value.get("physical_mapping_profile")
            synthesis = benchmark.value["synthesis"]
            periods = benchmark.value.get("clock_periods_ns")
            timing_io = benchmark.value.get("timing_io")
            if (
                benchmark.value.get("calibration_holdout_class") is None
                or mapping_profile != XILINX_ULTRASCALEPLUS_OPEN_PROFILE
                or synthesis.get("family") != "xcup"
                or synthesis.get("policy") != "native"
                or not isinstance(periods, dict)
                or timing_io is None
            ):
                raise EmuFlowError(
                    "--benchmark-run requires a timed calibration holdout with "
                    "an explicit native Xilinx physical_mapping_profile"
                )
            sources = benchmark.resolve_sources(args.source_root)
            include_dirs = benchmark.resolve_include_dirs(args.source_root)
            defines = list(synthesis.get("defines", []))
            top = benchmark.value["top"]
            clocks = list(benchmark.value["clocks"])
            clock_periods = dict(periods)
        else:
            if args.source_root is not None:
                raise EmuFlowError("--source-root requires --benchmark-run")
            sources = args.sources
            include_dirs = args.include_dir
            defines = args.define
            top = args.top
            clocks = args.clock
            clock_periods = (
                parse_clock_definitions(args.clock_period)
                if args.clock_period
                else None
            )
            mapping_profile = args.mapping_profile or XILINX_ULTRASCALEPLUS_OPEN_PROFILE
            timing_io = None
        compiler = run_multi_fpga_flow
        if args.compile_implementation == "native-control-v1":
            from .native_compile import run_native_multi_fpga_flow
            compiler = run_native_multi_fpga_flow
        report = compiler(
            platform_path=args.platform,
            output_dir=args.out,
            prepared_frontend=args.prepared_frontend,
            prepared_timing=args.prepared_timing,
            sources=sources,
            include_dirs=include_dirs,
            defines=defines,
            top=top,
            clocks=clocks,
            yosys_json=args.yosys_json,
            yosys=args.yosys,
            mapping_profile=mapping_profile,
            xilinx_mapping_strategy=args.xilinx_mapping_strategy,
            partition_constraints=args.partition_constraints,
            partition_provider=args.partition_provider,
            seed=args.seed,
            min_used_fpgas=args.min_used_fpgas,
            balance_tolerance=args.balance_tolerance,
            openroad=args.openroad,
            tritonpart_solution=args.tritonpart_solution,
            patron_refiner=args.patron_refiner,
            patron_max_moves=args.patron_max_moves,
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
            cut_mode=args.cut_mode,
            max_cross_fpga_dependency_depth=(
                args.max_cross_fpga_dependency_depth
            ),
            static_exact_candidate_policy=(
                args.static_exact_candidate_policy
            ),
            timing_driven=args.timing_driven,
            clock_periods=clock_periods,
            timing_io=timing_io,
            timing_model=args.timing_model,
            architecture_timing_db=args.architecture_timing_db,
            opensta=args.opensta,
            sta_max_paths=args.sta_max_paths,
            timing_criticality_scale=args.timing_criticality_scale,
            timing_criticality_exponent=(
                args.timing_criticality_exponent
            ),
            route_constraints=args.route_constraints,
            board_link_timing_db=args.board_link_timing_db,
            transport_cost_db=args.transport_cost_db,
            timing_paths=args.timing_paths,
            router=args.router,
            route_provider=args.route_provider,
            route_candidate_workers=args.route_candidate_workers,
            frame_slots=args.frame_slots,
            optimize_frame_slots=args.optimize_frame_slots,
            route_max_iterations=args.route_max_iterations,
            tdm_provider=args.tdm_provider,


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
            physical_openparf_install=args.physical_openparf_install,
            physical_openparf_python=args.physical_openparf_python,
            physical_seed=args.physical_seed,
            physical_vivado=args.physical_vivado,
            physical_vivado_dut_frequency_mhz=args.physical_vivado_dut_frequency_mhz,
            physical_vivado_place_directive=(
                args.physical_vivado_place_directive
            ),
            physical_vivado_route_directive=(
                args.physical_vivado_route_directive
            ),
            physical_rapidwright_jar=args.physical_rapidwright_jar,
            physical_rapidwright_java=args.physical_rapidwright_java,
            physical_rapidwright_classes=args.physical_rapidwright_classes,
            physical_rapidwright_java_source=(
                args.physical_rapidwright_java_source
            ),
            physical_rapidwright_device_data=(
                args.physical_rapidwright_device_data
            ),
            physical_rapidwright_timing_data=(
                args.physical_rapidwright_timing_data
            ),
            physical_rapidwright_opensta=args.physical_rapidwright_opensta,
            physical_rapidwright_native_constraints=(
                args.physical_rapidwright_native_constraints
            ),
            physical_rapidwright_provider_manifest=(
                args.physical_rapidwright_provider_manifest
            ),
            physical_workers=args.physical_workers,
            global_sta_executable=args.global_sta_executable,
            global_timing_engine=args.global_timing_engine,
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
        _print_json(report.get("summary", report))
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
            route_constraints_path=args.route_constraints,
            hop_refiner=args.hop_refiner,
            cut_mode=args.cut_mode,
            max_cross_fpga_dependency_depth=(
                args.max_cross_fpga_dependency_depth
            ),
            timing_database_path=args.timing_database,
            patron_refiner=args.patron_refiner,
            patron_max_moves=args.patron_max_moves,
            patron_algorithm_version=args.patron_algorithm_version,
            patron_initial_assignment_path=(
                args.patron_initial_assignment
            ),
            patron_initial_clusters_path=args.patron_initial_clusters,
            static_exact_candidate_policy=(
                args.static_exact_candidate_policy
            ),
        )
        _print_json(report)
        return 0 if report["status"] == "pass" else 2

    if args.command == "sta":
        if args.sta_command == "project-path-database":
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
            candidate_workers=args.candidate_workers,
            retain_candidate_pool=args.retain_candidate_pool,
        )
        _print_json(report)
        return 0 if report["status"] == "pass" else 2

    if args.command == "schedule":
        report = validate_phase5(
            routes_path=args.routes,
            platform_path=args.platform,
            schedule_path=args.schedule,

            assignment_path=args.assignment,
        )
        _print_json(report)
        return 0

    if args.command == "phase5":
        report = run_phase5(
            routes_path=args.routes,
            platform_path=args.platform,
            output_dir=args.out,
            assignment_path=args.assignment,

            provider=args.provider,

        )
        _print_json(report)
        return 0 if report["status"] == "pass" else 2


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
            assignment_input = read_chimew_bank_channel_input(
                args.assignment_input
            )
            assignment_report = read_chimew_bank_channel_report(
                args.assignment_report, allow_legacy=True
            )
            try:
                with assignment_input:
                    report = build_chimew_phase6_qualification(
                        read_json(args.schedule),
                        read_json(args.crossings),
                        read_json(args.initial_grouping),
                        read_position_hints(args.positions),
                        read_json(args.refined_grouping),
                        read_json(args.rudy_input),
                        read_json(args.rudy_report),
                        assignment_input,
                        assignment_report,
                    )
            finally:
                if isinstance(assignment_report, BankChannelReportView):
                    assignment_report.close()
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
        schedule = read_tdm_schedule(args.schedule)
        platform = Platform.load(args.platform)
        report = validate_pin_plan(
            schedule,
            platform,
            read_position_hints(args.positions),
            read_pin_plan(args.plan),
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
            transport_cost_path=args.transport_cost_db,
            equivalence_cycles=args.equivalence_cycles,
            equivalence_seed=args.equivalence_seed,
            clock_periods=(
                parse_clock_definitions(args.clock_period)
                if args.clock_period
                else None
            ),
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
        schedule = read_tdm_schedule(args.schedule)
        positions = (
            read_position_hints(args.position_hints)
            if args.position_hints is not None
            else None
        )
        plan = read_pin_plan(args.pin_plan) if args.pin_plan is not None else None
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
            assignment_path=args.assignment,
            physical_summary_path=args.physical_summary,
            original_ir_path=args.original_ir,
            original_path_database_path=args.original_path_database,
            original_endpoint_index_path=args.original_endpoint_index,
            routes_path=args.routes,
            simulation_frames=args.simulation_frames,
            global_sta_executable=args.global_sta_executable,
            global_timing_engine=args.global_timing_engine,
            output_dir=args.out,
        )
        _print_json(report)
        return 0 if report["status"] in {"generated", "pass"} else 2

    raise AssertionError(f"unhandled command {args.command!r}")

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except (EmuFlowError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f"emuflow: error: {error}", file=sys.stderr)
        return 1
