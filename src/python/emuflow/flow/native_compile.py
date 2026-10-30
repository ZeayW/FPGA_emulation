"""Bounded configuration bridge to the native one-shot compile controller."""
from __future__ import annotations

from emuflow.common.paths import PYTHON_ROOT

import inspect
import json
import os
from contextlib import contextmanager
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
from typing import Any, Dict, Iterable, Mapping, Optional
from emuflow.flow.compile_options import COMPILE_DEFAULTS

from emuflow.common.errors import EmuFlowError
import math
from emuflow.common.native_tools import resolve_native_executable


def _control_options(value, depth=0):
    """Encode finite public options without accepting circuit-sized views."""
    if depth > 64:
        raise EmuFlowError("native compiler control options are too deep")
    if isinstance(value, Path):
        return str(value)
    if type(value) in {dict, list, tuple}:
        if len(value) > 16384:
            raise EmuFlowError("graph-sized collection in native compiler options")
        if type(value) is dict:
            if not all(type(key) is str for key in value):
                raise EmuFlowError("invalid native compiler option key")
            return {key: _control_options(item, depth+1) for key,item in value.items()}
        return [_control_options(item, depth+1) for item in value]
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise EmuFlowError("unsupported object in native compiler options: "+type(value).__name__)


@contextmanager
def _cancellable():
    previous = {}
    def cancel(signum, _frame):
        raise KeyboardInterrupt(f"native compiler cancelled by signal {signum}")
    def stop_interrupts():
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
    try:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous[sig] = signal.signal(sig, cancel)
        yield stop_interrupts
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _request(args, kwargs):
    signature = _COMPILE_SIGNATURE
    bound = signature.bind(*args, **kwargs)
    bound.apply_defaults()
    if (
        isinstance(bound.arguments['patron_algorithm_version'], bool)
        or not isinstance(bound.arguments['patron_algorithm_version'], int)
        or bound.arguments['patron_algorithm_version'] != 14
    ):
        raise EmuFlowError("only PATRON v14 is supported")
    if bound.arguments["global_timing_engine"] != "opensta":
        raise EmuFlowError("only the OpenSTA global timing engine is supported")
    for name, value in list(bound.arguments.items()):
        annotation = str(signature.parameters[name].annotation)
        if value is not None and "Path" in annotation:
            def path(item):
                return Path(item).expanduser().absolute()
            bound.arguments[name] = tuple(path(item) for item in value) if "Iterable" in annotation else path(value)
        elif value is not None and "Iterable" in annotation:
            bound.arguments[name] = tuple(value)
    if bound.arguments["output_dir"] is None:
        raise EmuFlowError("multi-FPGA compilation requires an output directory")
    # Validate before serializing: mapping subclasses and graph views must never
    # be expanded by this control bridge. The file is configuration, not a graph.
    options = _control_options(dict(bound.arguments))
    value = {
        "schema": "emuflow.native-compile/v1",
        # Preserve the interpreter invocation (including a venv symlink).
        "python_executable": os.path.abspath(sys.executable),
        "python_source": str(PYTHON_ROOT),
        "working_directory": str(Path.cwd()),
        "options": options,
    }
    from emuflow.partition.structural_policy import _pattern_range_policy
    value["pattern_range_policy"] = _pattern_range_policy()
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 1024 * 1024:
        raise EmuFlowError("native compiler configuration exceeds bound")
    return value, encoded


