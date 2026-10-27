from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence
from emuflow.common.errors import EmuFlowError
from emuflow.common.io import write_json
from .benchmark import run_benchmark
from .board_arm_mps4 import materialize_arm_mps4_boarddb
from .contest_eda2023 import evaluate_eda2023_solution, import_eda2023_case, materialize_eda2023_rtl_boarddb
from .contest_eda2024 import evaluate_eda2024_solution, materialize_eda2024_rtl_boarddb
from .contest_iccad2019 import evaluate_iccad2019_solution, import_iccad2019_instance, materialize_iccad2019_rtl_boarddb
from .contest_public import evaluate_public_contest_case, fetch_public_contest_case, import_public_contest_case, materialize_public_contest_boarddb, validate_public_contest_evaluation
from .contest_validation_matrix import load_contest_validation_matrix

def _print_json(value):
    print(json.dumps(value, indent=2, sort_keys=True))

def _build_parser():
    parser = argparse.ArgumentParser(prog="emuflow-tools")
    subparsers = parser.add_subparsers(dest="command", required=True)
    platform_parser = subparsers.add_parser("platform", help="Independent BoardDB materializers")
    platform_subparsers = platform_parser.add_subparsers(dest="platform_command", required=True)
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
    contest_fetch.add_argument(
        "--fetcher-root", type=Path,
        help="checkout root containing the matrix-selected source fetcher scripts",
    )
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
    return parser

def _dispatch(args):
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
        raise AssertionError("unsupported platform tool")
    if args.command == "contest":
        if args.contest_command == "matrix-validate":
            _, report = load_contest_validation_matrix(args.matrix)
        elif args.contest_command == "fetch-public":
            report = fetch_public_contest_case(
                args.matrix, args.case_id, args.out,
                fetcher_root=args.fetcher_root,
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
                runtime_seconds=args.runtime_seconds,
                expected_routes_sha256=args.expected_routes_sha256,
                expected_tdm_plan_sha256=args.expected_tdm_plan_sha256,
                expected_solution_sha256=args.expected_solution_sha256,
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
        else:
            raise AssertionError(f"unhandled contest command {args.contest_command!r}")
        _print_json(report)
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
    raise AssertionError(f"unhandled tools command {args.command!r}")

def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except (EmuFlowError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f"emuflow-tools: error: {error}", file=sys.stderr)
        return 1
