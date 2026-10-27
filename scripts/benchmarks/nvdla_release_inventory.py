#!/usr/bin/env python3

"""Build a Phase 7D source/Phase-1 report for the custom NVDLA frontend."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

from emuflow_tools.benchmark import BENCHMARK_REPORT_SCHEMA
from emuflow.io import write_json


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_nvdla_source_files(repo_root: Path) -> List[Path]:
    """Return every pinned upstream file that determines NVDLA synthesis."""

    repo = repo_root.resolve()
    source = repo / "third_party" / "rtl" / "nvdla"
    paths = {source / ".emuflow-source.json"}
    paths.update((source / "vmod" / "nvdla").glob("*/*.v"))
    paths.update((source / "vmod" / "vlibs").glob("*.v"))
    paths.update(
        path
        for path in (source / "vmod" / "include").rglob("*")
        if path.is_file()
    )
    paths.update((source / "vmod" / "rams" / "synth").glob("nv_ram_*.v"))
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise ValueError(f"missing NVDLA source dependency: {missing[0]}")
    rtl_count = sum(
        1
        for path in paths
        if source / "vmod" / "nvdla" in path.parents and path.suffix == ".v"
    )
    if rtl_count < 250:
        raise ValueError(
            f"incomplete NVDLA source tree: found only {rtl_count} RTL files"
        )
    return sorted(paths)


def build_nvdla_benchmark_report(
    repo_root: Path,
    experiment_root: Path,
    platform: str,
    output_path: Path,
) -> Dict[str, Any]:
    repo = repo_root.resolve()
    experiment = experiment_root.resolve()
    phase1_path = experiment / "phase1" / "phase1_report.json"
    phase1 = json.loads(phase1_path.read_text(encoding="utf-8"))
    design = "NV_NVDLA_partition_a"
    if (
        phase1.get("status") != "pass"
        or phase1.get("design") != design
        or phase1.get("platform") != platform
    ):
        raise ValueError("NVDLA Phase 1 report does not match the release")

    sources = collect_nvdla_source_files(repo)
    relative_sources = [
        {
            "path": str(path.relative_to(repo)),
            "sha256": _sha256(path),
        }
        for path in sources
    ]
    report: Dict[str, Any] = {
        "schema": BENCHMARK_REPORT_SCHEMA,
        "benchmark": "nvdla_partition_a_logic_only",
        "design_id": design,
        "top": design,
        "source": {
            "root": str(repo),
            "files": relative_sources,
        },
        "synthesis": {
            "family": "xcup",
            "policy": "vivado-gated-clock-to-lut-ff",
            "mapped_json": "synthesis/mapped.json",
            "mapped_verilog": "synthesis/mapped.v",
            "log": "synthesis/yosys.log",
        },
        "gates": {
            "G0_source": "pass",
            "G1_elaboration": "pass",
            "G2_synthesis": "pass",
            "G3_emuir": "pass",
        },
        "phase1": phase1,
        "status": "pass",
    }
    write_json(output_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo_root", type=Path)
    parser.add_argument("experiment_root", type=Path)
    parser.add_argument("platform")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    report = build_nvdla_benchmark_report(
        args.repo_root,
        args.experiment_root,
        args.platform,
        args.output,
    )
    print(
        "EMUFLOW_NVDLA_SOURCE_INVENTORY "
        f"status=pass files={len(report['source']['files'])} "
        f"output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
