"""Mock-first queued runner and allowlisted report adapter for PPro calibration.

Runtime bindings intentionally are Python objects rather than serializable
artifacts: installation paths, license environment, concrete target names, and
raw report locations must never enter normalized observations or Git.
"""

from __future__ import annotations

import csv
import os
import re
import signal
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from .errors import ValidationError
from .io import write_json
from .ppro_blackbox_calibration import validate_blackbox_observation
from .ppro_blackbox_ppro_adapter import (
    PPRO_2026_REPORT_PROFILE,
    parse_ppro_2026_ordinary_reports,
)


RUN_SPEC_SCHEMA = "emuflow.ppro-blackbox-run-spec/v1"
MOCK_REPORT_PROFILE = "mock-ordinary-reports-v1"
SUPPORTED_REPORT_PROFILES = {MOCK_REPORT_PROFILE, PPRO_2026_REPORT_PROFILE}
_REPORTS = {
    "resource_summary",
    "partition_summary",
    "route_summary",
    "system_timing",
}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_HDL_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_LICENSE_PATTERNS = (
    re.compile(r"license[^\n]*(?:fail|denied|unavailable|checkout)", re.I),
    re.compile(r"flex(?:net|lm)[^\n]*(?:fail|denied|error)", re.I),
)
_INFRASTRUCTURE_PATTERNS = (
    re.compile(r"permission denied", re.I),
    re.compile(r"no such file or directory", re.I),
    re.compile(r"connection (?:timed out|refused|reset)", re.I),
    re.compile(r"host (?:is )?unreachable", re.I),
)
_CAPACITY_BOUNDARY_PATTERNS = (
    # PPro's ordinary console diagnostic for a resource-constrained partition
    # boundary.  Keep this deliberately narrow: generic "partition failed"
    # text is not evidence that the modeled hardware capacity was exceeded.
    re.compile(
        r"cannot\s+be\s+placed\s+on\s+any\s+FPGA\s+because\s+of\s+"
        r"\[(?:LUT|FF|BRAM|DSP|URAM)\]",
        re.I,
    ),
)
_TIMING_PATTERNS = {
    "sr0_worst_cross_fpga_delay_ns": re.compile(
        r"^Worst Cross FPGA Delay \(ns\):\s*([0-9]+(?:\.[0-9]+)?)\s*$",
        re.I | re.M,
    ),
    "cross_fpga_path_count": re.compile(
        r"^Cross FPGA Path Count:\s*([0-9]+)\s*$", re.I | re.M
    ),
    "maximum_tdm_ratio": re.compile(
        r"^Maximum TDM Ratio:\s*([0-9]+)\s*$", re.I | re.M
    ),
}


@dataclass(frozen=True)
class RuntimeBinding:
    """Non-serializable execution bindings for one isolated case."""

    case_dir: Path
    command: tuple[str, ...]
    report_paths: Mapping[str, Path]
    output_path: Path
    environment: Mapping[str, str] = field(default_factory=dict)
    fpga_aliases: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 3600.0
    cleanup_raw_reports: bool = True
    cleanup_paths: tuple[Path, ...] = ()
    retain_failure_diagnostics: bool = False


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{context}: expected an object")
    return value


def _strict_keys(
    value: Mapping[str, Any], required: set[str], optional: set[str], context: str
) -> None:
    missing = required - set(value)
    unknown = set(value) - required - optional
    if missing:
        raise ValidationError(f"{context}: missing fields {sorted(missing)}")
    if unknown:
        raise ValidationError(f"{context}: unknown fields {sorted(unknown)}")


