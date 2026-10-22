#!/usr/bin/env python3
"""Paired Phase4/5 qualification on a sealed, complete NV_nvdla Phase3 prefix.

Capture provenance before producing the prefix, seal the completed prefix, then
run the pair in a new directory. Neither existing arm metrics nor output dirs
are reused. Phase5 timing below describes the projected inter-FPGA population.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from phase4_author_compare import digest, now, process_rss, write

OLD = "timing-aware-global-candidate-v1"
NEW = "synergistic-author-route-v1"
P5 = "deterministic-dependency-earliest-slot-v1"
CLOCKS = {"dla_core_clk": 10.0, "dla_csb_clk": 10.0}
PHYSICAL_MEMORY = "physically-implementable-shared-memory-model-v2"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text())


def code_identity():
    paths = list((ROOT / "src/emuflow").rglob("*.py"))
    paths += [p for p in (ROOT / "src/native").rglob("*")
              if p.is_file() and p.suffix in {".cpp", ".h", ".hpp"}]
    paths += [Path(__file__).resolve(), ROOT / "scripts/phase4_author_compare.py",
              ROOT / "scripts/benchmarks/prepare_nvdla_holdout.py",
              ROOT / "scripts/benchmarks/nvdla_ram_stubs.py"]
    prefix_script = ROOT / "scripts/phase4_nvdla_prefix.py"
    if prefix_script.is_file():
        paths.append(prefix_script)
    return {str(p.relative_to(ROOT)): digest(p) for p in sorted(paths)}


def runtime_identity():
    return {"python_version": sys.version, "python_sha256": digest(sys.executable),
            "packages": dict(sorted((d.metadata["Name"], d.version)
                                    for d in importlib.metadata.distributions()
                                    if d.metadata.get("Name")))}


def check_benchmark(benchmark):
    require(benchmark.get("top") == "NV_nvdla", "complete NV_nvdla top required")
    require(benchmark.get("clock_periods_ns") == CLOCKS, "two 10 ns clocks required")
    require(set(benchmark.get("clocks", [])) == set(CLOCKS), "clock set differs")
    require(benchmark.get("physical_mapping_profile") == "xilinx-ultrascaleplus-open-v1",
            "Xilinx native mapping required")
    preparation = benchmark.get("preparation", {})
    require(preparation.get("memory_policy") == PHYSICAL_MEMORY, "physical SRAM required")
    count = preparation.get("ram_wrapper_count", 0)
    require(count > 0 and preparation.get("ram_modeled_count") == count,
            "incomplete SRAM coverage")
    require(preparation.get("upstream_revision") == "8e06b1b9d85aab65b40d43d08eec5ea4681ff715" and
            preparation.get("upstream_archive_sha256") == "3dc7270c8975acd439959120ec22ea6c968c06e822f169b0ec3899d74f7fa1b8",
            "NVDLA source revision differs from the study contract")
    require(len(benchmark.get("sources", [])) >= 250, "incomplete RTL source list")
    require(benchmark.get("timing_io"), "clock-specific IO constraints required")


def capture_provenance(benchmark_path, source_root, prefix_tools=()):
    benchmark = read(benchmark_path)
    check_benchmark(benchmark)
    source_root = source_root.resolve()
    paths = [benchmark_path.resolve(), source_root / ".emuflow-source.json"]
    paths.extend(Path(path).resolve() for path in prefix_tools)
    for relative in benchmark["sources"]:
        path = (source_root / relative).resolve()
        require(path.is_relative_to(source_root), "RTL source escapes source root")
        paths.append(path)
    # Include headers and the compatibility/mapping inputs in the producer seal.
    for directory in benchmark["synthesis"].get("include_dirs", []):
        include = (source_root / directory).resolve()
        require(include.is_relative_to(source_root), "include directory escapes source root")
        paths.extend(p for p in include.rglob("*") if p.is_file())
    stamp = read(source_root / ".emuflow-source.json")
    prep = benchmark["preparation"]
    require(stamp.get("revision") == prep.get("upstream_revision") and
            stamp.get("archive_sha256") == prep.get("upstream_archive_sha256"),
            "RTL archive identity differs")
    return {"schema": "emuflow.nvdla-prefix-provenance/v1", "captured_at": now(),
            "source_sha256": code_identity(), "runtime": runtime_identity(),
            "benchmark": str(benchmark_path.resolve()), "source_root": str(source_root),
            "files": {str(p): digest(p) for p in sorted(set(paths))}}


def verify_provenance(provenance, replay_source=None):
    require(provenance.get("schema") == "emuflow.nvdla-prefix-provenance/v1", "bad provenance")
    current = code_identity()
    if replay_source is None:
        require(provenance.get("source_sha256") == current, "prefix producer code changed")
    else:
        require(replay_source == current, "qualification consumer code changed")
        prior = provenance.get("source_sha256", {})
        # Only the research gate changed. No computation source or prefix
        # producer is granted a historical reuse exception here.
        changed = {key for key in set(prior) | set(current) if prior.get(key) != current.get(key)}
        require(changed <= {"scripts/phase4_nvdla_compare.py"}, "historical computation source changed")
    require(provenance.get("runtime") == runtime_identity(), "prefix Python runtime changed")
    require(provenance.get("files"), "missing RTL provenance")
    for name, expected in provenance["files"].items():
        require(digest(name) == expected, f"prefix input changed: {name}")
    check_benchmark(read(provenance["benchmark"]))


def check_platform(value):
    fpgas = value.get("fpgas", [])
    require(len(fpgas) == 4, "four FPGAs required")
    require(all(f.get("part") == "xcvu19p-fsva3824-2-e" and
                f.get("utilization_limit") == 0.25 for f in fpgas), "platform part/budget differs")
    expected = {frozenset((f"fpga{a}", f"fpga{b}")) for a, b in [(0,2),(0,3),(1,2),(1,3)]}
    links = value.get("links", [])
    require(len(links) == 4 and {frozenset(l["endpoints"]) for l in links} == expected,
            "K2,2 topology required")
    require(all(l.get("capacity_sharing") == "shared_bidirectional" and
                l.get("data_lanes_per_direction") == 360 and l.get("bank_count") == 18 and
                l.get("wires_per_bank") == 20 for l in links), "shared 360-pair banks required")
    reference = ROOT / "platforms/virtual/rapidwright_xcvu19p_4fpga_mesh.json"
    from emuflow.platform import Platform
    raw = read(reference)
    # Phase1 serializes parsed defaults and drops non-consumed annotations.
    # Accept exactly either known representation, never arbitrary extra keys.
    require(value == raw or value == Platform.from_dict(raw).to_dict(),
            "platform differs from repository configuration")


def check_prefix(report, qualification=None):
    require(report.get("schema") == "emuflow.multi-fpga-stage-run/v1" and
            report.get("completed_through") == "phase3" and
            report.get("validation", {}).get("status") == "pass", "completed Phase3 prefix required")
    config = report["configuration"]
    require(config.get("clock_periods") == CLOCKS and config.get("frame_slots") == 1024,
            "prefix clocks/frame differ")
    require(config.get("cut_mode") == "static-exact-combinational" and
            config.get("patron_algorithm_version") == 14 and
            config.get("partition_provider") == "patron", "PATRON v14 Static Exact required")
    timing = report["timing"]
    sta = timing["sta"]
    projection = timing["cut_path_projection"]
    require(sta.get("status") == "pass" and sta.get("path_limit_reached") is False,
            "original timing extraction failed or reached its limit")
    require(projection.get("status") == "pass", "failed original path projection")
    if qualification is None:
        require(projection.get("uncovered_cut_nets") == 0, "independent member/structural qualification required")
    else:
        require(qualification.get("schema") == "emuflow.nvdla-prefix-qualification/v1" and
                qualification.get("status") == "pass", "invalid prefix qualification")
        accounting = qualification["original_accounting"]
        structural = qualification["structural_qualification"]
        require(accounting.get("status") == structural.get("status") == "pass" and
                accounting.get("scope") == "fixed-original-representative-population",
                "unqualified original population/structure")
        require(accounting["original_paths"] == sta["paths"] and
                accounting["projected_members"] == projection["projected_paths"] and
                structural["cut_nets"] == projection["cut_nets"], "qualification population differs")
        for key in ("cut_nets", "covered_cut_nets", "uncovered_cut_nets"):
            require(accounting[key] == projection[key], f"qualification coverage differs: {key}")
        require({k: v["period_ns"] for k,v in accounting["per_clock"].items()} == CLOCKS,
                "qualification clock coverage differs")
    require(projection.get("database_paths") == sta.get("paths") and
            projection.get("projected_paths", 0) > 0, "original/projected timing counts disagree")
    coverage = {"original_paths": sta["paths"], "projected_paths": projection["projected_paths"],
            "cut_nets": projection["cut_nets"], "scope": "projected-inter-fpga-paths"}
    if qualification:
        coverage.update(covered_cut_nets=projection["covered_cut_nets"],
                        uncovered_cut_nets=projection["uncovered_cut_nets"],
                        original_scope="fixed-original-representative-population")
    return coverage


def check_producer(report, provenance):
    expected = {key: value for key, value in provenance["source_sha256"].items()
                if key.startswith("src/emuflow/")}
    require(expected and report.get("producer_source_sha256") == expected,
            "completed prefix producer differs from captured provenance")
    runtime = provenance["runtime"]
    require(report.get("producer_python") == {"version": runtime["python_version"],
            "executable_sha256": runtime["python_sha256"]}, "completed prefix Python differs")


def check_patron_frame(common):
    """Check the emitted native input, not only the requested flow options."""
    path = common / "partition/patron/patron.in"
    require(path.is_file(), "missing actual PATRON input evidence")
    with path.open() as stream:
        header = stream.readline().strip()
        fields = stream.readline().split()
    require(header == "EMUFLOW_PATRON_INPUT_V14_BANKS_V1", "actual PATRON version differs")
    require(len(fields) >= 9 and fields[0] == "PARAM" and fields[8] == "1024",
            "actual PATRON frame_slots must be 1024")
    return path.resolve()


def seal_contract(common, platform, provenance_path, qualification_path=None, *, architecture_timing_db=None):
    provenance = read(provenance_path)
    qualification = read(qualification_path) if qualification_path else None
    verify_provenance(provenance, replay_source=qualification.get("evaluator_source_sha256") if qualification else None)
    if qualification is None:
        from phase4_nvdla_qualification import qualify_common
        qualification = qualify_common(common, provenance, architecture_timing_db=architecture_timing_db)
    verify_qualification(qualification, provenance, common)
    report = read(common / "multi-fpga-stage-report.json")
    check_producer(report, provenance)
    coverage = check_prefix(report, qualification)
    check_platform(read(platform))
    artifacts = {key: value["path"] for key, value in report["artifacts"].items()}
    from emuflow.platform import Platform
    producer_platform = common / artifacts["platform"]
    check_platform(read(producer_platform))
    require(Platform.load(producer_platform).to_dict() == Platform.load(platform).to_dict(),
            "prefix platform differs")
    partition = read(common / "partition/phase3_report.json")
    cuts = partition.get("validation", {}).get("combinational_cut_nets", 0)
    require(cuts > 0, "Static Exact qualification requires naturally selected combinational cuts")
    coverage["combinational_cut_nets"] = cuts
    patron_input = check_patron_frame(common)
    # Seal only the canonical downstream contract, not scratch, logs or builds.
    paths = {(common / name).resolve() for name in artifacts.values()}
    paths.add(patron_input)
    paths.update({(common / "multi-fpga-stage-report.json").resolve(), platform.resolve(),
                  (common / "partition/phase3_report.json").resolve()})
    assignment = (common / artifacts["assignment"]).resolve()
    paths.update({assignment.with_name("assignment.sqlite"), assignment.with_name("clusters.json")})
    original = (common / artifacts["timing_path_database"]).resolve()
    for descriptor in read(original).get("payloads", {}).values():
        relative = Path(descriptor["path"])
        require(not relative.is_absolute() and relative.name == str(relative), "invalid timing sidecar path")
        paths.add(original.parent / relative)
    files = dict(qualification["files"])
    files.update({str(p): digest(p) for p in sorted(paths) if str(p) not in files})
    wiring = read(platform)["platform"].get("virtual_board_wiring")
    require(wiring, "platform wiring missing")
    wires = platform.parent / wiring
    files[str(wires.resolve())] = digest(wires)
    for key in ("assignment", "timing_path_database", "timing_paths", "ir"):
        require(str((common / artifacts[key]).resolve()) in files, f"missing {key}")
    return {"schema": "emuflow.phase4-nvdla-contract/v1", "sealed_at": now(),
            "common_run": str(common.resolve()), "platform": str(platform.resolve()),
            "provenance": provenance, "files": files, "artifacts": artifacts,
            "coverage": coverage, "qualification": qualification, "configuration": report["configuration"]}


def verify_qualification(qualification, provenance, common, *, verify_files=True):
    require(qualification.get("producer_source_sha256") == provenance["source_sha256"] and
            qualification.get("common_run") == str(common.resolve()), "qualification producer/input differs")
    require(qualification.get("evaluator_source_sha256") == code_identity() and
            qualification.get("evaluator_runtime") == runtime_identity(), "qualification evaluator differs")
    required = {str((ROOT / "scripts" / name).resolve()) for name in
                ("phase4_nvdla_qualification.py", "phase4_nvdla_timing.py")}
    require(set(qualification.get("evaluator_sha256", {})) == required, "qualification evaluator seals missing")
    require(qualification.get("files"), "qualification input seals missing")
    checks = dict(qualification["evaluator_sha256"])
    if verify_files:
        checks.update(qualification["files"])
    for name, expected in checks.items():
        require(digest(name) == expected, f"qualification identity changed: {name}")


def stat_identity(files):
    return {name: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            for name in files for s in [Path(name).stat()]}


def verify_contract(contract, common, platform):
    require(contract.get("schema") == "emuflow.phase4-nvdla-contract/v1", "bad common contract")
    require(contract.get("common_run") == str(common.resolve()) and
            contract.get("platform") == str(platform.resolve()), "common input location differs")
    qualification = contract.get("qualification")
    require(qualification, "independent prefix qualification required")
    verify_provenance(contract["provenance"], replay_source=qualification["evaluator_source_sha256"])
    verify_qualification(qualification, contract["provenance"], common, verify_files=False)
    require(all(contract["files"].get(k) == v for k,v in qualification["files"].items()),
            "qualification inputs missing from common seal")
    require(contract.get("files"), "missing common file seals")
    for name, expected in contract["files"].items():
        require(digest(name) == expected, f"common input changed: {name}")
    check_platform(read(platform))
    report = read(common / "multi-fpga-stage-report.json")
    check_producer(report, contract["provenance"])
    coverage = check_prefix(report, qualification)
    patron_input = check_patron_frame(common)
    require(str(patron_input) in contract["files"], "actual PATRON input is not sealed")
    coverage["combinational_cut_nets"] = read(common / "partition/phase3_report.json")["validation"]["combinational_cut_nets"]
    require(coverage == contract["coverage"] and coverage["combinational_cut_nets"] > 0,
            "common coverage changed")


def phase5_options():
    # Deterministic scheduling does not invoke the academic native optimizers.
    return dict(provider=P5, simulation_frames=16, managed_storage=True)


def worker(spec_path):
    from emuflow.phase4 import run_phase4, validate_phase4
    from emuflow.phase5 import run_phase5, validate_phase5
    spec = read(spec_path)
    common, arm = Path(spec["common"]), Path(spec["arm_dir"])
    assignment = common / spec["artifacts"]["assignment"]
    timing = common / spec["artifacts"]["timing_paths"]
    platform = Path(spec["platform"])
    stage = spec["stage"]
    result = {"status": "failed", "stage": stage}
    started = time.monotonic()
    try:
        if stage == "phase4":
            options = dict(spec["phase4"])
            constraints = spec["artifacts"].get("route_constraints")
            if constraints:
                options["constraints_path"] = common / constraints
            report = run_phase4(assignment, platform, arm / stage,
                                timing_paths_path=timing, **options)
            validation = validate_phase4(assignment, platform, arm / stage / "routes.json", timing,
                expected_author_cost_policy=("author-pressure-v1" if spec["name"] == "B" else None),
                expected_author_order_policy=("hard-connections-v1" if spec["name"] == "B" else None))
        else:
            routes = arm / "phase4/routes.json"
            report = run_phase5(routes, platform, arm / stage, assignment_path=assignment,
                                **spec["phase5"])
            ratio = arm / stage / "ratio_plan.json"
            validation = validate_phase5(routes, platform, arm / stage / "schedule.json",
                ratio_plan_path=ratio if ratio.is_file() else None, assignment_path=assignment)
            require(validation.get("timing", {}).get("status") == "pass", "Phase5 timing missing")
            require(validation["timing"] == report.get("timing_validation"),
                    "independently reconstructed timing differs from producer")
        require(report.get("status") == "pass" and validation.get("status") == "pass",
                f"{stage} independent validation failed")
        result.update(status="pass", report=report, independent_validation=validation)
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
        traceback.print_exc()
    result["worker_seconds_including_validation"] = time.monotonic() - started
    write(Path(spec["result"]), result)
    return 0 if result["status"] == "pass" else 1


def run_stage(spec, directory, timeout, memory_gib, sample_seconds):
    directory.mkdir(parents=True)
    manifest = directory / "invocation.json"
    spec["result"] = str(directory / "result.json")
    write(manifest, spec)
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", str(manifest)]
    record = {"command": command, "started_at": now(), "status": "running",
              "peak_sampled_tree_rss_kib": None,
              "memory_measurement": "sampled process-tree RSS sum; shared pages may be counted twice; not a hard cap"}
    start = time.monotonic()
    with (directory / "worker.log").open("w") as log:
        child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        record.update(pid=child.pid, pgid=child.pid)
        write(directory / "process.json", record)
        try:
            while child.poll() is None:
                rss = process_rss(child.pid)
                record["peak_sampled_tree_rss_kib"] = max(record["peak_sampled_tree_rss_kib"] or 0, rss)
                record["elapsed_seconds"] = time.monotonic() - start
                write(directory / "process.json", record)
                require(rss <= memory_gib * 1024**2, "sampled memory guard exceeded")
                require(record["elapsed_seconds"] < timeout, "stage deadline exceeded")
                time.sleep(sample_seconds)
        except BaseException as error:
            record.update(status="stopped", error=str(error))
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            raise
        finally:
            record.update(exit_code=child.poll(), wall_seconds=time.monotonic()-start,
                          finished_at=now())
            if record["status"] == "running":
                record["status"] = "pass" if child.returncode == 0 else "failed"
            write(directory / "process.json", record)
    result = read(spec["result"]) if Path(spec["result"]).exists() else {"status": "failed"}
    require(record["status"] == "pass" and result["status"] == "pass", "stage failed; inspect worker.log")
    return {"resources": record, **result}


def comparison_table(summary):
    lines = ["Phase5 timing covers projected inter-FPGA paths, not whole-design or physical timing.",
             "", "| Metric | Old Phase4 | Author Phase4 | New minus old |",
             "|---|---:|---:|---:|"]
    arms = summary.get("arms", {})
    def row(label, a, b):
        delta = b-a if isinstance(a, (int, float)) and isinstance(b, (int, float)) else "—"
        lines.append(f"| {label} | {a} | {b} | {delta} |")
    for stage in ("phase4", "phase5"):
        pair = [arms.get(name, {}).get(stage, {}) for name in ("A", "B")]
        row(f"{stage} legality", *(p.get("status", "pending") for p in pair))
        row(f"{stage} seconds including independent validation", *(p.get("worker_seconds_including_validation", "unknown") for p in pair))
        row(f"{stage} sampled peak RSS KiB", *(p.get("resources", {}).get("peak_sampled_tree_rss_kib", "unknown") for p in pair))
    timing = [arms.get(name, {}).get("phase5", {}).get("independent_validation", {}).get("timing", {}) for name in ("A", "B")]
    for clock in CLOCKS:
        values = [t.get("per_clock", {}).get(clock, {}) for t in timing]
        for metric in ("timing_paths", "worst_slack_ns", "total_negative_slack_ns", "negative_slack_paths"):
            row(f"{clock} {metric}", *(v.get(metric, "not in projected population") for v in values))
    notes = []
    for metric in ("worst_slack_ns", "total_negative_slack_ns"):
        a, b = (t.get(metric) for t in timing)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            row(f"projected {metric}", a, b)
            if a < 0:
                notes.append(f"\n{metric} deficit reduction relative to old: {(b-a)/abs(a)*100:.6g}% (negative means worse).")
    lines.extend(notes)
    lines.extend(["", "Actual stage commands:", ""])
    import shlex
    for name, arm in arms.items():
        for stage, result in arm.items():
            lines.extend([f"{name} {stage}:", "```sh",
                          shlex.join(result["resources"]["command"]), "```"])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--capture-provenance", type=Path)
    parser.add_argument("--benchmark", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--prefix-tool", type=Path, action="append", default=[],
                        help="Repeat for each prefix executable and architecture timing database")
    parser.add_argument("--seal-contract", type=Path)
    parser.add_argument("--provenance", type=Path)
    parser.add_argument("--qualification", type=Path, help="Independent replay qualification of completed prefix")
    parser.add_argument("--architecture-timing-db", type=Path, help="Sealed producer architecture timing model for qualification")
    parser.add_argument("--common-run", type=Path)
    parser.add_argument("--platform", type=Path)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--old-router")
    parser.add_argument("--author-router")
    parser.add_argument("--ratio-optimizer")
    parser.add_argument("--timing-dag-optimizer")
    parser.add_argument("--slot-optimizer")
    parser.add_argument("--candidate-workers", type=int, default=1)
    parser.add_argument("--stage-timeout-seconds", type=float, default=7200)
    parser.add_argument("--memory-limit-gib", type=float, default=100)
    parser.add_argument("--sample-seconds", type=float, default=5)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker)
    if args.capture_provenance:
        require(args.benchmark and args.source_root, "benchmark/source-root required")
        require(not args.capture_provenance.exists(), "provenance already exists")
        require(args.prefix_tool, "explicit prefix tools/timing database required")
        write(args.capture_provenance, capture_provenance(args.benchmark, args.source_root, args.prefix_tool))
        return 0
    require(args.common_run and args.platform, "common-run/platform required")
    if args.seal_contract:
        require(args.provenance and not args.seal_contract.exists(), "new contract/provenance required")
        write(args.seal_contract, seal_contract(args.common_run, args.platform, args.provenance, args.qualification,
                                                  architecture_timing_db=args.architecture_timing_db))
        return 0
    require(args.contract and args.output and not args.output.exists(), "contract and new output required")
    require(args.stage_timeout_seconds > 0 and args.memory_limit_gib > 0 and args.sample_seconds > 0,
            "resource limits must be positive")
    require(args.candidate_workers == 1, "paired author routing requires candidate-workers=1")
    contract = read(args.contract)
    verify_contract(contract, args.common_run, args.platform)
    snapshots = stat_identity(contract["files"])
    tools = {}
    for name in ("old_router", "author_router", "ratio_optimizer", "timing_dag_optimizer", "slot_optimizer"):
        path = getattr(args, name)
        require(path and Path(path).is_file(), f"explicit executable required: {name}")
        tools[name] = {"path": str(Path(path).resolve()), "sha256": digest(path)}
    phase5 = phase5_options()
    args.output.mkdir(parents=True)
    summary = {"schema": "emuflow.phase4-nvdla-comparison/v1", "status": "running",
               "contract_sha256": digest(args.contract), "source_sha256": code_identity(),
               "runtime": runtime_identity(), "tools": tools, "coverage": contract["coverage"],
               "timing_scope": "projected-inter-fpga-paths; not whole-design or physical timing",
               "phase5_options": phase5, "arms": {}}
    write(args.output / "comparison.json", summary)
    try:
        for name, provider, router in [("A", OLD, "old_router"), ("B", NEW, "author_router")]:
            arm = args.output / name
            options = dict(provider=provider, router=tools[router]["path"], frame_slots=1024,
                           candidate_workers=args.candidate_workers, managed_storage=True)
            if name == "B":
                options.update(author_cost_policy="author-pressure-v1", author_order_policy="hard-connections-v1")
            summary["arms"][name] = {}
            for stage in ("phase4", "phase5"):
                require(stat_identity(contract["files"]) == snapshots, "immutable common input changed")
                require(all(digest(t["path"]) == t["sha256"] for t in tools.values()), "tool changed")
                spec = dict(name=name, stage=stage, common=str(args.common_run.resolve()),
                            platform=str(args.platform.resolve()), arm_dir=str(arm.resolve()),
                            artifacts=contract["artifacts"], phase4=options, phase5=phase5)
                result = run_stage(spec, arm / (stage + "-measurement"), args.stage_timeout_seconds,
                                   args.memory_limit_gib, args.sample_seconds)
                summary["arms"][name][stage] = result
                write(args.output / "comparison.json", summary)
        require(stat_identity(contract["files"]) == snapshots, "immutable common input changed")
        verify_provenance(contract["provenance"], replay_source=contract["qualification"]["evaluator_source_sha256"])
        timing = [summary["arms"][name]["phase5"]["independent_validation"]["timing"] for name in ("A", "B")]
        require(timing[0]["timing_paths"] == timing[1]["timing_paths"], "A/B timing population differs")
        require(set(timing[0]["per_clock"]) == set(timing[1]["per_clock"]) and
                set(timing[0]["per_clock"]).issubset(CLOCKS), "unexpected or unequal clock domains")
        for clock in timing[0]["per_clock"]:
            require(timing[0]["per_clock"][clock]["timing_paths"] == timing[1]["per_clock"][clock]["timing_paths"],
                    "per-clock timing populations differ")
        summary.update(status="pass", timing_delta_new_minus_old={
            key: timing[1][key]-timing[0][key] for key in
            ("worst_slack_ns", "total_negative_slack_ns", "negative_slack_paths")})
    except Exception as error:
        summary.update(status="failed", error=f"{type(error).__name__}: {error}")
        traceback.print_exc()
    write(args.output / "comparison.json", summary)
    (args.output / "comparison.md").write_text(comparison_table(summary))
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
