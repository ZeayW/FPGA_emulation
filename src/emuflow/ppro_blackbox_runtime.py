"""Runtime-only renderer for an authorized ordinary PPro black-box run.

The generated launcher references the installed tool and a user-selected
platform configuration, but never reads, copies, hashes, or serializes either
one.  All generated scripts, raw reports, and the active project are confined
to one case directory and are deleted after the compact observation is made.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping

from .errors import ValidationError
from .ppro_blackbox_ppro_adapter import PPRO_2026_REPORT_PROFILE
from .ppro_blackbox_constraints import (
    render_ppro_prepartition_constraints,
    validate_logical_targets,
)
from .ppro_blackbox_runner import RuntimeBinding, validate_run_spec


@dataclass(frozen=True)
class PProRuntimeConfig:
    """Non-publishable execution inputs for one PPro case."""

    case_dir: Path
    install_root: Path
    platform_reference: Path
    documented_constraints: Path
    fpga_aliases: Mapping[str, str]
    logical_targets: Mapping[str, str]
    authorized_writable_root: Path = Path("/research/d4/gds/ziyiwang21")
    max_processes: int = 4
    utilization_limit_percent: int = 75
    timeout_seconds: float = 21600.0
    environment: Mapping[str, str] = field(default_factory=dict)
    keep_raw_project: bool = False


def _tcl_word(value: str, context: str) -> str:
    if not value or any(token in value for token in ("\x00", "\n", "\r", "{", "}")):
        raise ValidationError(f"{context}: cannot be represented as a safe Tcl word")
    return "{" + value + "}"


def _runtime_filelist(source: Path, destination: Path) -> None:
    """Materialize a runtime-only absolute filelist for generated probes.

    Calibration generators intentionally publish relative source names.  The
    ordinary PPro process runs in an isolated directory, so the runtime copy
    resolves those names without modifying or duplicating RTL contents.
    Compiler-option filelists are rejected here; application holdouts must
    supply a documented wrapper filelist rather than silently changing flags.
    """

    if not source.is_file():
        raise ValidationError("PPro runtime filelist does not exist")
    lines = []
    for number, raw in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        value = raw.strip()
        if not value or value.startswith("#") or value.startswith("//"):
            continue
        if value.startswith(("-", "+")) or any(character.isspace() for character in value):
            raise ValidationError(
                f"PPro runtime filelist line {number}: options and whitespace are not supported"
            )
        path = Path(value)
        if not path.is_absolute():
            path = source.parent / path
        path = path.resolve()
        if not path.is_file():
            raise ValidationError(f"PPro runtime filelist line {number}: source does not exist")
        lines.append(str(path))
    if not lines:
        raise ValidationError("PPro runtime filelist is empty")
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _validate_config(config: PProRuntimeConfig) -> None:
    if isinstance(config.max_processes, bool) or not isinstance(config.max_processes, int):
        raise ValidationError("PPro max_processes must be an integer")
    if config.max_processes <= 0 or config.max_processes > 64:
        raise ValidationError("PPro max_processes must be in [1, 64]")
    if isinstance(config.utilization_limit_percent, bool) or not isinstance(
        config.utilization_limit_percent, int
    ):
        raise ValidationError("PPro utilization limit must be an integer percentage")
    if config.utilization_limit_percent <= 0 or config.utilization_limit_percent > 100:
        raise ValidationError("PPro utilization limit must be in [1, 100]")
    if config.timeout_seconds <= 0:
        raise ValidationError("PPro timeout must be positive")
    case_dir = config.case_dir.resolve()
    writable_root = config.authorized_writable_root.resolve()
    if case_dir == writable_root or not case_dir.is_relative_to(writable_root):
        raise ValidationError(
            "PPro case directory must stay below the authorized writable root"
        )
    if not config.fpga_aliases:
        raise ValidationError("PPro runtime requires a physical-to-logical FPGA alias map")
    physical = list(config.fpga_aliases)
    logical = list(config.fpga_aliases.values())
    if len(set(logical)) != len(logical):
        raise ValidationError("PPro FPGA aliases must be one-to-one")
    if any(not re.fullmatch(r"F[0-9]+", value) for value in physical + logical):
        raise ValidationError("PPro physical and logical FPGA aliases must use F<n>")
    targets = validate_logical_targets(config.logical_targets)
    if set(targets) != set(logical):
        raise ValidationError(
            "PPro report aliases and logical placement targets must cover the same FPGAs"
        )

    install_root = config.install_root.resolve()
    executable = install_root / "bin" / "rtlpart_linux"
    settings = install_root / "setting_rtl.sh"
    if not executable.is_file() or not settings.is_file():
        raise ValidationError("PPro install root lacks rtlpart_linux or setting_rtl.sh")
    # These two files are intentionally only existence-checked.  Their content
    # is opaque to the calibration framework.
    if not config.platform_reference.resolve().is_file():
        raise ValidationError("PPro platform reference does not exist")
    if not config.documented_constraints.resolve().is_file():
        raise ValidationError("documented provider-neutral constraints do not exist")


def render_ppro_runtime_binding(
    raw_spec: Mapping[str, Any],
    *,
    source_filelist: Path,
    config: PProRuntimeConfig,
) -> RuntimeBinding:
    """Render a disposable PPro project and return its runtime binding."""

    spec = validate_run_spec(raw_spec)
    if spec["adapter"]["profile"] != PPRO_2026_REPORT_PROFILE:
        raise ValidationError("PPro runtime renderer requires the real ordinary-report profile")
    _validate_config(config)

    case_dir = config.case_dir.resolve()
    case_dir.mkdir(parents=True, exist_ok=True)
    project_dir = case_dir / "project"
    temporary_dir = case_dir / ".tmp"
    runtime_filelist = case_dir / ".runtime-files.f"
    ppro_constraints = case_dir / ".prepartition.cfg"
    tcl_path = case_dir / ".run-ppro.tcl"
    launcher_path = case_dir / ".run-ppro.sh"
    output_path = case_dir / "observation.json"
    if any(
        path.exists()
        for path in (
            project_dir,
            temporary_dir,
            runtime_filelist,
            ppro_constraints,
            tcl_path,
            launcher_path,
        )
    ):
        raise ValidationError("PPro case directory contains an active or stale runtime")
    if output_path.exists():
        raise ValidationError("PPro case already has an observation; use a new case directory")
    _runtime_filelist(source_filelist.resolve(), runtime_filelist)
    render_ppro_prepartition_constraints(
        config.documented_constraints.resolve(),
        config.logical_targets,
        ppro_constraints,
    )
    temporary_dir.mkdir()

    top = spec["workload"]["top_module"]
    tcl = "\n".join(
        (
            "# Generated runtime-only PPro black-box calibration script.",
            "create_project " + _tcl_word(str(project_dir), "PPro project directory"),
            "set_partition_mode -r -d",
            "create_rtlpart",
            "run_compile -top "
            + _tcl_word(top, "PPro top module")
            + " -lib work -filelist "
            + _tcl_word(str(runtime_filelist), "PPro runtime filelist"),
            "run_pre_partition -stf "
            + _tcl_word(str(config.platform_reference.resolve()), "PPro platform reference")
            + " -config "
            + _tcl_word(str(ppro_constraints), "PPro prepartition constraints")
            + " "
            + " ".join(
                f"-{resource}_area {config.utilization_limit_percent}"
                for resource in ("lut", "ff", "bram", "uram", "dsp")
            ),
            f"run_partition -costmode 1 -max_process_num {config.max_processes}",
            "run_system_route",
            "exit",
            "",
        )
    )
    tcl_path.write_text(tcl, encoding="utf-8")

    settings = config.install_root.resolve() / "setting_rtl.sh"
    executable = config.install_root.resolve() / "bin" / "rtlpart_linux"
    launcher = "\n".join(
        (
            "#!/usr/bin/env bash",
            "set -eo pipefail",
            "source " + shlex.quote(str(settings)),
            "exec " + shlex.quote(str(executable)) + " < " + shlex.quote(str(tcl_path)),
            "",
        )
    )
    launcher_path.write_text(launcher, encoding="utf-8")
    launcher_path.chmod(0o700)

    report_dir = project_dir / "rtlpart" / "report"
    cleanup_paths = (
        runtime_filelist,
        ppro_constraints,
        tcl_path,
        launcher_path,
        temporary_dir,
    )
    if not config.keep_raw_project:
        cleanup_paths += (project_dir,)
    return RuntimeBinding(
        case_dir=case_dir,
        command=("bash", str(launcher_path)),
        report_paths={
            "resource_summary": report_dir / "pa0.rpt",
            "partition_summary": report_dir / "pa0.rpt",
            "route_summary": report_dir / "sr0.rpt",
            "system_timing": report_dir / "sr0_time.rpt",
        },
        output_path=output_path,
        environment={
            **dict(config.environment),
            "TMPDIR": str(temporary_dir),
            "TMP": str(temporary_dir),
            "TEMP": str(temporary_dir),
        },
        fpga_aliases=dict(config.fpga_aliases),
        timeout_seconds=config.timeout_seconds,
        cleanup_raw_reports=True,
        cleanup_paths=cleanup_paths,
        retain_failure_diagnostics=config.keep_raw_project,
    )


def parse_fpga_aliases(values: list[str]) -> Dict[str, str]:
    """Parse repeatable PHYSICAL=F<n> runtime-only CLI arguments."""

    result: Dict[str, str] = {}
    for value in values:
        physical, separator, logical = value.partition("=")
        if not separator or not physical or not logical:
            raise ValidationError("PPro FPGA alias must use PHYSICAL=F<n>")
        if not re.fullmatch(r"F[0-9]+", physical) or not re.fullmatch(
            r"F[0-9]+", logical
        ):
            raise ValidationError("PPro physical and logical aliases must use F<n>")
        if physical in result:
            raise ValidationError("PPro FPGA alias repeats a physical identifier")
        result[physical] = logical
    if len(set(result.values())) != len(result):
        raise ValidationError("PPro FPGA aliases must be one-to-one")
    return result
