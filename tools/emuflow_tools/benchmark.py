"""Standalone benchmark synthesis and qualification."""
from __future__ import annotations
import hashlib
from pathlib import Path
from typing import Any, Dict, Optional
from emuflow.flow.benchmark_inputs import BenchmarkRun, BENCHMARK_RUN_SCHEMA
from emuflow_tools.timing.timing_io import timing_io_sha256
from emuflow.common.json_io import write_json
from emuflow_tools.frontend.frontend_native import run_rtl_frontend
from emuflow.common.errors import ValidationError

BENCHMARK_REPORT_SCHEMA = "emuflow.benchmark-report/v2"

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
    if synthesis['family'] != 'xcup' or synthesis['policy'] != 'native':
        raise ValidationError('benchmark frontend requires maintained xcup/native synthesis')
    platform_path = Path(spec.value["platform"])
    if not platform_path.is_absolute():
        platform_path = spec_path.resolve().parents[2] / platform_path

    phase1 = run_rtl_frontend(sources, spec.value['top'], platform_path, output_dir,
        clocks=spec.value['clocks'], include_dirs=include_dirs, defines=defines, yosys=yosys)
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
            "execution": "frontend/phase1/yosys-execution.json",
        },
        "gates": {
            "G0_source": "pass",
            "G1_elaboration": "pass",
            "G2_synthesis": "pass",
            "G3_emuir": "pass" if phase1["status"] == "pass" else "fail",
        },
        "phase1": phase1,
        "artifacts": {"phase1_report": "frontend/phase1/phase1_report.json",
                      "emuir": "frontend/phase1/design.emuir.sqlite",
                      "platform": "frontend/phase1/platform.normalized.json"},
        "status": "pass" if phase1["status"] == "pass" else "fail",
    }
    write_json(output_dir / "benchmark_report.json", report)
    return report