def run_native_multi_fpga_flow(
    platform_path: Path = COMPILE_DEFAULTS['platform_path'],
    output_dir: Optional[Path] = COMPILE_DEFAULTS['output_dir'],
    *,
    sources: Iterable[Path] = COMPILE_DEFAULTS['sources'],
    include_dirs: Iterable[Path] = COMPILE_DEFAULTS['include_dirs'],
    defines: Iterable[str] = COMPILE_DEFAULTS['defines'],
    source_checksums: Optional[Dict[str, str]] = COMPILE_DEFAULTS['source_checksums'],
    top: Optional[str] = COMPILE_DEFAULTS['top'],
    clocks: Iterable[str] = COMPILE_DEFAULTS['clocks'],
    yosys_json: Optional[Path] = COMPILE_DEFAULTS['yosys_json'],
    prepared_frontend: Optional[Path] = COMPILE_DEFAULTS['prepared_frontend'],
    prepared_timing: Optional[Path] = COMPILE_DEFAULTS['prepared_timing'],
    yosys: Optional[str] = COMPILE_DEFAULTS['yosys'],
    mapping_profile: str = COMPILE_DEFAULTS['mapping_profile'],
    xilinx_mapping_strategy: Optional[str] = COMPILE_DEFAULTS['xilinx_mapping_strategy'],
    partition_constraints: Optional[Path] = COMPILE_DEFAULTS['partition_constraints'],
    partition_provider: str = COMPILE_DEFAULTS['partition_provider'],
    seed: int = COMPILE_DEFAULTS['seed'],
    min_used_fpgas: Optional[int] = COMPILE_DEFAULTS['min_used_fpgas'],
    balance_tolerance: Optional[float] = COMPILE_DEFAULTS['balance_tolerance'],
    openroad: Optional[str] = COMPILE_DEFAULTS['openroad'],
    tritonpart_solution: Optional[Path] = COMPILE_DEFAULTS['tritonpart_solution'],
    patron_refiner: Optional[str] = COMPILE_DEFAULTS['patron_refiner'],
    patron_max_moves: Optional[int] = COMPILE_DEFAULTS['patron_max_moves'],
    patron_algorithm_version: int = COMPILE_DEFAULTS['patron_algorithm_version'],
    partition_timeout_seconds: int = COMPILE_DEFAULTS['partition_timeout_seconds'],
    partition_seed_attempts: int = COMPILE_DEFAULTS['partition_seed_attempts'],
    partition_num_initial_solutions: int = COMPILE_DEFAULTS['partition_num_initial_solutions'],
    partition_num_best_initial_solutions: int = COMPILE_DEFAULTS['partition_num_best_initial_solutions'],
    partition_repair_min_used_fpgas: bool = COMPILE_DEFAULTS['partition_repair_min_used_fpgas'],
    cut_mode: str = COMPILE_DEFAULTS['cut_mode'],
    max_cross_fpga_dependency_depth: int = (
        COMPILE_DEFAULTS['max_cross_fpga_dependency_depth']
    ),
    static_exact_candidate_policy: str = COMPILE_DEFAULTS['static_exact_candidate_policy'],
    timing_driven: bool = COMPILE_DEFAULTS['timing_driven'],
    clock_periods: Optional[Dict[str, float]] = COMPILE_DEFAULTS['clock_periods'],
    timing_io: Optional[Mapping[str, Any]] = COMPILE_DEFAULTS['timing_io'],
    timing_model: Path = COMPILE_DEFAULTS['timing_model'],
    architecture_timing_db: Optional[Path] = COMPILE_DEFAULTS['architecture_timing_db'],
    opensta: Optional[str] = COMPILE_DEFAULTS['opensta'],
    sta_max_paths: int = COMPILE_DEFAULTS['sta_max_paths'],
    timing_criticality_scale: float = COMPILE_DEFAULTS['timing_criticality_scale'],
    timing_criticality_exponent: float = COMPILE_DEFAULTS['timing_criticality_exponent'],
    route_constraints: Optional[Path] = COMPILE_DEFAULTS['route_constraints'],
    board_link_timing_db: Optional[Path] = COMPILE_DEFAULTS['board_link_timing_db'],
    transport_cost_db: Optional[Path] = COMPILE_DEFAULTS['transport_cost_db'],
    timing_paths: Optional[Path] = COMPILE_DEFAULTS['timing_paths'],
    router: Optional[str] = COMPILE_DEFAULTS['router'],
    route_provider: Optional[str] = COMPILE_DEFAULTS['route_provider'],
    route_candidate_workers: int = COMPILE_DEFAULTS['route_candidate_workers'],
    frame_slots: Optional[int] = COMPILE_DEFAULTS['frame_slots'],
    optimize_frame_slots: bool = COMPILE_DEFAULTS['optimize_frame_slots'],
    route_max_iterations: Optional[int] = COMPILE_DEFAULTS['route_max_iterations'],
    tdm_provider: Optional[str] = COMPILE_DEFAULTS['tdm_provider'],

    simulation_frames: int = COMPILE_DEFAULTS['simulation_frames'],
    equivalence_cycles: int = COMPILE_DEFAULTS['equivalence_cycles'],
    equivalence_seed: int = COMPILE_DEFAULTS['equivalence_seed'],
    phase6_provider: str = COMPILE_DEFAULTS['phase6_provider'],
    phase6_chimew_region_count: int = COMPILE_DEFAULTS['phase6_chimew_region_count'],
    phase6_chimew_grouper: Optional[str] = COMPILE_DEFAULTS['phase6_chimew_grouper'],
    phase6_chimew_refiner: Optional[str] = COMPILE_DEFAULTS['phase6_chimew_refiner'],
    phase6_chimew_rudy: Optional[str] = COMPILE_DEFAULTS['phase6_chimew_rudy'],
    phase6_chimew_assigner: Optional[str] = COMPILE_DEFAULTS['phase6_chimew_assigner'],
    physical: bool = COMPILE_DEFAULTS['physical'],
    physical_backend: str = COMPILE_DEFAULTS['physical_backend'],
    physical_architecture: Optional[Path] = COMPILE_DEFAULTS['physical_architecture'],
    physical_openparf_install: Optional[Path] = COMPILE_DEFAULTS['physical_openparf_install'],
    physical_openparf_python: Optional[Path] = COMPILE_DEFAULTS['physical_openparf_python'],
    physical_seed: int = COMPILE_DEFAULTS['physical_seed'],
    physical_vivado: Optional[str] = COMPILE_DEFAULTS['physical_vivado'],
    physical_vivado_place_directive: str = COMPILE_DEFAULTS['physical_vivado_place_directive'],
    physical_vivado_route_directive: str = COMPILE_DEFAULTS['physical_vivado_route_directive'],
    physical_rapidwright_jar: Optional[Path] = COMPILE_DEFAULTS['physical_rapidwright_jar'],
    physical_rapidwright_java: Optional[Path] = COMPILE_DEFAULTS['physical_rapidwright_java'],
    physical_rapidwright_java_source: Optional[Path] = COMPILE_DEFAULTS['physical_rapidwright_java_source'],
    physical_rapidwright_device_data: Optional[Path] = COMPILE_DEFAULTS['physical_rapidwright_device_data'],
    physical_rapidwright_timing_data: Optional[Path] = COMPILE_DEFAULTS['physical_rapidwright_timing_data'],
    physical_rapidwright_opensta: Optional[str] = COMPILE_DEFAULTS['physical_rapidwright_opensta'],
    physical_rapidwright_native_constraints: Optional[Path] = COMPILE_DEFAULTS['physical_rapidwright_native_constraints'],
    physical_rapidwright_provider_manifest: Optional[Path] = COMPILE_DEFAULTS['physical_rapidwright_provider_manifest'],
    physical_workers: int = COMPILE_DEFAULTS['physical_workers'],
    global_sta_executable: Optional[str] = COMPILE_DEFAULTS['global_sta_executable'],
    global_timing_engine: str = COMPILE_DEFAULTS['global_timing_engine'],
) -> Dict[str, Any]:
    """Execute the sole production controller; never select another control chain.

    The C++ controller owns RTL frontend, pre-placement timing, partitioning,
    routing, TDM, baseline/Chimew splitting, both physical backends and
    global timing. This host encodes bounded options and reads final summaries.
    """
    options = locals().copy()
    from emuflow.common.native_tools import require_native_schema

    require_native_schema("boarddb-v1")
    value, encoded = _request((), options)
    executable = resolve_native_executable("emuflow-native")
    output = Path(value["options"]["output_dir"])
    output.parent.mkdir(parents=True, exist_ok=True)
    # Outside the output so its empty-directory publication contract is intact.
    with _cancellable() as stop_interrupts, tempfile.TemporaryDirectory(
        prefix=".emuflow-compile-request-", dir=output.parent,
    ) as scratch:
        request = Path(scratch) / "request.json"
        request.write_text(encoded, encoding="utf-8")
        with (Path(scratch) / "driver.stderr").open("w+b") as errors:
            try:
                process = subprocess.Popen([executable, "compile", str(request)], stderr=errors)
            except OSError as error:
                raise EmuFlowError(f"native compiler failed to launch: {error}") from error
            try:
                code = process.wait()
            except BaseException:
                stop_interrupts()
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                # The native owner reaps workers and their nested tool owners
                # before request scratch is removed. Do not kill it early.
                process.wait()
                raise
            errors.seek(0, os.SEEK_END)
            errors.seek(max(0, errors.tell() - 65536))
            diagnostic = errors.read().decode("utf-8", errors="replace")
        if code != 0:
            raise EmuFlowError(f"native compile failed ({code}): {diagnostic}")
        result_path = output / "native-compile-result.json"
        if not result_path.is_file() or result_path.stat().st_size > 4 * 1024 * 1024:
            raise EmuFlowError("native compiler result is missing or exceeds bound")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if (result.get("schema") != "emuflow.native-compile-result/v2" or
                result.get("operation", {}).get("status") != "pass"):
            raise EmuFlowError("native compiler result disagrees with its exit")
        # The usual flow report remains the API result. Controller configuration
        # and metrics have their own canonical owner, never copied into it.
        report_path = output / "multi-fpga-flow-report.json"
        if report_path.stat().st_size > 8 * 1024 * 1024:
            raise EmuFlowError("native compiler flow summary exceeds bound")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("schema") != "emuflow.multi-fpga-flow/v2" or report.get("status") != "pass":
            raise EmuFlowError("native compiler flow report is invalid")
        return report


_COMPILE_SIGNATURE = inspect.signature(run_native_multi_fpga_flow)
