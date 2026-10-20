#!/usr/bin/env python3
"""Bounded contest A/B diagnostics for current and author-derived Phase 4.

Run one invocation on each host with separate --output directories. Input files
and exact-source-keyed binaries can be reused through --data and --build-root.
This tests routing/TDM legality and contest weights, not physical implementation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def source_files(source):
    """Include local headers transitively in the binary cache identity."""
    pending, found = [source], set()
    while pending:
        path = pending.pop().resolve()
        if path in found:
            continue
        found.add(path)
        for name in re.findall(r'^\s*#include\s+"([^"]+)"', path.read_text(), re.M):
            pending.append(path.parent / name)
    return {str(p.relative_to(ROOT)): digest(p) for p in sorted(found)}


def build_binaries(build_root, compiler, output):
    compiler_path = shutil.which(compiler)
    if not compiler_path:
        raise RuntimeError(f"compiler not found: {compiler}")
    version = subprocess.check_output([compiler_path, "--version"], text=True)
    tools = {}
    for name in ("tlr_router", "synergistic_author_router", "tdm_ratio_optimizer"):
        source = ROOT / "src/native" / (name + ".cpp")
        identity = {"sources": source_files(source), "compiler": compiler_path,
                    "compiler_sha256": digest(compiler_path), "compiler_version": version,
                    "flags": ["-std=c++17", "-O2", "-pthread"]}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        directory = build_root / name / key
        binary = directory / ("emuflow_" + name)
        cached = False
        if binary.is_file() and (directory / "BUILD.json").is_file():
            previous = json.loads((directory / "BUILD.json").read_text())
            cached = (previous.get("identity") == identity
                      and previous.get("binary_sha256") == digest(binary))
        if not cached:
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / (binary.name + f".tmp-{os.getpid()}")
            command = [compiler_path, *identity["flags"], str(source), "-o", str(temporary)]
            try:
                with (output / (name + ".build.log")).open("w") as log:
                    subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                   check=True, timeout=180)
                temporary.replace(binary)
            finally:
                temporary.unlink(missing_ok=True)
            write(directory / "BUILD.json", {"identity": identity,
                  "binary_sha256": digest(binary), "built_at": now(), "command": command})
        tools[name] = {"path": str(binary), "sha256": digest(binary),
                       "cache_key": key, "reused": cached, "identity": identity}
    return tools


def worker(manifest_path):
    from emuflow.contest_eda2023 import optimize_eda2023_tdm, evaluate_eda2023_solution
    from emuflow.phase4 import run_phase4

    manifest = json.loads(manifest_path.read_text())
    output = Path(manifest["output"])
    result = {"status": "running", "provider": manifest["provider"],
              "started_at": now(), "stage_seconds": {}}
    start = time.monotonic()

    def progress(stage):
        result["stage"] = stage
        result["updated_at"] = now()
        result["elapsed_seconds"] = time.monotonic() - start
        write(output / "progress.json", result)
        print(json.dumps({"stage": stage, "at": result["updated_at"]}), flush=True)

    try:
        artifacts = {key: Path(value) for key, value in manifest["artifacts"].items()}
        progress("phase4-running")
        stamp = time.monotonic()
        result["phase4"] = run_phase4(
            artifacts["assignment"], artifacts["boarddb"], output / "phase4",
            constraints_path=artifacts["route_constraints"],
            timing_paths_path=artifacts["timing_paths"], provider=manifest["provider"],
            router=manifest["router"], candidate_workers=1)
        result["stage_seconds"]["phase4"] = time.monotonic() - stamp
        progress("phase4-done")
        if result["phase4"].get("status") != "pass":
            raise RuntimeError("Phase 4 reported a non-pass result")
        progress("tdm-running")
        stamp = time.monotonic()
        result["tdm"] = optimize_eda2023_tdm(
            artifacts["instance"], output / "phase4/routes.json", output / "tdm",
            optimizer=manifest["optimizer"], **manifest["tdm_settings"])
        result["stage_seconds"]["tdm"] = time.monotonic() - stamp
        progress("tdm-done")
        if result["tdm"].get("status") != "pass":
            raise RuntimeError("TDM reported a non-pass result")
        stamp = time.monotonic()
        evaluation = evaluate_eda2023_solution(
            artifacts["instance"], output / "phase4/routes.json",
            Path(result["tdm"]["artifacts"]["tdm_plan"]))
        result["evaluation"] = {key: evaluation[key] for key in
                                ("schema", "status", "instance", "metrics")}
        result["stage_seconds"]["evaluation"] = time.monotonic() - stamp
        if result["evaluation"].get("status") != "pass":
            raise RuntimeError("independent contest evaluation reported a non-pass result")
        result["status"] = "pass"
        progress("done")
        code = 0
    except Exception as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
        traceback.print_exc()
        progress("failed")
        code = 1
    result["finished_at"] = now()
    write(output / "result.json", result)
    return code


def process_rss(root_pid):
    """A sampled process-tree RSS sum; shared pages may be counted repeatedly."""
    raw = subprocess.check_output(["ps", "-eo", "pid=,ppid=,rss="], text=True)
    rows = [tuple(map(int, line.split())) for line in raw.splitlines() if line.strip()]
    descendants = {root_pid}
    while True:
        expanded = descendants | {pid for pid, ppid, rss in rows if ppid in descendants}
        if expanded == descendants:
            break
        descendants = expanded
    return sum(rss for pid, ppid, rss in rows if pid in descendants)


def run_arm(manifest, timeout, sample_interval, rss_limit_gib=24):
    output = Path(manifest["output"])
    output.mkdir(parents=True)
    write(output / "manifest.json", manifest)
    started = time.monotonic()
    with (output / "worker.log").open("w") as log, (output / "resources.jsonl").open("w") as resources:
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                  "--worker", str(output / "manifest.json")],
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        record = {"pid": child.pid, "pgid": child.pid, "started_at": now(),
                  "status": "running", "timeout_seconds": timeout,
                  "rss_kind": "sampled process-tree RSS sum, KiB; not a hard memory cap",
                  "sampled_rss_limit_gib": rss_limit_gib,
                  "peak_sampled_tree_rss_kib": None, "rss_samples": 0}
        write(output / "process.json", record)
        while child.poll() is None:
            elapsed = time.monotonic() - started
            stop_reason = None
            if elapsed >= timeout:
                stop_reason = ("timeout", f"arm wall deadline {timeout:g}s exceeded")
            if stop_reason:
                record.update(status=stop_reason[0], reason=stop_reason[1])
                # The worker starts its own session, so this cannot select another job.
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                # Also clean up a native descendant if its Python parent exited first.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
                break
            try:
                rss = process_rss(child.pid)
                resources.write(json.dumps({"at": now(), "elapsed_seconds": elapsed,
                                           "tree_rss_kib": rss}) + "\n")
                resources.flush()
                record["peak_sampled_tree_rss_kib"] = max(record["peak_sampled_tree_rss_kib"] or 0, rss)
                record["rss_samples"] += 1
                if rss > rss_limit_gib * 1024 * 1024:
                    record.update(status="memory-limit", reason=(
                        f"sampled process-tree RSS {rss} KiB exceeded {rss_limit_gib:g} GiB; "
                        "sampled guard, not a hard cap"))
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()
                    break
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                record["rss_sampling_error"] = str(error)
                record.update(status="rss-sampling-failed", reason=(
                    "cannot enforce the sampled memory guard: " + str(error)))
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
                break
            time.sleep(min(sample_interval, max(0.01, timeout - elapsed)))
        record.update(exit_code=child.returncode, wall_seconds=time.monotonic() - started,
                      finished_at=now())
    result_path = output / "result.json"
    result = json.loads(result_path.read_text()) if result_path.is_file() else {}
    if record["status"] in {"timeout", "memory-limit", "rss-sampling-failed"}:
        result.update(status=record["status"], error=record["reason"])
    elif child.returncode != 0:
        result["status"] = "failed"
        result.setdefault("error", f"worker exited {child.returncode}; see worker.log")
    elif result.get("status") != "pass":
        result.update(status="failed", error="worker exited without a passing result")
    record["status"] = result["status"]
    result["process"] = record
    if (output / "progress.json").is_file():
        result["last_progress"] = json.loads((output / "progress.json").read_text())
    write(output / "process.json", record)
    write(output / "result.json", result)
    return result


def reuse_baseline(baseline_path, baseline, case, current, summary, provider):
    """Validate the scientific inputs and reuse A without launching its worker.

    A source commit can change while the baseline native implementation and
    normalized problem remain identical. Compare those identities explicitly,
    rather than requiring an unrelated author-router edit to rerun baseline A.
    """
    previous = baseline.get("cases", {}).get(case)
    if not previous or "A" not in previous.get("arms", {}):
        raise RuntimeError(f"baseline has no arm A for {case}")
    arm = previous["arms"]["A"]
    if arm.get("status") != "pass" or arm.get("provider") != provider:
        raise RuntimeError(f"baseline {case}/A must be a passing {provider} result")
    if previous.get("input_sha256") != current["input_sha256"]:
        raise RuntimeError(f"baseline raw input hash mismatch for {case}")
    if baseline.get("tdm_settings") != summary["tdm_settings"]:
        raise RuntimeError("baseline TDM settings mismatch")
    for name in ("tlr_router", "tdm_ratio_optimizer"):
        old = baseline.get("tools", {}).get(name, {})
        new = summary["tools"][name]
        if (old.get("identity") != new["identity"]
                or old.get("cache_key") != new["cache_key"]
                or old.get("sha256") != new["sha256"]):
            raise RuntimeError(f"baseline native tool identity mismatch: {name}")
    original_import = previous.get("import", {})
    artifacts = original_import.get("artifacts", {})
    if set(artifacts) != set(current["import"]["artifacts"]):
        raise RuntimeError(f"baseline normalized artifact set mismatch for {case}")
    hashes = {}
    for key, value in artifacts.items():
        path = Path(value)
        if not path.is_absolute():
            raise RuntimeError(f"baseline artifact path must be absolute: {path}")
        hashes[key] = digest(path)
    sealed = previous.get("normalized_artifact_sha256")
    if sealed is not None and sealed != hashes:
        raise RuntimeError(f"baseline normalized artifacts changed after sealing for {case}")
    if hashes != current["normalized_artifact_sha256"]:
        raise RuntimeError(f"baseline normalized model differs from current import for {case}")
    # Keep B bound to the original normalized input files. The newly generated
    # files are verification evidence only and never replace the baseline.
    current["import"] = original_import
    current["normalized_artifact_sha256"] = hashes
    reference = {
        "summary": str(baseline_path), "summary_sha256": digest(baseline_path),
        "source_commit": baseline.get("source_commit"), "host": baseline.get("host"),
        "runner_sha256": baseline.get("runner_sha256"),
        "input_sha256": previous["input_sha256"],
        "normalized_artifact_sha256": hashes, "tdm_settings": baseline["tdm_settings"],
        "tools": {name: {key: baseline["tools"][name][key]
                         for key in ("sha256", "cache_key", "identity")}
                  for name in ("tlr_router", "tdm_ratio_optimizer")},
        "validation": "raw hashes, current normalized bytes, sealed hashes if present, native tools and TDM settings matched",
    }
    reused = {key: arm[key] for key in
              ("status", "provider", "stage_seconds", "evaluation", "started_at", "finished_at")
              if key in arm}
    if "evaluation" in reused:
        reused["evaluation"] = {key: reused["evaluation"][key] for key in
                                ("schema", "status", "instance", "metrics")
                                if key in reused["evaluation"]}
    reused.update(reused=True, baseline_reference=reference)
    current["arms"]["A"] = reused
    current["baseline_reference"] = reference


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--build-root", type=Path)
    parser.add_argument("--cases", default="case1,case2,case3,case4,case5")
    parser.add_argument("--arms", default="A,B", help="fresh arms to run: A,B, A or B")
    parser.add_argument("--baseline-summary", type=Path,
                        help="reuse validated previous arm A and its normalized inputs; never rerun A")
    parser.add_argument("--arm-timeout", type=float, default=180)
    parser.add_argument("--sample-interval", type=float, default=2)
    parser.add_argument("--rss-limit-gib", type=float, default=24,
                        help="sampled process-tree RSS stop threshold per arm; not a hard cap")
    parser.add_argument("--compiler", default="g++")
    parser.add_argument("--tdm-iterations", type=int, default=100)
    parser.add_argument("--tdm-refinement-iterations", type=int, default=2000)
    parser.add_argument("--tdm-exact-domain-limit", type=int, default=2048)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker)
    if not all((args.output, args.data, args.build_root)):
        parser.error("--output, --data and --build-root are required")
    if args.arm_timeout <= 0 or args.sample_interval <= 0 or args.rss_limit_gib <= 0:
        parser.error("timeouts, sampling interval and RSS threshold must be positive")
    if min(args.tdm_iterations, args.tdm_refinement_iterations, args.tdm_exact_domain_limit) < 0:
        parser.error("TDM settings must be nonnegative")
    arms = args.arms.split(",")
    if len(set(arms)) != len(arms) or any(arm not in {"A", "B"} for arm in arms):
        parser.error("--arms must contain distinct A and/or B")
    if arms == ["B"] and not args.baseline_summary:
        parser.error("--arms B requires --baseline-summary to preserve comparison evidence")
    from fetch_eda2023_benchmarks import CASES, download
    from emuflow.contest_eda2023 import import_eda2023_case, EDA2023_TDM_PROVIDER
    from emuflow.timing_routing import GLOBAL_CANDIDATE_PROVIDER
    from emuflow.author_routing import AUTHOR_ROUTER_PROVIDER

    cases = args.cases.split(",")
    if len(set(cases)) != len(cases) or any(case not in CASES for case in cases):
        parser.error("cases must be distinct names case1 through case10")
    output, data, build_root = (p.resolve() for p in (args.output, args.data, args.build_root))
    if output.exists() and any(output.iterdir()):
        parser.error("--output must be a unique empty run directory")
    output.mkdir(parents=True, exist_ok=True)
    summary = {"schema": "emuflow.phase4-author-comparison/v1", "status": "running",
               "scope": "contest routing/TDM diagnostics; no physical/full-flow/timing signoff claim",
               "host": socket.gethostname(), "started_at": now(),
               "source_commit": git("rev-parse", "HEAD"), "branch": git("branch", "--show-current"),
               "git_status": git("status", "--porcelain"), "cases_requested": cases,
               "arms_requested": arms,
               "baseline_summary": str(args.baseline_summary.resolve()) if args.baseline_summary else None,
               "python": sys.version, "runner_sha256": digest(__file__),
               "python_source_sha256": {str(p.relative_to(ROOT)): digest(p)
                                        for p in sorted((ROOT / "src/emuflow").rglob("*.py"))},
               "fetcher_sha256": digest(ROOT / "scripts/fetch_eda2023_benchmarks.py"),
               "sampled_rss_limit_gib": args.rss_limit_gib,
               "tdm_settings": {"provider": EDA2023_TDM_PROVIDER,
                                "max_iterations": args.tdm_iterations,
                                "post_refinement_iterations": args.tdm_refinement_iterations,
                                "exact_domain_limit": args.tdm_exact_domain_limit}, "cases": {}}
    write(output / "summary.json", summary)
    try:
        baseline_path = args.baseline_summary.resolve() if args.baseline_summary else None
        baseline = json.loads(baseline_path.read_text()) if baseline_path else None
        summary["tools"] = build_binaries(build_root, args.compiler, output)
        write(output / "summary.json", summary)
        for case in cases:
            record = {"status": "preparing", "arms": {}}
            summary["cases"][case] = record
            write(output / "summary.json", summary)
            try:
                record["fetch"] = download(case, data / case)
                record["input_sha256"] = {name: digest(data / case / name)
                                          for name, _, _ in CASES[case]}
                old_case = baseline.get("cases", {}).get(case, {}) if baseline else {}
                # A sealed import from unchanged Python code needs only hash
                # verification. Legacy unsealed imports or changed adapters are
                # checked against a fresh normalization before read-only reuse.
                sealed_unchanged_import = (
                    old_case.get("normalized_artifact_sha256") is not None
                    and baseline.get("python_source_sha256") == summary["python_source_sha256"])
                if sealed_unchanged_import:
                    record["import"] = old_case["import"]
                    record["normalized_inputs_reused"] = True
                else:
                    record["import"] = import_eda2023_case(data / case, output / case / "inputs", case)
                    record["normalized_inputs_reused"] = False
                record["normalized_artifact_sha256"] = {
                    key: digest(value) for key, value in record["import"]["artifacts"].items()}
                if baseline is not None:
                    reuse_baseline(baseline_path, baseline, case, record, summary, GLOBAL_CANDIDATE_PROVIDER)
                for label, provider, binary in (
                    ("A", GLOBAL_CANDIDATE_PROVIDER, "tlr_router"),
                    ("B", AUTHOR_ROUTER_PROVIDER, "synergistic_author_router")):
                    if label not in arms or (label == "A" and baseline is not None):
                        continue
                    record["status"] = f"{label}-running"
                    write(output / "summary.json", summary)
                    manifest = {"output": str(output / case / label), "provider": provider,
                                "artifacts": record["import"]["artifacts"],
                                "router": summary["tools"][binary]["path"],
                                "optimizer": summary["tools"]["tdm_ratio_optimizer"]["path"],
                                "tdm_settings": summary["tdm_settings"]}
                    record["arms"][label] = run_arm(manifest, args.arm_timeout, args.sample_interval,
                                                    args.rss_limit_gib)
                    write(output / "summary.json", summary)
                record["status"] = ("pass" if all(a["status"] == "pass" for a in record["arms"].values())
                                    else "failed")
            except Exception as error:
                record.update(status="failed", error=f"{type(error).__name__}: {error}")
                traceback.print_exc()
            write(output / "summary.json", summary)
        summary["status"] = "pass" if all(c["status"] == "pass" for c in summary["cases"].values()) else "failed"
    except Exception as error:
        summary.update(status="failed", error=f"{type(error).__name__}: {error}")
        traceback.print_exc()
    summary["finished_at"] = now()
    write(output / "summary.json", summary)
    print(json.dumps({"status": summary["status"], "summary": str(output / "summary.json")}))
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
