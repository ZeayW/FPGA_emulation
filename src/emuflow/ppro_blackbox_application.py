"""Scratch-only PPro application holdout bundles from benchmark contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from .benchmark import BenchmarkRun
from .errors import ValidationError
from .io import write_json
from .ppro_blackbox_ppro_adapter import PPRO_2026_REPORT_PROFILE
from .ppro_blackbox_runner import RUN_SPEC_SCHEMA, validate_run_spec


_GENERATOR_ID = "ppro-blackbox-application-holdout-v1"
_GENERATOR_REVISION = hashlib.sha256(_GENERATOR_ID.encode("utf-8")).hexdigest()
_EXPECTED_REPORTS = [
    "partition_summary",
    "resource_summary",
]


@dataclass(frozen=True)
class ApplicationHoldoutBundle:
    root: Path
    filelist_path: Path
    compilation_context_path: Path
    constraints_path: Path
    run_spec_path: Path
    run_spec: Dict[str, Any]


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _include_file_records(root: Path, include_dirs: list[Path]) -> list[Dict[str, Any]]:
    records: Dict[str, Dict[str, Any]] = {}
    for include_dir in include_dirs:
        for path in sorted(include_dir.rglob("*")):
            if path.is_symlink():
                resolved = path.resolve()
                if root not in resolved.parents or not resolved.is_file():
                    raise ValidationError(
                        "benchmark include file escapes its source root"
                    )
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            records[relative] = {
                "path": relative,
                "sha256": _sha256_file(path),
                "size": path.stat().st_size,
            }
    return [records[name] for name in sorted(records)]


def benchmark_rtl_identity(
    benchmark_run_path: Path, source_root: Path
) -> Dict[str, Any]:
    """Return the canonical RTL identity shared by PPro and EmuFlow.

    The identity is deliberately derived from the checked benchmark contract,
    ordered relative source names and bytes, every file in the ordered include
    search path, preprocessor defines, and top module.  A blind-result assembler
    can therefore prove that both tools consumed the same natural RTL without
    trusting a caller-supplied digest.
    """

    benchmark_path = benchmark_run_path.resolve()
    root = source_root.resolve()
    benchmark = BenchmarkRun.load(benchmark_path)
    sources = benchmark.resolve_sources(root)
    include_dirs = benchmark.resolve_include_dirs(root)
    compilation_context = benchmark.compilation_context(root)
    relative_records = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": _sha256_file(path),
            "size": path.stat().st_size,
        }
        for path in sources
    ]
    include_file_records = _include_file_records(root, include_dirs)
    rtl_inputs = {
        "source_records": relative_records,
        "include_file_records": include_file_records,
        "include_dirs": compilation_context["include_dirs"],
        "defines": compilation_context["defines"],
    }
    parameters = {
        "benchmark_id": benchmark.value["id"],
        "calibration_holdout_class": benchmark.value.get(
            "calibration_holdout_class"
        ),
        "clocks": benchmark.value["clocks"],
        "clock_periods_ns": benchmark.value.get("clock_periods_ns"),
        "rtl_inputs": rtl_inputs,
        "top": benchmark.value["top"],
    }
    return {
        "benchmark_id": benchmark.value["id"],
        "benchmark_run": benchmark_path,
        "benchmark_run_sha256": _sha256_file(benchmark_path),
        "source_root": root,
        "sources": sources,
        "source_records": relative_records,
        "include_dirs": include_dirs,
        "include_file_records": include_file_records,
        "defines": compilation_context["defines"],
        "compilation_context": compilation_context,
        "rtl_sha256": hashlib.sha256(_canonical(rtl_inputs)).hexdigest(),
        "parameters_sha256": hashlib.sha256(_canonical(parameters)).hexdigest(),
        "top_module": benchmark.value["top"],
        "clocks": benchmark.value["clocks"],
        "clock_periods_ns": benchmark.value.get("clock_periods_ns"),
        "calibration_holdout_class": benchmark.value.get(
            "calibration_holdout_class"
        ),
        "preparation": benchmark.value.get("preparation"),
    }


def generate_application_holdout_bundle(
    output_dir: Path,
    *,
    benchmark_run_path: Path,
    source_root: Path,
    campaign_id: str,
    public_prior_id: str,
    configuration_id: str,
    tool_release: str,
    runner_revision: str,
    seed: int = 1,
) -> ApplicationHoldoutBundle:
    """Bind a checked benchmark contract to a free-partition PPro holdout run."""

    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValidationError("application holdout seed must be a nonnegative integer")
    identity = benchmark_rtl_identity(benchmark_run_path, source_root)
    if identity["calibration_holdout_class"] is None:
        raise ValidationError(
            "application holdout benchmark contract lacks "
            "calibration_holdout_class"
        )
    benchmark = BenchmarkRun.load(identity["benchmark_run"])
    sources = identity["sources"]
    relative_records = identity["source_records"]
    rtl_sha256 = identity["rtl_sha256"]
    clock_periods = identity["clock_periods_ns"]
    if not isinstance(clock_periods, dict) or set(clock_periods) != set(
        identity["clocks"]
    ):
        raise ValidationError(
            "application holdout benchmark must define a period for every clock"
        )
    constraints = {
        "control_mode": "none",
        "documented_actions": [],
        "seed": seed,
        "timing_clocks": [
            {"port": clock, "period_ns": float(clock_periods[clock])}
            for clock in identity["clocks"]
        ],
    }

    root = output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    filelist_path = root / "sources.f"
    constraints_path = root / "documented_constraints.json"
    compilation_context_path = root / "compilation-context.json"
    run_spec_path = root / "run-spec.json"
    # This filelist is active-run scratch. Its paths are consumed by the runtime
    # renderer and never enter the normalized observation.
    filelist_path.write_text(
        "".join(str(path.resolve()) + "\n" for path in sources), encoding="utf-8"
    )
    write_json(
        compilation_context_path,
        {
            "schema": "emuflow.ppro-compilation-context/v1",
            "source_root": str(identity["source_root"]),
            "include_dirs": identity["compilation_context"]["include_dirs"],
            "defines": identity["defines"],
        },
        compact=True,
    )
    write_json(constraints_path, constraints, compact=True)
    case_id = f"application-{benchmark.value['id']}-s{seed}"
    spec = {
        "schema": RUN_SPEC_SCHEMA,
        "identity": {
            "id": f"{campaign_id}.{case_id}",
            "campaign_id": campaign_id,
            "case_id": case_id,
            "role": "holdout",
            "public_prior_id": public_prior_id,
            "configuration_id": configuration_id,
        },
        "tool": {
            "name": "PPro",
            "release": tool_release,
            "runner_revision": runner_revision,
        },
        "workload": {
            "generator_id": _GENERATOR_ID,
            "generator_revision": _GENERATOR_REVISION,
            "rtl_sha256": rtl_sha256,
            "parameters_sha256": identity["parameters_sha256"],
            "top_module": benchmark.value["top"],
            "design_metrics": {
                "clock_count": len(benchmark.value["clocks"]),
                "source_bytes": sum(record["size"] for record in relative_records),
                "source_file_count": len(relative_records),
                "include_file_bytes": sum(
                    record["size"] for record in identity["include_file_records"]
                ),
                "include_file_count": len(identity["include_file_records"]),
            },
        },
        "experiment": {
            "kind": "application_holdout",
            "control_mode": "none",
            "documented_actions": [],
            "constraints_sha256": hashlib.sha256(_canonical(constraints)).hexdigest(),
        },
        "execution": {"seed": seed},
        "adapter": {
            "profile": PPRO_2026_REPORT_PROFILE,
            "expected_reports": _EXPECTED_REPORTS,
        },
    }
    normalized = validate_run_spec(spec)
    write_json(run_spec_path, normalized, compact=True)
    return ApplicationHoldoutBundle(
        root=root,
        filelist_path=filelist_path,
        compilation_context_path=compilation_context_path,
        constraints_path=constraints_path,
        run_spec_path=run_spec_path,
        run_spec=normalized,
    )
