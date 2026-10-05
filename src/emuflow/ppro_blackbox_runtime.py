"""Runtime-only renderer for an authorized ordinary PPro black-box run.

The generated launcher references the installed tool and a user-selected
platform configuration, but never reads, copies, hashes, or serializes either
one.  All generated scripts, raw reports, and the active project are confined
to one case directory and are deleted after the compact observation is made.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping

from .errors import ValidationError
from .ppro_blackbox_ppro_adapter import PPRO_2026_REPORT_PROFILE
from .ppro_blackbox_constraints import (
    render_ppro_prepartition_constraints,
    render_ppro_ssta_constraints,
    validate_logical_targets,
)
from .ppro_blackbox_provenance import require_current_runner_revision
from .ppro_blackbox_runner import RuntimeBinding, validate_run_spec
from .synthesis import YOSYS_DEFINE


PPRO_COMPILATION_CONTEXT_SCHEMA = "emuflow.ppro-compilation-context/v1"


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


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_constraint_binding(
    path: Path, spec: Mapping[str, Any]
) -> None:
    try:
        constraints = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(
            "documented provider-neutral constraints are not valid JSON"
        ) from error
    if not isinstance(constraints, dict):
        raise ValidationError(
            "documented provider-neutral constraints must be an object"
        )
    digest = hashlib.sha256(_canonical(constraints)).hexdigest()
    experiment = spec["experiment"]
    if digest != experiment["constraints_sha256"]:
        raise ValidationError(
            "documented provider-neutral constraints disagree with the run spec"
        )
    if constraints.get("control_mode") != experiment["control_mode"]:
        raise ValidationError(
            "documented constraint control mode disagrees with the run spec"
        )
    actions = constraints.get("documented_actions")
    if (
        not isinstance(actions, list)
        or not all(isinstance(action, str) for action in actions)
        or sorted(actions) != experiment["documented_actions"]
    ):
        raise ValidationError(
            "documented constraint actions disagree with the run spec"
        )
    if constraints.get("seed") != spec["execution"]["seed"]:
        raise ValidationError(
            "documented constraint seed disagrees with the run spec"
        )


def _read_compilation_context(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise ValidationError("PPro compilation context does not exist")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError("PPro compilation context is not valid JSON") from error
    if not isinstance(value, dict) or set(value) != {
        "schema", "source_root", "include_dirs", "defines"
    }:
        raise ValidationError("PPro compilation context has invalid fields")
    if value["schema"] != PPRO_COMPILATION_CONTEXT_SCHEMA:
        raise ValidationError("PPro compilation context schema is invalid")
    if not isinstance(value["source_root"], str):
        raise ValidationError("PPro compilation source root is invalid")
    source_root = Path(value["source_root"])
    if not source_root.is_absolute() or not source_root.resolve().is_dir():
        raise ValidationError("PPro compilation source root is invalid")
    include_dirs = value["include_dirs"]
    defines = value["defines"]
    if (
        not isinstance(include_dirs, list)
        or not all(isinstance(item, str) and item for item in include_dirs)
        or len(include_dirs) != len(set(include_dirs))
    ):
        raise ValidationError("PPro compilation include_dirs are invalid")
    if (
        not isinstance(defines, list)
        or not all(
            isinstance(item, str) and YOSYS_DEFINE.fullmatch(item) is not None
            for item in defines
        )
        or len(defines) != len(set(defines))
    ):
        raise ValidationError("PPro compilation defines are invalid")
    resolved_include_dirs = []
    root = source_root.resolve()
    for raw_path in include_dirs:
        relative = Path(raw_path)
        if relative.is_absolute() or raw_path in {"", "."} or ".." in relative.parts:
            raise ValidationError("PPro compilation include path is not contained")
        include_dir = (root / relative).resolve()
        if root not in include_dir.parents or not include_dir.is_dir():
            raise ValidationError("PPro compilation include directory is invalid")
        if any(character.isspace() for character in str(include_dir)) or "+" in str(
            include_dir
        ):
            raise ValidationError(
                "PPro compilation include directory is not filelist-safe"
            )
        resolved_include_dirs.append(include_dir)
    return {
        "source_root": root,
        "include_dirs": resolved_include_dirs,
        "relative_include_dirs": include_dirs,
        "defines": defines,
    }


def _runtime_filelist(
    source: Path,
    destination: Path,
    *,
    compilation_context: Path | None = None,
    expected_rtl_sha256: str | None = None,
) -> None:
    """Materialize a runtime-only absolute filelist for generated probes.

    Calibration generators intentionally publish relative source names.  The
    ordinary PPro process runs in an isolated directory, so the runtime copy
    resolves those names without modifying or duplicating RTL contents.
    Filelists remain source-only. Application holdouts and compilation-context
    smoke probes may supply the separate strict compilation-context contract;
    this renderer, rather than the caller's filelist, emits the bounded
    include/define options.
    """

    if not source.is_file():
        raise ValidationError("PPro runtime filelist does not exist")
    source_paths = []
    for number, raw in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        value = raw.strip()
        if not value or value.startswith("#") or value.startswith("//"):
            continue
        if value.startswith(("-", "+")) or any(
            character.isspace() for character in value
        ):
            raise ValidationError(
                f"PPro runtime filelist line {number}: options and whitespace are not supported"
            )
        path = Path(value)
        if not path.is_absolute():
            path = source.parent / path
        path = path.resolve()
        if not path.is_file():
            raise ValidationError(
                f"PPro runtime filelist line {number}: source does not exist"
            )
        source_paths.append(path)
    if not source_paths:
        raise ValidationError("PPro runtime filelist is empty")
    lines: list[str] = []
    if compilation_context is not None:
        context = _read_compilation_context(compilation_context.resolve())
        root = context["source_root"]
        source_records = []
        for path in source_paths:
            if root not in path.parents:
                raise ValidationError("PPro application source escapes its source root")
            source_records.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": _sha256_file(path),
                    "size": path.stat().st_size,
                }
            )
        include_records: Dict[str, Dict[str, Any]] = {}
        for include_dir in context["include_dirs"]:
            for path in sorted(include_dir.rglob("*")):
                if path.is_symlink():
                    resolved = path.resolve()
                    if root not in resolved.parents or not resolved.is_file():
                        raise ValidationError(
                            "PPro application include file escapes its source root"
                        )
                if path.is_file():
                    relative = path.relative_to(root).as_posix()
                    include_records[relative] = {
                        "path": relative,
                        "sha256": _sha256_file(path),
                        "size": path.stat().st_size,
                    }
        rtl_inputs = {
            "source_records": source_records,
            "include_file_records": [
                include_records[name] for name in sorted(include_records)
            ],
            "include_dirs": context["relative_include_dirs"],
            "defines": context["defines"],
        }
        observed_rtl_sha256 = hashlib.sha256(_canonical(rtl_inputs)).hexdigest()
        if expected_rtl_sha256 != observed_rtl_sha256:
            raise ValidationError(
                "PPro application sources disagree with the sealed RTL identity"
            )
        lines.extend(f"+incdir+{path}" for path in context["include_dirs"])
        lines.extend(f"+define+{value}" for value in context["defines"])
    lines.extend(str(path) for path in source_paths)
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
    if not writable_root.is_dir():
        raise ValidationError("PPro authorized writable root does not exist")
    if not os.access(writable_root, os.W_OK | os.X_OK):
        raise ValidationError("PPro authorized writable root is not writable")
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
    targets = (
        validate_logical_targets(config.logical_targets)
        if config.logical_targets
        else {}
    )
    if not set(targets).issubset(set(logical)):
        raise ValidationError(
            "PPro logical placement targets must be covered by report aliases"
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
    compilation_context: Path | None = None,
    config: PProRuntimeConfig,
) -> RuntimeBinding:
    """Render a disposable PPro project and return its runtime binding."""

    spec = validate_run_spec(raw_spec)
    require_current_runner_revision(spec["tool"]["runner_revision"])
    if spec["adapter"]["profile"] != PPRO_2026_REPORT_PROFILE:
        raise ValidationError("PPro runtime renderer requires the real ordinary-report profile")
    experiment_kind = spec["experiment"]["kind"]
    requires_context = (
        experiment_kind == "application_holdout"
        or spec["workload"]["generator_id"] == "ppro-blackbox-connected-smoke-v3"
    )
    if requires_context and compilation_context is None:
        raise ValidationError(
            "PPro application holdouts and connected smoke v3 runs require a "
            "compilation context"
        )
    if compilation_context is not None and experiment_kind not in {
        "application_holdout",
        "reproducibility",
    }:
        raise ValidationError(
            "PPro compilation context is only valid for application holdouts "
            "and reproducibility probes"
        )
    _validate_config(config)
    _validate_constraint_binding(
        config.documented_constraints.resolve(), spec
    )

    case_dir = config.case_dir.resolve()
    case_dir.mkdir(parents=True, exist_ok=True)
    project_dir = case_dir / "project"
    temporary_dir = case_dir / ".tmp"
    runtime_filelist = case_dir / ".runtime-files.f"
    ppro_constraints = case_dir / ".prepartition.cfg"
    ssta_constraints = case_dir / ".ssta.sdc"
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
            ssta_constraints,
            tcl_path,
            launcher_path,
        )
    ):
        raise ValidationError("PPro case directory contains an active or stale runtime")
    if output_path.exists():
        raise ValidationError("PPro case already has an observation; use a new case directory")
    _runtime_filelist(
        source_filelist.resolve(),
        runtime_filelist,
        compilation_context=compilation_context,
        expected_rtl_sha256=(
            spec["workload"]["rtl_sha256"]
            if compilation_context is not None
            else None
        ),
    )
    render_ppro_prepartition_constraints(
        config.documented_constraints.resolve(),
        config.logical_targets,
        ppro_constraints,
    )
    if experiment_kind == "application_holdout":
        render_ppro_ssta_constraints(
            config.documented_constraints.resolve(),
            ssta_constraints,
        )
    temporary_dir.mkdir()
    runtime_home = temporary_dir / "home"
    runtime_home.mkdir()

    top = spec["workload"]["top_module"]
    # Application holdouts must ask the documented system-route interface to
    # perform timing budgeting and then materialize the partitioned RTL before
    # invoking post-partition SSTA.  A route-only ``run_ssta -state sr0`` run
    # can succeed while classifying every endpoint as a clockless false path;
    # that produces a syntactically valid timing-budget report but no usable
    # timing observation.  ``run_gen_rtl`` plus ``-post_partition`` is the
    # documented stage boundary for analyzing the generated FPGA designs.
    # Keep calibration microbenchmarks on their original route mode so this
    # qualification-only change cannot silently alter fitted parameters.
    if experiment_kind == "application_holdout":
        system_route_commands = (
            "run_system_route -timing_budget",
            f"run_gen_rtl -max_process_num {config.max_processes}",
            "run_ssta -post_partition -state sr0 -config "
            + _tcl_word(str(ssta_constraints), "PPro SSTA constraints"),
        )
    else:
        system_route_commands = ("run_system_route",)
    tcl = "\n".join(
        (
            "# Generated runtime-only PPro black-box calibration script.",
            "create_project -project_name {project} -project_path "
            + _tcl_word(str(case_dir), "PPro project parent directory")
            + " -force",
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
            *system_route_commands,
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
        ssta_constraints,
        tcl_path,
        launcher_path,
        case_dir / "runtime_Flag.tcl",
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
            "HOME": str(runtime_home),
            "TMPDIR": str(temporary_dir),
            "TMP": str(temporary_dir),
            "TEMP": str(temporary_dir),
        },
        fpga_aliases=dict(config.fpga_aliases),
        timeout_seconds=config.timeout_seconds,
        cleanup_raw_reports=not config.keep_raw_project,
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
