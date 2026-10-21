#!/usr/bin/env python3
"""Produce a sealed full-NV_nvdla Phase3 prefix for paired Phase4/5 research."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from phase4_nvdla_compare import (
    CLOCKS, capture_provenance, check_platform, digest, now, process_rss,
    read, require, seal_contract, verify_provenance, write,
)
from emuflow.benchmark import BenchmarkRun


def flow_options(args, benchmark, constraints):
    """Keep scientific options explicit and independent of CLI defaults."""
    return dict(
        platform_path=args.platform, output_dir=args.output / "common",
        sources=benchmark.resolve_sources(args.source_root),
        include_dirs=benchmark.resolve_include_dirs(args.source_root),
        defines=benchmark.value["synthesis"].get("defines", []),
        top="NV_nvdla", clocks=list(CLOCKS), clock_periods=dict(CLOCKS),
        timing_io=benchmark.value["timing_io"],
        mapping_profile="xilinx-ultrascaleplus-open-v1",
        yosys=str(args.yosys), opensta=str(args.sta),
        architecture_timing_db=args.architecture_timing_db,
        partition_constraints=constraints, partition_provider="patron",
        patron_algorithm_version=14, patron_flow_refinement=True,
        seed=1, min_used_fpgas=4, partition_repair_min_used_fpgas=True,
        partition_repair_balance=True,
        partition_timeout_seconds=args.partition_timeout_seconds,
        cut_mode="static-exact-combinational", frame_slots=1024,
        optimize_frame_slots=False, timing_driven=True, timing_backend="opensta",
        sta_max_paths=args.sta_max_paths, sta_require_complete=True,
        stop_after="phase3", physical=False,
    )


def worker(args):
    from emuflow.multi_fpga_flow import run_multi_fpga_flow
    os.environ["EMUFLOW_NATIVE_ROOT"] = str(args.native_root)
    provenance = args.output / "provenance.json"
    verify_provenance(read(provenance))
    benchmark = BenchmarkRun.load(args.benchmark)
    constraints = args.output / "partition-constraints.json"
    write(constraints, {"schema": "emuflow.partition-constraints/v1",
                        "min_used_fpgas": 4, "balance_dimensions": []})
    report = run_multi_fpga_flow(**flow_options(args, benchmark, constraints))
    require(report.get("completed_through") == "phase3", "wrong prefix boundary")
    contract = seal_contract(args.output / "common", args.platform, provenance)
    write(args.output / "contract.json", contract)
    write(args.output / "result.json", {
        "status": "pass", "completed_through": "phase3",
        "contract_sha256": digest(args.output / "contract.json"),
        "finished_at": now(),
    })


def supervise(args, argv):
    require(not args.output.exists(), "output must be a new directory")
    check_platform(read(args.platform))
    BenchmarkRun.load(args.benchmark)
    for path in (args.yosys, args.sta):
        require(path.is_file() and os.access(path, os.X_OK), f"not executable: {path}")
    bins = args.native_root / "bin"
    tools = [args.yosys, args.sta, args.architecture_timing_db]
    for name in ("openroad", "emuflow_patron_refiner", "emuflow_tritonpart_balance_legalizer"):
        path = bins / name
        require(path.is_file() and os.access(path, os.X_OK), f"missing native tool: {path}")
        tools.append(path)
    for extra in args.provenance_input:
        require(extra.exists(), f"missing provenance input: {extra}")
        tools.extend(sorted(p for p in extra.rglob("*") if p.is_file()) if extra.is_dir() else [extra])
    args.output.mkdir(parents=True)
    write(args.output / "invocation.json", {
        "command": [sys.executable, str(Path(__file__).resolve()), *argv],
        "started_at": now(), "sta_max_paths": args.sta_max_paths,
        "path_limit_policy": "reject reached limit before partitioning",
    })
    write(args.output / "provenance.json", capture_provenance(
        args.benchmark, args.source_root, prefix_tools=tools))
    command = [sys.executable, str(Path(__file__).resolve()), *argv, "--worker"]
    record = {"status": "running", "command": command, "started_at": now(),
              "peak_sampled_tree_rss_kib": None,
              "memory_measurement": "sampled process-tree RSS sum; shared pages may be counted twice; not a hard cap"}
    started = time.monotonic()
    child = None
    try:
        with (args.output / "worker.log").open("w") as log:
            child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
            record.update(pid=child.pid, pgid=child.pid)
            while child.poll() is None:
                rss = process_rss(child.pid)
                record["peak_sampled_tree_rss_kib"] = max(record["peak_sampled_tree_rss_kib"] or 0, rss)
                record["elapsed_seconds"] = time.monotonic() - started
                write(args.output / "process.json", record)
                require(rss <= args.memory_gib * 1024**2, "sampled memory guard exceeded")
                require(record["elapsed_seconds"] < args.timeout_seconds, "prefix deadline exceeded")
                time.sleep(args.sample_seconds)
            require(child.returncode == 0, "prefix worker failed; inspect worker.log")
            require(read(args.output / "result.json").get("status") == "pass", "prefix result failed")
            record["status"] = "pass"
    except BaseException as error:
        record.update(status="failed", error=str(error))
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        raise
    finally:
        record.update(exit_code=child.returncode if child is not None else None,
                      wall_seconds=time.monotonic() - started, finished_at=now())
        write(args.output / "process.json", record)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("benchmark", "source-root", "platform", "architecture-timing-db",
                 "output", "yosys", "sta", "native-root"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--sta-max-paths", type=int, default=10000000,
                   help="OpenSTA export bound; reaching it rejects the run before Phase3")
    p.add_argument("--partition-timeout-seconds", type=int, default=14400)
    p.add_argument("--timeout-seconds", type=int, default=86400)
    p.add_argument("--memory-gib", type=float, default=100)
    p.add_argument("--sample-seconds", type=float, default=2)
    p.add_argument("--provenance-input", type=Path, action="append", default=[],
                   help="additional consumed mapping file or tool-data directory to seal")
    p.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = parser().parse_args(argv)
    for key, value in vars(args).items():
        if isinstance(value, Path):
            setattr(args, key, value.resolve())
    args.provenance_input = [p.resolve() for p in args.provenance_input]
    require(args.sta_max_paths > 0 and args.partition_timeout_seconds > 0 and
            args.timeout_seconds > 0 and args.memory_gib > 0 and args.sample_seconds > 0,
            "limits must be positive")
    if args.worker:
        try:
            worker(args)
        except BaseException as error:
            write(args.output / "result.json", {"status": "failed", "error": str(error),
                  "traceback": traceback.format_exc(), "finished_at": now()})
            raise
    else:
        supervise(args, argv)


if __name__ == "__main__":
    main()
