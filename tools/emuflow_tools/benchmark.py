"""Standalone benchmark synthesis and qualification."""
from __future__ import annotations
import hashlib
from pathlib import Path
from typing import Any, Dict, Optional
from emuflow.benchmark_inputs import BenchmarkRun, BENCHMARK_RUN_SCHEMA
from emuflow.timing_io import timing_io_sha256
from emuflow.io import write_json
from emuflow.phase1 import run_phase1
from emuflow.platform import Platform
from emuflow.synthesis import run_yosys

BENCHMARK_REPORT_SCHEMA = "emuflow.benchmark-report/v1"

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_benchmark(
    spec_path: Path,
    source_root: Path,
    output_dir: Path,
    yosys: Optional[str] = None,
) -> Dict[str, Any]:
    spec = BenchmarkRun.load(spec_path)
    sources = spec.resolve_sources(source_root)
    include_dirs = spec.resolve_include_dirs(source_root)
    defines = spec.value["synthesis"].get("defines", [])
    synthesis = spec.value["synthesis"]
    mapped_json = output_dir / "synthesis" / "mapped.json"
    mapped_verilog = output_dir / "synthesis" / "mapped.v"
    yosys_log = output_dir / "synthesis" / "yosys.log"
    platform_path = Path(spec.value["platform"])
    if not platform_path.is_absolute():
        platform_path = spec_path.resolve().parents[2] / platform_path

    run_yosys(
        sources=sources,
        top=spec.value["top"],
        output=mapped_json,
        family=synthesis["family"],
        policy=synthesis["policy"],
        verilog_output=mapped_verilog,
        executable=yosys,
        log_path=yosys_log,
        include_dirs=include_dirs,
        defines=defines,
    )
    phase1 = run_phase1(
        yosys_json=mapped_json,
        platform_path=platform_path,
        output_dir=output_dir / "phase1",
        top=spec.value["top"],
        clocks=spec.value["clocks"],
    )
    relative_sources = [
        {
            "path": str(path.relative_to(source_root.resolve())),
            "sha256": _sha256(path),
        }
        for path in sources
    ]
    report: Dict[str, Any] = {
        "schema": BENCHMARK_REPORT_SCHEMA,
        "benchmark": spec.value["id"],
        "design_id": spec.value["design_id"],
        "top": spec.value["top"],
        "source": {
            "root": str(source_root.resolve()),
            "files": relative_sources,
        },
        "synthesis": {
            "family": synthesis["family"],
            "policy": synthesis["policy"],
            "include_dirs": [
                path.relative_to(source_root.resolve()).as_posix()
                for path in include_dirs
            ],
            "defines": list(defines),
            "mapped_json": "synthesis/mapped.json",
            "mapped_verilog": "synthesis/mapped.v",
            "log": "synthesis/yosys.log",
        },
        "gates": {
            "G0_source": "pass",
            "G1_elaboration": "pass",
            "G2_synthesis": "pass",
            "G3_emuir": "pass" if phase1["status"] == "pass" else "fail",
        },
        "phase1": phase1,
        "status": "pass" if phase1["status"] == "pass" else "fail",
    }
    write_json(output_dir / "benchmark_report.json", report)
    return report
