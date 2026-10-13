#!/usr/bin/env python3
"""Run fresh, serial v5 PPro communication probes; retain raw pilot evidence.

This driver collects observations, not a fitted platform. PPro selects the TDM
ratios. Raw projects remain scratch for the active parser investigation and
must be removed after the compact terminal evidence is independently checked.
Run with PYTHONPATH=src from the exact versioned repository checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

from emuflow.errors import ValidationError
from emuflow.io import write_json
from emuflow.ppro_blackbox_campaign import _cleanup_generated_bundle
from emuflow.ppro_blackbox_communication import generate_communication_probe_bundle
from emuflow.ppro_blackbox_constraints import parse_logical_targets
from emuflow.ppro_blackbox_provenance import current_runner_revision
from emuflow.ppro_blackbox_runner import execute_blackbox_case
from emuflow.ppro_blackbox_runtime import (
    PProRuntimeConfig,
    parse_fpga_aliases,
    render_ppro_runtime_binding,
)


DRIVER_SCHEMA = "emuflow.ppro-bank-singlehop-campaign/v1"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["pilot", "matrix"], default="pilot")
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--platform-reference", type=Path, required=True)
    parser.add_argument("--result-root", type=Path, required=True)
    parser.add_argument("--authorized-writable-root", type=Path,
                        default=Path("/research/d4/gds/ziyiwang21"))
    parser.add_argument("--fpga-alias", action="append", required=True)
    parser.add_argument("--logical-target", action="append", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--public-prior-id", default="lx2-public-prior-v1")
    parser.add_argument("--configuration-id", required=True)
    parser.add_argument("--tool-release", required=True)
    parser.add_argument("--source-index", type=int, default=0)
    parser.add_argument("--sink-index", type=int, default=2)
    parser.add_argument("--clock-period-ns", type=float, default=10.0)
    parser.add_argument("--widths", nargs="+", type=int)
    parser.add_argument("--reverse-widths", nargs="+", type=int)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--role", choices=["fit", "holdout"], default="fit")
    parser.add_argument("--seed-base", type=int, default=1)
    parser.add_argument("--required-observed-tdm-ratios", nargs="+", type=int,
                        default=[], help="coverage check only; never forces a ratio")
    parser.add_argument("--max-processes", type=int, default=4)
    parser.add_argument("--timeout-seconds", type=float, default=21600.0)
    parser.add_argument("--keep-raw-project", action="store_true",
                        help="retain raw matrix projects during an active investigation; pilot always retains")
    return parser


def _points(args: argparse.Namespace) -> list[tuple[int, int, int]]:
    if args.repeats < 1 or args.seed_base < 0:
        raise ValidationError("repeats must be positive and seed-base nonnegative")
    if not math.isfinite(args.clock_period_ns) or args.clock_period_ns <= 0:
        raise ValidationError("clock-period-ns must be finite and positive")
    if args.source_index == args.sink_index:
        raise ValidationError("single-hop probe endpoints must differ")
    if any(value < 1 for value in args.required_observed_tdm_ratios):
        raise ValidationError("observed TDM ratios must be positive")
    if args.mode == "pilot":
        if (args.widths is not None or args.reverse_widths is not None
                or args.source_index != 0 or args.sink_index != 2
                or args.clock_period_ns != 10.0 or args.role != "fit"
                or args.repeats != 1):
            raise ValidationError("pilot is fixed at F0->F2, 32/0 and 32/16, 10 ns, fit; use matrix")
        return [(32, 0, 0), (32, 16, 0)]
    if not args.widths or not args.reverse_widths:
        raise ValidationError("matrix requires --widths and --reverse-widths")
    if (any(value < 1 for value in args.widths)
            or any(value < 0 for value in args.reverse_widths)
            or len(set(args.widths)) != len(args.widths)
            or len(set(args.reverse_widths)) != len(args.reverse_widths)):
        raise ValidationError("matrix widths must be unique; forward positive, reverse nonnegative")
    return list(itertools.product(args.widths, args.reverse_widths, range(args.repeats)))


def _source_commit(repository: Path | None = None) -> str:
    repository = (repository or Path(__file__).resolve().parents[1]).resolve()
    git_dir = repository / ".git"
    if git_dir.is_file():
        pointer = git_dir.read_text(encoding="utf-8").strip()
        if not pointer.startswith("gitdir: "):
            raise ValidationError("source checkout has an invalid Git directory pointer")
        git_dir = (repository / pointer[8:]).resolve()
    head = git_dir / "HEAD"
    if head.is_file():
        value = head.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-fA-F]{40}", value):
            return value.lower()
    # Detached deployments need no Git executable inside the tool container.
    # Local branch/worktree execution may resolve its ref through Git, but a
    # missing or malformed identity is never replaced with an unknown value.
    git = shutil.which("git")
    if git is None:
        raise ValidationError("source checkout requires a detached 40-hex HEAD when Git is unavailable")
    try:
        value = subprocess.check_output(
            [git, "rev-parse", "HEAD"], cwd=repository, text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValidationError("cannot resolve the source checkout commit") from error
    if not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise ValidationError("source checkout commit must be exactly 40 hexadecimal characters")
    return value.lower()


def run(args: argparse.Namespace) -> dict[str, Any]:
    points = _points(args)
    authorized = args.authorized_writable_root.resolve()
    root = args.result_root.resolve()
    if (not authorized.is_dir() or root == authorized
            or not root.is_relative_to(authorized)):
        raise ValidationError("result-root must be below the existing authorized writable root")
    if root.exists():
        raise ValidationError("result-root already exists; inspect its recorded process, never rerun it")
    aliases = parse_fpga_aliases(args.fpga_alias)
    targets = parse_logical_targets(args.logical_target)
    logical = {f"F{index}" for index in range(4)}
    if set(aliases.values()) != logical or set(targets) != logical:
        raise ValidationError("the four-FPGA campaign requires complete F0..F3 aliases and targets")
    if args.source_index not in range(4) or args.sink_index not in range(4):
        raise ValidationError("source and sink indices must select F0..F3")
    if not 1 <= args.max_processes <= 64:
        raise ValidationError("max-processes must be in [1, 64]")
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        raise ValidationError("timeout-seconds must be finite and positive")
    if not (args.install_root / "bin" / "rtlpart_linux").is_file():
        raise ValidationError("PPro install root lacks rtlpart_linux")
    if not (args.install_root / "setting_rtl.sh").is_file():
        raise ValidationError("PPro install root lacks setting_rtl.sh")
    if not args.platform_reference.is_file():
        raise ValidationError("opaque platform reference does not exist")
    keep_raw = args.mode == "pilot" or args.keep_raw_project
    summary: dict[str, Any] = {
        "schema": DRIVER_SCHEMA,
        "mode": args.mode,
        "status": "running",
        "calibration_status": "observations-only-not-a-fitted-platform",
        "source_commit": _source_commit(),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "runner_revision": current_runner_revision(),
        "process": {"host": socket.gethostname(), "pid": os.getpid(),
                    "parent_pid": os.getppid(), "process_group": os.getpgrp()},
        "raw_project_policy": ("retain-for-active-inspection" if keep_raw
                               else "remove-after-observation"),
        "expected_case_count": len(points),
        "cases": [],
    }
    # Exclusive creation prevents a second driver from overwriting or relaunching
    # a partially completed campaign. Appended matrices use fresh result roots.
    root.mkdir(parents=True, exist_ok=False)
    summary_path = root / "campaign-summary.json"
    write_json(summary_path, summary, compact=True)
    observed_ratios: set[int] = set()
    try:
        for width, reverse_width, repeat in points:
            case_root = root / f"w{width}-rw{reverse_width}-r{repeat}"
            case_root.mkdir(exist_ok=False)
            bundle = generate_communication_probe_bundle(
                case_root / "bundle", kind="latency", fpga_count=4,
                source_index=args.source_index, sink_indices=[args.sink_index],
                width=width, reverse_width=reverse_width, flow_count=1,
                clock_period_ns=args.clock_period_ns, local_baseline=False,
                forced_tdm_ratio=0, repeat=repeat, role=args.role,
                campaign_id=args.campaign_id, public_prior_id=args.public_prior_id,
                configuration_id=args.configuration_id, tool_release=args.tool_release,
                runner_revision=summary["runner_revision"], seed=args.seed_base + repeat,
                adapter_profile="ppro-2026-ordinary-reports-v1",
            )
            config = PProRuntimeConfig(
                case_dir=case_root / "run", install_root=args.install_root,
                platform_reference=args.platform_reference,
                documented_constraints=bundle.constraints_path,
                fpga_aliases=aliases, logical_targets=targets,
                authorized_writable_root=authorized, max_processes=args.max_processes,
                timeout_seconds=args.timeout_seconds, keep_raw_project=keep_raw,
            )
            binding = render_ppro_runtime_binding(
                bundle.run_spec, source_filelist=bundle.filelist_path, config=config,
            )
            result = execute_blackbox_case(bundle.run_spec, binding)
            metrics = result["metrics"]
            details = metrics.get("calibration_details", {})
            bank_rows = details.get("bank_rows", [])
            ratios = sorted({int(row["maximum_tdm_ratio"]) for row in bank_rows
                             if row["maximum_tdm_ratio"] > 0})
            observed_ratios.update(ratios)
            case_summary = {
                "case_id": result["identity"]["id"],
                "forward_data_bits": width, "reverse_data_bits": reverse_width,
                "repeat": repeat, "outcome": result["execution"]["outcome"],
                "failure_code": result["execution"]["failure_code"],
                "runtime_seconds": result["execution"]["runtime_seconds"],
                "observation": str(binding.output_path.relative_to(root)),
                "bank_row_count": len(bank_rows),
                "observed_bank_tdm_ratios": ratios,
                "timing_path_count": len(details.get("timing_paths", [])),
                "identification": details.get("identification", {}),
            }
            write_json(case_root / "case-summary.json", case_summary, compact=True)
            summary["cases"].append(case_summary)
            if not keep_raw:
                # execute_blackbox_case returns only after the tool has ended
                # and its compact observation has passed validation. Preserve
                # the terminal summary before removing replay-only probe RTL,
                # filelists, parameters, constraints, and the duplicate spec.
                # The shared cleanup rejects unexpected files and symlinks.
                _cleanup_generated_bundle(bundle.root)
            write_json(summary_path, summary, compact=True)
            print(json.dumps({"case": case_summary}, separators=(",", ":")), flush=True)
            if case_summary["outcome"] != "pass":
                summary["status"] = "failed"
                break
        if summary["status"] == "running":
            missing = sorted(set(args.required_observed_tdm_ratios) - observed_ratios)
            summary["missing_observed_tdm_ratios"] = missing
            summary["status"] = "incomplete-ratio-coverage" if missing else "completed"
    except Exception as error:
        summary["status"] = "failed"
        summary["driver_error"] = {"type": type(error).__name__, "message": str(error)}
        write_json(summary_path, summary, compact=True)
        raise
    write_json(summary_path, summary, compact=True)
    return summary


def main(argv: list[str] | None = None) -> int:
    summary = run(_parser().parse_args(argv))
    print(json.dumps({"status": summary["status"], "case_count": len(summary["cases"]),
                      "calibration_status": summary["calibration_status"]}), flush=True)
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
