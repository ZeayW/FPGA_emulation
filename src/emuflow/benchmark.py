from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .errors import EmuFlowError, ValidationError
from .io import read_json, write_json
from .phase1 import run_phase1
from .synthesis import (
    VALID_SYNTHESIS_POLICIES,
    VALID_XILINX_FAMILIES,
    YOSYS_DEFINE,
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
VALID_CALIBRATION_HOLDOUT_CLASSES = {
    "secworks_aes",
    "open_cpu",
    "koios_compute",
    "koios_dla",
    "nvdla",
}
_HDL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


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


def _optional_string_list(value: Any, context: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValidationError(f"{context}: expected a string array")
    if len(value) != len(set(value)):
        raise ValidationError(f"{context}: duplicate values are not allowed")
    return list(value)


def _relative_path(value: str, context: str) -> str:
    path = Path(value)
    if path.is_absolute() or value in {"", "."} or ".." in path.parts:
        raise ValidationError(f"{context}: expected a contained relative path")
    return path.as_posix()


def _validate_timing_io(value: Any, clocks: List[str]) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {
        "input_groups",
        "output_groups",
    }:
        raise ValidationError(
            "benchmark.timing_io: expected input_groups and output_groups"
        )
    clock_set = set(clocks)
    for direction in ("input", "output"):
        groups = value[f"{direction}_groups"]
        if not isinstance(groups, list):
            raise ValidationError(
                f"benchmark.timing_io.{direction}_groups: expected an array"
            )
        seen_ports: set[str] = set()
        for index, group in enumerate(groups):
            context = f"benchmark.timing_io.{direction}_groups[{index}]"
            if not isinstance(group, dict) or set(group) != {
                "clock",
                "delay_ns",
                "ports",
            }:
                raise ValidationError(
                    f"{context}: expected clock, delay_ns, and ports"
                )
            clock = group["clock"]
            delay = group["delay_ns"]
            ports = group["ports"]
            if clock not in clock_set:
                raise ValidationError(f"{context}.clock: undeclared clock")
            if (
                isinstance(delay, bool)
                or not isinstance(delay, (int, float))
                or not math.isfinite(float(delay))
                or float(delay) < 0.0
            ):
                raise ValidationError(
                    f"{context}.delay_ns: expected a finite nonnegative delay"
                )
            if (
                not isinstance(ports, list)
                or not ports
                or not all(
                    isinstance(port, str)
                    and _HDL_IDENTIFIER.fullmatch(port) is not None
                    for port in ports
                )
                or len(ports) != len(set(ports))
            ):
                raise ValidationError(
                    f"{context}.ports: expected unique HDL port identifiers"
                )
            if any(port in clock_set for port in ports):
                raise ValidationError(f"{context}.ports: clock ports are not data I/O")
            if seen_ports.intersection(ports):
                raise ValidationError(
                    f"benchmark.timing_io.{direction}_groups: duplicate ports"
                )
            seen_ports.update(ports)


def timing_io_sha256(value: Any) -> Optional[str]:
    """Return the canonical identity of a benchmark timing-I/O contract.

    The benchmark run remains the canonical owner. Timing producers persist
    only this compact identity so a holdout assembler can prove that PPro and
    EmuFlow consumed the same environment without duplicating all port groups.
    """

    if value is None:
        return None
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
        _validate_timing_io(value.get("timing_io"), clocks)
        physical_mapping_profile = value.get("physical_mapping_profile")
        if (
            physical_mapping_profile is not None
            and physical_mapping_profile not in VALID_PHYSICAL_MAPPING_PROFILES
        ):
            raise ValidationError(
                "benchmark.physical_mapping_profile: unsupported value"
            )
        _required_string(value, "platform", "benchmark")
        holdout_class = value.get("calibration_holdout_class")
        if (
            holdout_class is not None
            and holdout_class not in VALID_CALIBRATION_HOLDOUT_CLASSES
        ):
            raise ValidationError(
                "benchmark.calibration_holdout_class: unsupported value"
            )
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
        include_dirs = _optional_string_list(
            synthesis.get("include_dirs"), "benchmark.synthesis.include_dirs"
        )
        for index, include_dir in enumerate(include_dirs):
            _relative_path(
                include_dir, f"benchmark.synthesis.include_dirs[{index}]"
            )
        defines = _optional_string_list(
            synthesis.get("defines"), "benchmark.synthesis.defines"
        )
        for index, define in enumerate(defines):
            if YOSYS_DEFINE.fullmatch(define) is None:
                raise ValidationError(
                    "benchmark.synthesis.defines["
                    f"{index}]: expected NAME or NAME=VALUE"
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

    def resolve_include_dirs(self, source_root: Path) -> List[Path]:
        root = source_root.resolve()
        resolved: List[Path] = []
        for raw_path in self.value["synthesis"].get("include_dirs", []):
            candidate = (root / raw_path).resolve()
            if (
                candidate != root
                and root not in candidate.parents
            ) or not candidate.is_dir():
                raise EmuFlowError(
                    "benchmark include directory escapes its source root or "
                    f"does not exist: {candidate}"
                )
            resolved.append(candidate)
        return resolved

    def compilation_context(self, source_root: Path) -> Dict[str, Any]:
        root = source_root.resolve()
        return {
            "include_dirs": [
                path.relative_to(root).as_posix()
                for path in self.resolve_include_dirs(root)
            ],
            "defines": list(self.value["synthesis"].get("defines", [])),
        }


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