def _identifier(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValidationError(f"{context}: invalid stable identifier")
    return value


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{context}: expected a non-empty string")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValidationError(f"{context}: multiline or NUL text is forbidden")
    return value.strip()


def _sha256(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise ValidationError(f"{context}: expected lowercase SHA-256")
    return value


def _hdl_identifier(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _HDL_ID_RE.fullmatch(value):
        raise ValidationError(f"{context}: invalid HDL identifier")
    return value


def _nonnegative_metrics(value: Any, context: str) -> Dict[str, float]:
    source = _mapping(value, context)
    result: Dict[str, float] = {}
    for raw_name, raw_amount in sorted(source.items()):
        name = _identifier(raw_name, f"{context} key")
        if isinstance(raw_amount, bool) or not isinstance(raw_amount, (int, float)):
            raise ValidationError(f"{context}.{name}: expected a number")
        amount = float(raw_amount)
        if amount < 0.0 or amount != amount or amount == float("inf"):
            raise ValidationError(f"{context}.{name}: expected a finite non-negative number")
        result[name] = amount
    return result


def validate_run_spec(value: Mapping[str, Any]) -> Dict[str, Any]:
    root = _mapping(value, "run spec")
    _strict_keys(
        root,
        {
            "schema",
            "identity",
            "tool",
            "workload",
            "experiment",
            "execution",
            "adapter",
        },
        set(),
        "run spec",
    )
    if root.get("schema") != RUN_SPEC_SCHEMA:
        raise ValidationError(f"run spec.schema: expected {RUN_SPEC_SCHEMA!r}")

    identity = _mapping(root["identity"], "run spec.identity")
    _strict_keys(
        identity,
        {"id", "campaign_id", "case_id", "role", "public_prior_id", "configuration_id"},
        set(),
        "run spec.identity",
    )
    role = _string(identity["role"], "run spec.identity.role")
    if role not in {"fit", "holdout"}:
        raise ValidationError("run spec.identity.role: expected fit or holdout")
    normalized_identity = {
        field: _identifier(identity[field], f"run spec.identity.{field}")
        for field in (
            "id",
            "campaign_id",
            "case_id",
            "public_prior_id",
            "configuration_id",
        )
    }
    normalized_identity["role"] = role

    tool = _mapping(root["tool"], "run spec.tool")
    _strict_keys(tool, {"name", "release", "runner_revision"}, set(), "run spec.tool")
    normalized_tool = {
        "name": _string(tool["name"], "run spec.tool.name"),
        "release": _string(tool["release"], "run spec.tool.release"),
        "runner_revision": _sha256(tool["runner_revision"], "run spec.tool.runner_revision"),
    }

    workload = _mapping(root["workload"], "run spec.workload")
    _strict_keys(
        workload,
        {
            "generator_id",
            "generator_revision",
            "rtl_sha256",
            "parameters_sha256",
            "top_module",
            "design_metrics",
        },
        set(),
        "run spec.workload",
    )
    normalized_workload = {
        "generator_id": _identifier(workload["generator_id"], "run spec.workload.generator_id"),
        "generator_revision": _sha256(
            workload["generator_revision"], "run spec.workload.generator_revision"
        ),
        "rtl_sha256": _sha256(workload["rtl_sha256"], "run spec.workload.rtl_sha256"),
        "parameters_sha256": _sha256(
            workload["parameters_sha256"], "run spec.workload.parameters_sha256"
        ),
        "top_module": _hdl_identifier(
            workload["top_module"], "run spec.workload.top_module"
        ),
        "design_metrics": _nonnegative_metrics(
            workload["design_metrics"], "run spec.workload.design_metrics"
        ),
    }
    if not normalized_workload["design_metrics"]:
        raise ValidationError("run spec.workload.design_metrics: expected non-empty metrics")

    experiment = _mapping(root["experiment"], "run spec.experiment")
    _strict_keys(
        experiment,
        {"kind", "control_mode", "documented_actions", "constraints_sha256"},
        set(),
        "run spec.experiment",
    )
    actions = experiment["documented_actions"]
    if not isinstance(actions, list):
        raise ValidationError("run spec.experiment.documented_actions: expected an array")
    normalized_experiment = {
        "kind": _string(experiment["kind"], "run spec.experiment.kind"),
        "control_mode": _string(
            experiment["control_mode"], "run spec.experiment.control_mode"
        ),
        "documented_actions": sorted(
            _string(action, "run spec.experiment.documented_actions") for action in actions
        ),
        "constraints_sha256": _sha256(
            experiment["constraints_sha256"], "run spec.experiment.constraints_sha256"
        ),
    }

    execution = _mapping(root["execution"], "run spec.execution")
    _strict_keys(execution, {"seed"}, set(), "run spec.execution")
    seed = execution["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValidationError("run spec.execution.seed: expected an integer >= 0")

    adapter = _mapping(root["adapter"], "run spec.adapter")
    _strict_keys(adapter, {"profile", "expected_reports"}, set(), "run spec.adapter")
    profile = _string(adapter["profile"], "run spec.adapter.profile")
    if profile not in SUPPORTED_REPORT_PROFILES:
        raise ValidationError("run spec.adapter.profile: unsupported report profile")
    expected_reports = adapter["expected_reports"]
    if not isinstance(expected_reports, list):
        raise ValidationError("run spec.adapter.expected_reports: expected an array")
    expected = sorted(_string(item, "run spec.adapter.expected_reports") for item in expected_reports)
    if set(expected) != _REPORTS or len(expected) != len(set(expected)):
        raise ValidationError("run spec.adapter.expected_reports: expected each allowlisted report once")

    normalized = {
        "schema": RUN_SPEC_SCHEMA,
        "identity": normalized_identity,
        "tool": normalized_tool,
        "workload": normalized_workload,
        "experiment": normalized_experiment,
        "execution": {"seed": seed},
        "adapter": {"profile": profile, "expected_reports": expected},
    }
    # Reuse the observation validator to enforce experiment enums/actions.
    probe = _failure_observation(normalized, "tool_failure", "run-spec-probe", None)
    validate_blackbox_observation(probe)
    return normalized


def validate_runtime_binding(binding: RuntimeBinding, *, profile: str = MOCK_REPORT_PROFILE) -> None:
    if not binding.command or any(not isinstance(item, str) or not item for item in binding.command):
        raise ValidationError("runtime binding command must be a non-empty argv tuple")
    if set(binding.report_paths) != _REPORTS:
        raise ValidationError("runtime binding must name each allowlisted report exactly once")
    if binding.timeout_seconds <= 0:
        raise ValidationError("runtime binding timeout must be positive")
    if type(binding.retain_failure_diagnostics) is not bool:
        raise ValidationError("runtime diagnostic-retention flag must be boolean")
    case_dir = binding.case_dir.resolve()
    for name, raw_path in binding.report_paths.items():
        path = raw_path.resolve()
        if not path.is_relative_to(case_dir):
            raise ValidationError(f"runtime report {name} must stay inside the isolated case directory")
    if binding.output_path.resolve().parent != case_dir:
        raise ValidationError("runtime observation must be directly inside the isolated case directory")
    output_path = binding.output_path.resolve()
    for raw_path in binding.cleanup_paths:
        path = raw_path.resolve()
        if path == case_dir or not path.is_relative_to(case_dir):
            raise ValidationError("runtime cleanup paths must stay below the isolated case directory")
        if path == output_path:
            raise ValidationError("runtime cleanup paths cannot remove the compact observation")
    if profile == PPRO_2026_REPORT_PROFILE:
        if not binding.fpga_aliases:
            raise ValidationError("real PPro runtime binding requires FPGA aliases")
    elif binding.fpga_aliases:
        raise ValidationError("mock runtime binding must not provide physical FPGA aliases")


def _read_csv(path: Path, required: set[str]) -> list[Dict[str, str]]:
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValidationError(f"ordinary report {path.name}: exceeds the 64 MiB parser bound")
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or set(reader.fieldnames) != required:
            raise ValidationError(
                f"ordinary report {path.name}: expected exact columns {sorted(required)}"
            )
        rows = [dict(row) for row in reader]
    if not rows:
        raise ValidationError(f"ordinary report {path.name}: expected at least one row")
    return rows


def _parse_nonnegative(text: str, context: str) -> float:
    try:
        value = float(text)
    except ValueError as error:
        raise ValidationError(f"{context}: expected a number") from error
    if value < 0.0 or value != value or value == float("inf"):
        raise ValidationError(f"{context}: expected a finite non-negative number")
    return value


def _parse_nonnegative_integer(text: str, context: str, *, minimum: int = 0) -> int:
    if not re.fullmatch(r"[0-9]+", text.strip()):
        raise ValidationError(f"{context}: expected an integer")
    value = int(text)
    if value < minimum:
        raise ValidationError(f"{context}: expected an integer >= {minimum}")
    return value


def parse_mock_ordinary_reports(
    report_paths: Mapping[str, Path], design_metrics: Mapping[str, float]
) -> Dict[str, Any]:
    """Parse only the four explicitly named synthetic ordinary reports."""
    if set(report_paths) != _REPORTS:
        raise ValidationError("report adapter received an incomplete allowlist")
    for name, path in report_paths.items():
        if not path.is_file():
            raise ValidationError(f"missing ordinary report: {name}")

    resource_rows = _read_csv(report_paths["resource_summary"], {"resource", "demand"})
    resource_demand: Dict[str, float] = {}
    for row in resource_rows:
        resource = _identifier(row["resource"], "resource summary resource")
        if resource in resource_demand:
            raise ValidationError("resource summary contains duplicate resource")
        resource_demand[resource] = _parse_nonnegative(
            row["demand"], f"resource summary {resource}"
        )

    partition_columns = {
        "partition",
        "fpga",
        "lut_utilization",
        "ff_utilization",
        "bram_utilization",
        "dsp_utilization",
    }
    partition_rows = _read_csv(report_paths["partition_summary"], partition_columns)
    assignments = []
    fpga_resources: Dict[str, Dict[str, float]] = {}
    for row in partition_rows:
        partition = row["partition"]
        fpga = row["fpga"]
        if not re.fullmatch(r"P[0-9]+", partition) or not re.fullmatch(r"F[0-9]+", fpga):
            raise ValidationError("partition summary must use vendor-neutral P<n>/F<n> aliases")
        assignments.append({"partition": partition, "fpga": fpga})
        resources = {
            name: _parse_nonnegative(row[f"{name}_utilization"], f"{fpga} {name} utilization")
            for name in ("lut", "ff", "bram", "dsp")
        }
        if any(value > 1.0 for value in resources.values()):
            raise ValidationError("partition summary utilization exceeds one")
        if fpga in fpga_resources and fpga_resources[fpga] != resources:
            raise ValidationError("partition summary disagrees on FPGA utilization")
        fpga_resources[fpga] = resources

    route_rows = _read_csv(
        report_paths["route_summary"],
        {"route", "source", "sinks", "effective_hops", "path_count", "maximum_tdm_ratio"},
    )
    routes = []
    total_paths = 0
    maximum_tdm_ratio = 0
    for row in route_rows:
        sinks = [item for item in row["sinks"].split(";") if item]
        routes.append(
            {
                "id": _identifier(row["route"], "route summary route"),
                "source": row["source"],
                "sinks": sinks,
                "effective_hops": _parse_nonnegative_integer(
                    row["effective_hops"], "route hops", minimum=1
                ),
                "signal_count": _parse_nonnegative_integer(
                    row["path_count"], "route path count", minimum=1
                ),
            }
        )
        total_paths += routes[-1]["signal_count"]
        maximum_tdm_ratio = max(
            maximum_tdm_ratio,
            _parse_nonnegative_integer(
                row["maximum_tdm_ratio"], "route maximum TDM ratio", minimum=1
            ),
        )

    if report_paths["system_timing"].stat().st_size > 16 * 1024 * 1024:
        raise ValidationError("ordinary report system_timing: exceeds the 16 MiB parser bound")
    timing_text = report_paths["system_timing"].read_text(encoding="utf-8")
    timing_values: Dict[str, float] = {}
    for name, pattern in _TIMING_PATTERNS.items():
        matches = pattern.findall(timing_text)
        if len(matches) != 1:
            raise ValidationError(f"system timing report: expected exactly one {name}")
        timing_values[name] = _parse_nonnegative(matches[0], f"system timing {name}")
    if int(timing_values["cross_fpga_path_count"]) != total_paths:
        raise ValidationError("route and system timing reports disagree on cross-FPGA path count")
    if int(timing_values["maximum_tdm_ratio"]) != maximum_tdm_ratio:
        raise ValidationError("route and system timing reports disagree on maximum TDM ratio")

    return {
        "design": dict(design_metrics),
        "resource_demand": resource_demand,
        "fpga_utilization": [
            {"fpga": fpga, "resources": resources}
            for fpga, resources in sorted(fpga_resources.items())
        ],
        "assignments": assignments,
        "routes": routes,
        "communication": {
            "cross_fpga_path_count": float(total_paths),
            "maximum_tdm_ratio": float(maximum_tdm_ratio),
            "route_count": float(len(routes)),
        },
        "timing": {
            "sr0_worst_cross_fpga_delay_ns": timing_values[
                "sr0_worst_cross_fpga_delay_ns"
            ]
        },
    }


def classify_process_failure(return_code: int, diagnostic_tail: str) -> tuple[str, str]:
    if any(pattern.search(diagnostic_tail) for pattern in _LICENSE_PATTERNS):
        return "license_failure", "license-unavailable"
    if any(pattern.search(diagnostic_tail) for pattern in _INFRASTRUCTURE_PATTERNS):
        return "infrastructure_failure", "execution-environment"
    if any(pattern.search(diagnostic_tail) for pattern in _CAPACITY_BOUNDARY_PATTERNS):
        return "capacity_infeasible", "capacity-boundary"
    return "tool_failure", f"tool-exit-{abs(return_code)}"


def _read_text_tail(path: Path, maximum_bytes: int = 16384) -> str:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - maximum_bytes), os.SEEK_SET)
        return stream.read(maximum_bytes).decode("utf-8", errors="replace")


def _empty_metrics() -> Dict[str, Any]:
    return {
        "design": {},
        "resource_demand": {},
        "fpga_utilization": [],
        "assignments": [],
        "routes": [],
        "communication": {},
        "timing": {},
    }


def _failure_observation(
    spec: Mapping[str, Any], outcome: str, failure_code: str, runtime_seconds: float | None
) -> Dict[str, Any]:
    role = spec["identity"]["role"]
    controlled = spec["experiment"]["control_mode"] in {
        "fixed_assignment",
        "fixed_communication",
    }
    evaluated = outcome in {
        "capacity_infeasible",
        "link_capacity_infeasible",
        "routing_infeasible",
    }
    fit_eligible = role == "fit" and controlled and evaluated
    reason = (
        "controlled-evaluated-observation"
        if fit_eligible
        else "holdout-not-fit"
        if role == "holdout"
        else "uncontrolled-not-fit"
        if not controlled
        else "non-hardware-failure"
    )
    return {
        "schema": "emuflow.ppro-blackbox-observation/v1",
        "identity": dict(spec["identity"]),
        "tool": dict(spec["tool"]),
        "workload": {
            key: spec["workload"][key]
            for key in (
                "generator_id",
                "generator_revision",
                "rtl_sha256",
                "parameters_sha256",
                "top_module",
            )
        },
        "experiment": dict(spec["experiment"]),
        "execution": {
            "seed": spec["execution"]["seed"],
            "outcome": outcome,
            "failure_code": failure_code,
            "runtime_seconds": runtime_seconds,
        },
        "reports": {name: False for name in sorted(_REPORTS)},
        "metrics": {
            **_empty_metrics(),
            # Boundary observations have no ordinary success reports, but the
            # controlled input coordinates remain valid fitting evidence.
            "design": dict(spec["workload"]["design_metrics"]) if evaluated else {},
        },
        "provenance": {"class": "black_box_observation"},
        "derived": {"fit_eligible": fit_eligible, "reason": reason},
    }


def cleanup_runtime_artifacts(binding: RuntimeBinding) -> None:
    """Remove allowlisted raw reports and explicitly registered runtime scratch."""

    if binding.cleanup_raw_reports:
        for path in binding.report_paths.values():
            path.unlink(missing_ok=True)
    for path in sorted(
        {item.resolve() for item in binding.cleanup_paths},
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate the isolated provider process group and wait for collection."""

    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait(timeout=5.0)


def execute_blackbox_case(
    raw_spec: Mapping[str, Any], binding: RuntimeBinding
) -> Dict[str, Any]:
    spec = validate_run_spec(raw_spec)
    validate_runtime_binding(binding, profile=spec["adapter"]["profile"])
    binding.case_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = binding.case_dir / ".runner-stdout.log"
    stderr_path = binding.case_dir / ".runner-stderr.log"
    started = time.monotonic()
    timed_out = False
    return_code: int | None = None
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                list(binding.command),
                cwd=binding.case_dir,
                env={**os.environ, **binding.environment},
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            try:
                return_code = process.wait(timeout=binding.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_group(process)
        runtime_seconds = time.monotonic() - started
    except OSError:
        runtime_seconds = time.monotonic() - started
        observation = _failure_observation(
            spec, "infrastructure_failure", "execution-environment", runtime_seconds
        )
    else:
        if timed_out:
            observation = _failure_observation(
                spec, "infrastructure_failure", "execution-timeout", runtime_seconds
            )
        elif return_code is None:
            raise AssertionError("PPro provider process ended without a return code")
        elif return_code != 0:
            diagnostic_tail = (
                _read_text_tail(stdout_path) + "\n" + _read_text_tail(stderr_path)
            )
            outcome, failure_code = classify_process_failure(return_code, diagnostic_tail)
            observation = _failure_observation(spec, outcome, failure_code, runtime_seconds)
        elif any(not path.is_file() for path in binding.report_paths.values()):
            diagnostic_tail = (
                _read_text_tail(stdout_path) + "\n" + _read_text_tail(stderr_path)
            )
            outcome, failure_code = classify_process_failure(0, diagnostic_tail)
            if outcome == "tool_failure":
                outcome, failure_code = "missing_report", "ordinary-report-missing"
            observation = _failure_observation(spec, outcome, failure_code, runtime_seconds)
        else:
            try:
                if spec["adapter"]["profile"] == MOCK_REPORT_PROFILE:
                    metrics = parse_mock_ordinary_reports(
                        binding.report_paths, spec["workload"]["design_metrics"]
                    )
                else:
                    metrics = parse_ppro_2026_ordinary_reports(
                        binding.report_paths,
                        spec["workload"]["design_metrics"],
                        binding.fpga_aliases,
                    )
            except (OSError, UnicodeError, ValidationError):
                observation = _failure_observation(
                    spec,
                    "report_parse_failure",
                    "ordinary-report-invalid",
                    runtime_seconds,
                )
            else:
                controlled = spec["experiment"]["control_mode"] in {
                    "fixed_assignment",
                    "fixed_communication",
                }
                fit_eligible = spec["identity"]["role"] == "fit" and controlled
                reason = (
                    "controlled-evaluated-observation"
                    if fit_eligible
                    else "holdout-not-fit"
                    if spec["identity"]["role"] == "holdout"
                    else "uncontrolled-not-fit"
                )
                observation = {
                    "schema": "emuflow.ppro-blackbox-observation/v1",
                    "identity": dict(spec["identity"]),
                    "tool": dict(spec["tool"]),
                    "workload": {
                        key: spec["workload"][key]
                        for key in (
                            "generator_id",
                            "generator_revision",
                            "rtl_sha256",
                            "parameters_sha256",
                            "top_module",
                        )
                    },
                    "experiment": dict(spec["experiment"]),
                    "execution": {
                        "seed": spec["execution"]["seed"],
                        "outcome": "pass",
                        "failure_code": None,
                        "runtime_seconds": runtime_seconds,
                    },
                    "reports": {name: True for name in sorted(_REPORTS)},
                    "metrics": metrics,
                    "provenance": {"class": "black_box_observation"},
                    "derived": {"fit_eligible": fit_eligible, "reason": reason},
                }
    normalized = validate_blackbox_observation(observation)
    write_json(binding.output_path, normalized, compact=True)
    if binding.retain_failure_diagnostics and normalized["execution"]["outcome"] != "pass":
        for path in (stdout_path, stderr_path):
            path.write_text(_read_text_tail(path), encoding="utf-8")
    else:
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)
    cleanup_runtime_artifacts(binding)
    return normalized


def execute_blackbox_queue(
    cases: Sequence[tuple[Mapping[str, Any], RuntimeBinding]], *, max_workers: int
) -> list[Dict[str, Any]]:
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers <= 0:
        raise ValidationError("black-box queue max_workers must be a positive integer")
    if max_workers > len(cases) and cases:
        max_workers = len(cases)
    if not cases:
        return []
    results: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(execute_blackbox_case, spec, binding): spec["identity"]["id"]
            for spec, binding in cases
        }
        for future in as_completed(futures):
            observation = future.result()
            results[observation["identity"]["id"]] = observation
    return [results[name] for name in sorted(results)]
