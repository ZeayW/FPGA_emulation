"""Bounded configuration bridge to the native one-shot compile controller."""
from __future__ import annotations

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

from .errors import EmuFlowError
from .native_compile_adapter import _pack, _plain
from .native_tools import resolve_native_executable


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
    from .multi_fpga_flow import run_multi_fpga_flow

    signature = inspect.signature(run_multi_fpga_flow)
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
    _pack(dict(bound.arguments))
    value = {
        "schema": "emuflow.native-compile/v1",
        # Preserve the interpreter invocation (including a venv symlink).
        "python_executable": os.path.abspath(sys.executable),
        "python_source": str(Path(__file__).resolve().parent.parent),
        "working_directory": str(Path.cwd()),
        "options": _plain(dict(bound.arguments)),
    }
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 1024 * 1024:
        raise EmuFlowError("native compiler configuration exceeds bound")
    return value, encoded


def run_native_multi_fpga_flow(*args, **kwargs):
    """Select native orchestration explicitly; never retry the Python compiler.

    The C++ controller runs TDM natively and other shared phase adapters. This entry does
    not assert that their remaining Python data planes have been migrated.
    """
    from .native_tools import require_native_schema

    require_native_schema("boarddb-v1")
    value, encoded = _request(args, kwargs)
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
                # The native owner reaps adapters and their nested tool owners
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
