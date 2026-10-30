"""Reproducible design configuration; execution always uses native compile."""

from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path

from emuflow.common.errors import EmuFlowError
from emuflow.common.io import file_sha256
from emuflow.flow.benchmark_inputs import BenchmarkRun
from emuflow.flow.environment import (
    load_environment,
    read_control,
    resolve_path,
    compile_environment,
    resolve_environment_path,
    environment_scope,
)
from emuflow.flow import native_compile
from emuflow.platform.default_platform import DEFAULT_PLATFORM_PATH

SCHEMA = "emuflow.run/v1"
_FRONTEND_KEYS = {
    "source_checksums",
    "sources",
    "include_dirs",
    "defines",
    "top",
    "clocks",
    "clock_periods",
    "timing_io",
    "mapping_profile",
    "yosys_json",
    "prepared_frontend",
    "prepared_timing",
    "platform_path",
    "output_dir",
    "physical",
    "physical_backend",
}
_ENV_KEYS = {
    "yosys",
    "opensta",
    "openroad",
    "router",
    "global_sta_executable",
    "architecture_timing_db",
    "patron_refiner",
}


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def validate_nvdla_source(benchmark, source_root):
    from emuflow.flow.designs.nvdla_contract import _catalog_entry, CATALOG_PATH, PHYSICAL_MEMORY_POLICY

    value = benchmark.value
    preparation = value.get("preparation", {})
    catalog = _catalog_entry(CATALOG_PATH)
    if (
        value.get("design_id") != "nvdla"
        or value.get("top") != "NV_nvdla"
        or value.get("clocks") != ["dla_core_clk", "dla_csb_clk"]
        or preparation.get("schema") != "emuflow.nvdla-preparation/v1"
        or preparation.get("memory_policy") != PHYSICAL_MEMORY_POLICY
        or preparation.get("upstream_revision") != catalog["revision"]
        or preparation.get("upstream_archive_sha256") != catalog["archive_sha256"]
        or preparation.get("ram_modeled_count", 0) <= 0
        or preparation.get("ram_modeled_count") != preparation.get("ram_wrapper_count")
    ):
        raise EmuFlowError(
            "NVDLA run requires the pinned NV_nvdla design and complete physical SRAM models; scale/black-box contracts are not physical inputs"
        )
    _, stamp = read_control(Path(source_root) / ".emuflow-source.json")
    for key, expected in (
        ("schema", "emuflow.source-archive/v1"),
        ("design_id", "nvdla"),
        ("revision", catalog["revision"]),
        ("archive_sha256", catalog["archive_sha256"]),
    ):
        if stamp.get(key) != expected:
            raise EmuFlowError("NVDLA source revision differs from the pinned run")
    if hashlib.sha256(_canonical(value["sources"])).hexdigest() != preparation.get(
        "source_list_sha256"
    ):
        raise EmuFlowError("NVDLA source list differs from preparation")
    for item in preparation.get("generated_files", []):
        path = resolve_path(Path(source_root), item["path"])
        if Path(source_root).resolve() not in path.parents or file_sha256(
            path
        ) != item.get("sha256"):
            raise EmuFlowError("NVDLA generated source differs from preparation")
    if len(preparation.get("generated_files", [])) != 3:
        raise EmuFlowError(
            "NVDLA preparation lacks its complete generated-source inventory"
        )


def source_records(benchmark, source_root):
    files = set(benchmark.resolve_sources(source_root))
    for directory in benchmark.resolve_include_dirs(source_root):
        files.update(p.resolve() for p in directory.rglob("*") if p.is_file())
    if len(files) > 16384:
        raise EmuFlowError("design source inventory exceeds the configuration bound")
    records = []
    for path in sorted(files):
        if source_root not in path.parents:
            raise EmuFlowError("design source escapes its declared root")
        records.append([path.relative_to(source_root).as_posix(), file_sha256(path)])
    return records


def source_digest(benchmark, source_root):
    return hashlib.sha256(
        _canonical(source_records(benchmark, source_root))
    ).hexdigest()


def load_run(path):
    path, value = read_control(path)
    if value.get("schema") != SCHEMA or set(value) != {
        "schema",
        "environment",
        "benchmark_run",
        "benchmark_sha256",
        "source_root",
        "platform",
        "options",
        "source_sha256",
        "platform_sha256",
    }:
        raise EmuFlowError("unsupported run configuration schema or fields")
    platform = resolve_path(path.parent, value["platform"])
    if file_sha256(platform) != value["platform_sha256"]:
        raise EmuFlowError("platform differs from the prepared run configuration")
    benchmark_path = resolve_path(path.parent, value["benchmark_run"])
    if file_sha256(benchmark_path) != value["benchmark_sha256"]:
        raise EmuFlowError(
            "design benchmark differs from the prepared run configuration"
        )
    benchmark = BenchmarkRun.load(benchmark_path)
    source_root = resolve_path(path.parent, value["source_root"])
    if benchmark.value.get("design_id") == "nvdla":
        validate_nvdla_source(benchmark, source_root)
    if source_digest(benchmark, source_root) != value["source_sha256"]:
        raise EmuFlowError("design RTL/include bytes differ from the prepared run")
    value = {**value, "options": _compile_options(value["options"], path.parent)}
    return path, value, benchmark, source_root


