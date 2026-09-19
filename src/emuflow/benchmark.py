from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .errors import EmuFlowError, ValidationError
from .io import read_json, write_json
from .phase1 import run_phase1
from .synthesis import (
    VALID_SYNTHESIS_POLICIES,
    VALID_XILINX_FAMILIES,
    run_yosys,
)
from .xilinx_primitives import XILINX_ULTRASCALEPLUS_OPEN_PROFILE


BENCHMARK_RUN_SCHEMA = "emuflow.benchmark-run/v1"
BENCHMARK_REPORT_SCHEMA = "emuflow.benchmark-report/v1"
VALID_PHYSICAL_MAPPING_PROFILES = {
    "generic-soft",
    "vtr-hard-blocks",
    XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
}


def _required_string(value: Mapping[str, Any], key: str, context: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValidationError(f"{context}.{key}: expected a non-empty string")
    return result


def _string_list(value: Any, context: str) -> List[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise ValidationError(f"{context}: expected a non-empty string array")
    return list(value)


class BenchmarkRun:
    def __init__(self, value: Mapping[str, Any]):
        self.value = dict(value)
        self.validate()

    @classmethod
    def load(cls, path: Path) -> "BenchmarkRun":
        return cls(read_json(path))

    def validate(self) -> None:
        value = self.value
        if value.get("schema") != BENCHMARK_RUN_SCHEMA:
            raise ValidationError(
                f"benchmark.schema: expected {BENCHMARK_RUN_SCHEMA!r}"
            )
        _required_string(value, "id", "benchmark")
        _required_string(value, "design_id", "benchmark")
        _required_string(value, "top", "benchmark")
        _string_list(value.get("sources"), "benchmark.sources")
        clocks = _string_list(value.get("clocks"), "benchmark.clocks")
        clock_periods = value.get("clock_periods_ns")
        if clock_periods is not None and (
            not isinstance(clock_periods, dict)
            or set(clock_periods) != set(clocks)
            or any(
                isinstance(period, bool)
                or not isinstance(period, (int, float))
                or not math.isfinite(float(period))
                or float(period) <= 0.0
                for period in clock_periods.values()
            )
        ):
            raise ValidationError(
                "benchmark.clock_periods_ns: expected one positive finite "
                "period for every declared clock"
            )
        physical_mapping_profile = value.get("physical_mapping_profile")
        if (
            physical_mapping_profile is not None
            and physical_mapping_profile not in VALID_PHYSICAL_MAPPING_PROFILES
        ):
            raise ValidationError(
                "benchmark.physical_mapping_profile: unsupported value"
            )
        _required_string(value, "platform", "benchmark")
        synthesis = value.get("synthesis")
        if not isinstance(synthesis, dict):
            raise ValidationError("benchmark.synthesis: expected an object")
        family = _required_string(synthesis, "family", "benchmark.synthesis")
        if family not in VALID_XILINX_FAMILIES:
            raise ValidationError(
                f"benchmark.synthesis.family: unsupported value {family!r}"
            )
        policy = _required_string(synthesis, "policy", "benchmark.synthesis")
        if policy not in VALID_SYNTHESIS_POLICIES:
            raise ValidationError(
                f"benchmark.synthesis.policy: unsupported value {policy!r}"
            )

    def resolve_sources(self, source_root: Path) -> List[Path]:
        root = source_root.resolve()
        resolved: List[Path] = []
        for pattern in self.value["sources"]:
            matches = sorted(root.glob(pattern))
            if not matches:
                raise EmuFlowError(
                    f"benchmark source pattern {pattern!r} matched no files "
                    f"under {root}"
                )
            for path in matches:
                candidate = path.resolve()
                if root not in candidate.parents or not candidate.is_file():
                    raise EmuFlowError(
                        f"benchmark source escapes its source root: {candidate}"
                    )
                if candidate not in resolved:
                    resolved.append(candidate)
        return resolved


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
