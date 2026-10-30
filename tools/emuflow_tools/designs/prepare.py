"""Prepare sealed design inputs without adding another flow controller."""
from __future__ import annotations

import fcntl
import hashlib
from pathlib import Path

from emuflow.common.errors import EmuFlowError
from emuflow.common.json_io import file_sha256, write_json
from emuflow.flow.benchmark_inputs import BenchmarkRun
from emuflow.flow.environment import load_environment, read_control, resolve_path, resolve_environment_path
from emuflow.flow.run_config import (
    SCHEMA, _canonical, _compile_options, _require_physical_design,
    source_digest, validate_nvdla_source,
)
from emuflow.platform.default_platform import DEFAULT_PLATFORM_PATH

DESIGN_SCHEMA = "emuflow.design/v1"

NVDLA_OPTIONS = {
    "partition_provider": "patron",
    "patron_algorithm_version": 14,
    "cut_mode": "static-exact-combinational",
    "max_cross_fpga_dependency_depth": 8,
    "patron_max_moves": 64,
    "min_used_fpgas": 4,
    "balance_tolerance": 2.1,
    "partition_timeout_seconds": 10800,
    "frame_slots": 1024,
    "physical_seed": 1,
    "physical_workers": 1,
    "phase6_provider": "baseline",
}

def prepare_design_run(design_path, output_path, *, environment_path=None):
    """Seal user RTL and constraints without requiring a benchmark preset."""
    path, value = read_control(design_path)
    required = {
        "schema",
        "name",
        "source_root",
        "sources",
        "top",
        "clock_periods_ns",
        "timing_io",
    }
    allowed = required | {"include_dirs", "defines", "platform", "options"}
    if value.get("schema") != DESIGN_SCHEMA or not required <= set(value) <= allowed:
        raise EmuFlowError("unsupported design configuration schema or fields")
    environment_path = resolve_environment_path(environment_path)
    profile = load_environment(environment_path)
    source_root = resolve_path(path.parent, value["source_root"])
    platform = (
        resolve_path(path.parent, value["platform"])
        if "platform" in value
        else DEFAULT_PLATFORM_PATH.resolve()
    )
    from emuflow.platform.platform import Platform

    Platform.load(platform)
    periods = value["clock_periods_ns"]
    if type(periods) is not dict or not periods:
        raise EmuFlowError("clock_periods_ns requires a period for every design clock")
    benchmark = BenchmarkRun(
        {
            "schema": "emuflow.benchmark-run/v1",
            "id": value["name"],
            "design_id": value["name"],
            "top": value["top"],
            "sources": value["sources"],
            "clocks": list(periods),
            "clock_periods_ns": periods,
            "timing_io": value["timing_io"],
            "platform": str(platform),
            "physical_mapping_profile": "xilinx-ultrascaleplus-open-v1",
            "synthesis": {
                "family": "xcup",
                "policy": "native",
                "include_dirs": value.get("include_dirs", []),
                "defines": value.get("defines", []),
            },
        }
    )
    _require_physical_design(benchmark)
    if value["name"] == "nvdla":
        validate_nvdla_source(benchmark, source_root)
    options = _compile_options(value.get("options", {}), path.parent)
    return _publish_run(
        benchmark,
        source_root,
        platform,
        environment_path,
        output_path,
        options,
        profile,
    )

def _publish_run(
    benchmark, source_root, platform, environment_path, output_path, options, profile
):
    output = Path(output_path).expanduser().resolve()
    benchmark_path = output.with_name(output.stem + ".benchmark.json")
    if source_root == output or source_root in output.parents:
        raise EmuFlowError("run configuration must be outside the source tree")
    if output.exists() or benchmark_path.exists():
        raise EmuFlowError("design preparation requires new run/benchmark paths")
    source_sha256 = source_digest(benchmark, source_root)
    write_json(benchmark_path, benchmark.value)
    write_json(
        output,
        {
            "schema": SCHEMA,
            "environment": str(environment_path),
            "benchmark_run": str(benchmark_path),
            "benchmark_sha256": file_sha256(benchmark_path),
            "source_root": str(source_root),
            "platform": str(platform),
            "platform_sha256": file_sha256(platform),
            "source_sha256": source_sha256,
            "options": {
                key: str(item) if isinstance(item, Path) else item
                for key, item in options.items()
            },
        },
    )
    return {
        "status": "pass",
        "scope": "design-preparation",
        "terminal_qualification": False,
        "run_config": str(output),
        "top": benchmark.value["top"],
        "source_files": len(benchmark.resolve_sources(source_root)),
        "backends": sorted(profile["backends"]),
    }

def prepare_nvdla_run(
    source_root,
    output_path,
    *,
    platform_path=DEFAULT_PLATFORM_PATH,
    environment_path=None,
):
    from emuflow_tools.designs.nvdla import prepare_nvdla_holdout
    from emuflow_tools.designs.nvdla_memory import PHYSICAL_MEMORY_POLICY

    source_root = Path(source_root).expanduser().resolve()
    environment_path = resolve_environment_path(environment_path)
    profile = load_environment(environment_path)
    platform_path = Path(platform_path).expanduser().resolve()
    from emuflow.platform.platform import Platform

    if len(Platform.load(platform_path).fpgas) < NVDLA_OPTIONS["min_used_fpgas"]:
        raise EmuFlowError("the NVDLA preset requires a four-FPGA platform")
    output_path = Path(output_path).expanduser().resolve()
    if source_root == output_path or source_root in output_path.parents:
        raise EmuFlowError("run configuration must be outside the pinned source tree")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    benchmark_path = output_path.with_name(output_path.stem + ".benchmark.json")
    if output_path.exists() or benchmark_path.exists():
        raise EmuFlowError("design preparation requires new run/benchmark paths")
    # Source-owned overlays are immutable once published. Re-preparing another
    # run reuses a checked generation and cannot rewrite an active compile's RTL.
    from emuflow_tools.designs import nvdla as generator
    from emuflow_tools.document_io import read_json
    from emuflow_tools.designs.nvdla_memory import __file__ as memory_source

    seed = [
        file_sha256(Path(generator.__file__)),
        file_sha256(Path(memory_source)),
        file_sha256(generator.COMPAT_PATH),
        file_sha256(generator.CATALOG_PATH),
    ]
    generation = hashlib.sha256(_canonical(seed)).hexdigest()[:20]
    generated = source_root / (".emuflow-physical-" + generation)
    with (source_root / ".emuflow-prepare.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        internal = generated / "benchmark.json"
        if generated.exists():
            if not internal.is_file():
                raise EmuFlowError(
                    "previous source preparation is incomplete; use a fresh source directory"
                )
            benchmark_value = read_json(internal)
        else:
            prepared = prepare_nvdla_holdout(
                source_root=source_root,
                generated_dir=generated,
                benchmark_path=internal,
                platform=str(Path(platform_path).resolve()),
                memory_policy=PHYSICAL_MEMORY_POLICY,
            )
            benchmark_value = prepared["benchmark"]
        validate_nvdla_source(BenchmarkRun(benchmark_value), source_root)
    benchmark = BenchmarkRun(benchmark_value)
    validate_nvdla_source(benchmark, source_root)
    report = _publish_run(
        benchmark,
        source_root,
        platform_path,
        environment_path,
        output_path,
        NVDLA_OPTIONS,
        profile,
    )
    return {**report, "memory_policy": PHYSICAL_MEMORY_POLICY}