def _compile_options(options, base):
    if (
        type(options) is not dict
        or set(options) & (_FRONTEND_KEYS | _ENV_KEYS)
        or any(
            key.startswith("physical_")
            and key
            not in {
                "physical_seed",
                "physical_workers",
                "physical_vivado_place_directive",
                "physical_vivado_route_directive",
            }
            for key in options
        )
    ):
        raise EmuFlowError(
            "run options cannot override the design or environment owners"
        )
    signature = native_compile._COMPILE_SIGNATURE
    options = dict(options)
    for key, item in list(options.items()):
        if key not in signature.parameters:
            raise EmuFlowError(f"unknown compile option {key!r}")
        if item is not None and "Path" in str(signature.parameters[key].annotation):
            options[key] = resolve_path(base, item)
    return options


def _require_physical_design(benchmark):
    synthesis = benchmark.value["synthesis"]
    if (
        synthesis.get("family") != "xcup"
        or synthesis.get("policy") != "native"
        or not benchmark.value.get("clock_periods_ns")
        or benchmark.value.get("timing_io") is None
        or not benchmark.value.get("physical_mapping_profile")
    ):
        raise EmuFlowError(
            "a complete physical run requires a timed native Xilinx benchmark"
        )






def _execute_benchmark(
    benchmark,
    source_root,
    profile,
    platform,
    options,
    output_dir,
    *,
    backend,
    check_only,
):
    environment_options = compile_environment(profile, backend)
    synthesis = benchmark.value["synthesis"]
    periods = benchmark.value.get("clock_periods_ns")
    _require_physical_design(benchmark)
    records = source_records(benchmark, source_root)
    compile_options = {
        **options,
        **environment_options,
        "source_checksums": {
            str(source_root / name): digest for name, digest in records
        },
        "sources": benchmark.resolve_sources(source_root),
        "include_dirs": benchmark.resolve_include_dirs(source_root),
        "defines": list(synthesis.get("defines", [])),
        "top": benchmark.value["top"],
        "clocks": list(benchmark.value["clocks"]),
        "clock_periods": dict(periods),
        "timing_io": benchmark.value["timing_io"],
        "mapping_profile": benchmark.value["physical_mapping_profile"],
        "platform_path": Path(platform).expanduser().resolve(),
        "output_dir": Path(output_dir).expanduser().resolve(),
        "physical": True,
        "physical_backend": backend,
    }
    from emuflow.flow.native_compile import _request

    with environment_scope(profile):
        _request((), compile_options)
        if check_only:
            return {
                "schema": "emuflow.run-check/v1",
                "status": "pass",
                "scope": "source-configuration-and-runtime-inventory",
                "design": benchmark.value["design_id"],
                "top": benchmark.value["top"],
                "source_files": len(compile_options["sources"]),
                "clocks": periods,
                "backend": backend,
                "output": str(compile_options["output_dir"]),
                "terminal_qualification": False,
            }
        return native_compile.run_native_multi_fpga_flow(**compile_options)


def execute_sources(
    sources,
    *,
    top,
    clock_periods,
    output_dir,
    backend="rapidwright",
    include_dirs=(),
    defines=(),
    platform_path=DEFAULT_PLATFORM_PATH,
    phase6_provider="baseline",
    check_only=False,
):
    """Run user RTL directly; preparation artifacts stay internal."""
    source_paths = [Path(item).expanduser().resolve() for item in sources]
    include_paths = [Path(item).expanduser().resolve() for item in include_dirs]
    if not source_paths or any(not item.is_file() for item in source_paths):
        raise EmuFlowError("run requires existing RTL source files")
    if any(not item.is_dir() for item in include_paths):
        raise EmuFlowError("run include directories must exist")
    roots = [item.parent for item in source_paths] + include_paths
    source_root = Path(os.path.commonpath([str(item) for item in roots])).resolve()
    if source_root.is_file():
        source_root = source_root.parent
    platform = Path(platform_path).expanduser().resolve()
    from emuflow.platform.platform import Platform

    Platform.load(platform)
    benchmark = BenchmarkRun(
        {
            "schema": "emuflow.benchmark-run/v1",
            "id": top,
            "design_id": top,
            "top": top,
            "sources": [
                item.relative_to(source_root).as_posix() for item in source_paths
            ],
            "clocks": list(clock_periods),
            "clock_periods_ns": dict(clock_periods),
            "timing_io": {"input_groups": [], "output_groups": []},
            "platform": str(platform),
            "physical_mapping_profile": "xilinx-ultrascaleplus-open-v1",
            "synthesis": {
                "family": "xcup",
                "policy": "native",
                "include_dirs": [
                    item.relative_to(source_root).as_posix() for item in include_paths
                ],
                "defines": list(defines),
            },
        }
    )
    profile = load_environment(resolve_environment_path())
    return _execute_benchmark(
        benchmark,
        source_root,
        profile,
        platform,
        {"phase6_provider": phase6_provider},
        output_dir,
        backend=backend,
        check_only=check_only,
    )


def execute_run(config_path, output_dir, *, backend="rapidwright", check_only=False):
    path, value, benchmark, source_root = load_run(config_path)
    profile = load_environment(resolve_path(path.parent, value["environment"]))
    return _execute_benchmark(
        benchmark,
        source_root,
        profile,
        resolve_path(path.parent, value["platform"]),
        value["options"],
        output_dir,
        backend=backend,
        check_only=check_only,
    )
