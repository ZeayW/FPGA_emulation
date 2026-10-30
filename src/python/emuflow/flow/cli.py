from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence
from emuflow.common.errors import EmuFlowError
from emuflow.frontend.clocks import parse_clock_definitions
from emuflow.frontend.policy import VALID_XILINX_MAPPING_STRATEGIES
from emuflow.flow.benchmark_inputs import BenchmarkRun
from emuflow.flow.compile_options import COMPILE_DEFAULTS
from emuflow.flow import native_compile
from emuflow.partition.combinational_cut import STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2, STATIC_EXACT_DEFAULT_CANDIDATE_POLICY, STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH

DEFAULT_PLATFORM_PATH = COMPILE_DEFAULTS['platform_path']
DEFAULT_TIMING_MODEL = COMPILE_DEFAULTS['timing_model']
DEFAULT_PARTITION_CUT_MODE = COMPILE_DEFAULTS['cut_mode']
DEFAULT_PATRON_ALGORITHM_VERSION = COMPILE_DEFAULTS['patron_algorithm_version']
XILINX_ULTRASCALEPLUS_OPEN_PROFILE = COMPILE_DEFAULTS['mapping_profile']
GLOBAL_CANDIDATE_PROVIDER = 'timing-aware-global-candidate-v1'
TDM_EXACT_DEPENDENCY_PROVIDER = 'deterministic-dependency-earliest-slot-v1'


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

_BooleanOptionalAction = getattr(argparse, "BooleanOptionalAction", _Python38BooleanOptionalAction)


def _print_json(value: Dict[str, Any]) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="emuflow",
        description="Open multi-FPGA emulation flow frontend",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser(
        "run", help="run RTL through the complete Phase 1–7/7C flow"
    )

    run.add_argument(
        "inputs", nargs="+", type=Path, help="RTL sources, or one prepared .run.json"
    )

    run.add_argument("--top", help="RTL top module")

    run.add_argument(
        "--clock", action="append", default=[], metavar="NAME=NS",
        help="clock port and period in ns; repeat for multiple clocks",
    )

    run.add_argument("--include-dir", action="append", default=[], type=Path)

    run.add_argument("--define", action="append", default=[])

    run.add_argument("--platform", type=Path, default=DEFAULT_PLATFORM_PATH)

    run.add_argument(
        "--phase6-provider", choices=("baseline", "chimew"), default="baseline"
    )

    run.add_argument(
        "--backend",
        choices=("rapidwright", "vivado"),
        default="rapidwright",
        help="physical implementation route (default: rapidwright)",
    )

    run.add_argument("--out", type=Path, help="default: runs/TOP")

    run.add_argument("--check", action="store_true", help="check input/configuration inventory without executing the design")

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
        choices=(14,),
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

    multi_fpga_compile.add_argument(
        "--physical-vivado-place-directive", default="Default"
    )

    multi_fpga_compile.add_argument(
        "--physical-vivado-route-directive", default="Default"
    )

    multi_fpga_compile.add_argument("--physical-rapidwright-jar", type=Path)

    multi_fpga_compile.add_argument("--physical-rapidwright-java", type=Path)

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

    return parser


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "run":
        from emuflow.flow.run_config import execute_run, execute_sources

        prepared = (
            len(args.inputs) == 1
            and args.inputs[0].suffix == ".json"
            and not args.top
            and not args.clock
        )
        if prepared:
            name = args.inputs[0].stem.removesuffix(".run")
            output = args.out or Path("runs") / name
            report = execute_run(
                args.inputs[0], output, backend=args.backend, check_only=args.check
            )
        else:
            if not args.top or not args.clock:
                raise EmuFlowError(
                    "RTL runs require --top and at least one --clock NAME=NS"
                )
            output = args.out or Path("runs") / args.top
            report = execute_sources(
                args.inputs,
                top=args.top,
                clock_periods=parse_clock_definitions(args.clock),
                output_dir=output,
                backend=args.backend,
                include_dirs=args.include_dir,
                defines=args.define,
                platform_path=args.platform,
                phase6_provider=args.phase6_provider,
                check_only=args.check,
            )
        _print_json(report.get("summary", report))
        return 0 if report["status"] == "pass" else 2

    if args.command == "multi-fpga":
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

        report = native_compile.run_native_multi_fpga_flow(
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
            physical_vivado_place_directive=(
                args.physical_vivado_place_directive
            ),
            physical_vivado_route_directive=(
                args.physical_vivado_route_directive
            ),
            physical_rapidwright_jar=args.physical_rapidwright_jar,
            physical_rapidwright_java=args.physical_rapidwright_java,
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
        )

        _print_json(report.get("summary", report))

        return 0 if report["status"] == "pass" else 2

    raise AssertionError(f"unhandled command {args.command!r}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except (EmuFlowError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f"emuflow: error: {error}", file=sys.stderr)
        return 1
