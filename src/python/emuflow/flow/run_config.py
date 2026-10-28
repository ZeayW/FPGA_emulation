"""Reproducible design configuration; execution always uses native compile."""

from __future__ import annotations
import hashlib
import fcntl
import json
from pathlib import Path
import tempfile

from emuflow.common.errors import EmuFlowError
from emuflow.common.io import file_sha256, write_json
from emuflow.flow.benchmark_inputs import BenchmarkRun
from emuflow.flow.environment import (
    load_environment,
    read_control,
    resolve_path,
    compile_environment,
    environment_scope,
)
from emuflow.flow.multi_fpga_flow import run_multi_fpga_flow
from emuflow.platform.default_platform import DEFAULT_PLATFORM_PATH

SCHEMA = "emuflow.run/v1"
# These are the recorded four-FPGA NVDLA run choices, not changed algorithm
# defaults. They are written explicitly into each prepared run configuration.
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
    from emuflow.flow.designs.nvdla import _catalog_entry, CATALOG_PATH
    from emuflow.flow.designs.nvdla_memory import PHYSICAL_MEMORY_POLICY

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
        files.update(p for p in directory.rglob("*") if p.is_file())
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
    options = value["options"]
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
    import inspect

    signature = inspect.signature(run_multi_fpga_flow)
    options = dict(options)
    for key, item in list(options.items()):
        if key not in signature.parameters:
            raise EmuFlowError(f"unknown compile option {key!r}")
        if item is not None and "Path" in str(signature.parameters[key].annotation):
            options[key] = resolve_path(path.parent, item)
    value = {**value, "options": options}
    return path, value, benchmark, source_root


def execute_run(config_path, output_dir, *, backend="rapidwright", check_only=False):
    path, value, benchmark, source_root = load_run(config_path)
    profile = load_environment(resolve_path(path.parent, value["environment"]))
    environment_options = compile_environment(profile, backend)
    synthesis = benchmark.value["synthesis"]
    periods = benchmark.value.get("clock_periods_ns")
    if (
        synthesis.get("family") != "xcup"
        or synthesis.get("policy") != "native"
        or not periods
        or benchmark.value.get("timing_io") is None
    ):
        raise EmuFlowError(
            "a complete physical run requires a timed native Xilinx benchmark"
        )
    records = source_records(benchmark, source_root)
    options = {
        **value["options"],
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
        "platform_path": resolve_path(path.parent, value["platform"]),
        "output_dir": Path(output_dir).expanduser().resolve(),
        "physical": True,
        "physical_backend": backend,
    }
    # Use the very same binding/size/default contract as execution. A check is
    # a configuration/inventory check, never a physical completion certificate.
    from emuflow.flow.native_compile import _request

    with environment_scope(profile):
        _request((), options)
        if check_only:
            return {
                "schema": "emuflow.run-check/v1",
                "status": "pass",
                "scope": "source-configuration-and-runtime-inventory",
                "design": benchmark.value["design_id"],
                "top": benchmark.value["top"],
                "source_files": len(options["sources"]),
                "clocks": periods,
                "backend": backend,
                "output": str(options["output_dir"]),
                "terminal_qualification": False,
            }
        return run_multi_fpga_flow(**options)


def prepare_nvdla_run(
    source_root, environment_path, output_path, *, platform_path=DEFAULT_PLATFORM_PATH
):
    from emuflow.flow.designs.nvdla import prepare_nvdla_holdout
    from emuflow.flow.designs.nvdla_memory import PHYSICAL_MEMORY_POLICY

    source_root = Path(source_root).expanduser().resolve()
    environment_path = Path(environment_path).expanduser().resolve()
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
    from emuflow.flow.designs import nvdla as generator
    from emuflow.common.io import read_json
    from emuflow.flow.designs.nvdla_memory import __file__ as memory_source

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
        write_json(benchmark_path, benchmark_value)
    prepared = {
        "benchmark": benchmark_value,
        "source_count": len(benchmark_value["sources"]),
    }
    validate_nvdla_source(BenchmarkRun(prepared["benchmark"]), source_root)
    value = {
        "schema": SCHEMA,
        "environment": str(environment_path),
        "benchmark_run": str(benchmark_path),
        "benchmark_sha256": file_sha256(benchmark_path),
        "source_root": str(source_root),
        "platform": str(Path(platform_path).resolve()),
        "platform_sha256": file_sha256(Path(platform_path)),
        "source_sha256": source_digest(
            BenchmarkRun(prepared["benchmark"]), source_root
        ),
        "options": dict(NVDLA_OPTIONS),
    }
    write_json(output_path, value)
    return {
        "status": "pass",
        "scope": "design-preparation",
        "terminal_qualification": False,
        "run_config": str(output_path),
        "top": "NV_nvdla",
        "source_files": prepared["source_count"],
        "memory_policy": PHYSICAL_MEMORY_POLICY,
        "backends": sorted(profile["backends"]),
    }
