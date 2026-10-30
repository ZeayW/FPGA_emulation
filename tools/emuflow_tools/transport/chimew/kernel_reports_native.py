"""File-reference host for the native grouping/refinement/RUDY report owner."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess

_FIELDS = ("device", "inode", "bytes", "mtime_seconds", "mtime_nanoseconds", "ctime_seconds", "ctime_nanoseconds")
_REPORTS = ("initial_grouping", "refined_grouping", "rudy_report", "kernel_reports")


def build_chimew_kernel_reports(schedule, crossings, positions, rudy_input, output, *, executables=()):
    native = resolve_native_executable("emuflow-native")
    for executable in executables:
        if executable is not None:
            try:
                same = Path(executable).samefile(native)
            except OSError:
                same = False
            if not same:
                raise ValidationError("Chimew kernels require the unified native executable")
    sources = {role: {"path": str(Path(path).resolve()), "schema": "physical-input-document-v1"}
               for role, path in zip(("schedule", "crossings", "positions", "rudy_input"),
                                     (schedule, crossings, positions, rudy_input))}
    target = Path(output).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".chimew-kernel-host-", dir=target.parent) as temporary:
        root = Path(temporary).resolve()
        request, receipt = root / "request.json", root / "operation.json"
        request.write_text(json.dumps({"schema": "emuflow.chimew-kernel-reports-input/v1", "sources": sources},
                                      sort_keys=True, allow_nan=False), encoding="utf-8")
        expected = {(row["path"], row["schema"]): native_file_generation(row["path"]) for row in sources.values()}
        expected[(str(request), "chimew-kernel-reports-input-v1")] = native_file_generation(request)
        with NativeToolProcess(native, [native, "--report", str(receipt), "phase6", "build-chimew-kernel-reports",
                                       str(request), str(target)], environment=dict(os.environ), scratch_parent=root) as process:
            completed = process.finish()
        if not receipt.is_file() or receipt.stat().st_size > 1048576:
            raise ValidationError("native Chimew kernel receipt is missing or exceeds bound")
        operation = json.loads(receipt.read_text())
        if completed.returncode:
            raise ValidationError("native Chimew kernel reports failed: " + operation.get("error", {}).get("message", completed.stdout[-4096:]))
        if (operation.get("schema") != "emuflow.native-stage-result/v1" or operation.get("status") != "pass"
                or operation.get("stage") != "phase6.build-chimew-kernel-reports" or len(operation.get("outputs", [])) != 4):
            raise ValidationError("native Chimew kernel receipt identity differs")
        observed = {}
        for row in operation.get("inputs", []):
            key = row["path"], row["schema"]
            generation = tuple(row["generation"][field] for field in _FIELDS)
            if native_file_generation(row["path"]) != generation or (key in observed and observed[key] != generation):
                raise ValidationError("native Chimew kernel input generation changed")
            observed[key] = generation
        if any(observed.get(key) != generation for key, generation in expected.items()):
            raise ValidationError("native Chimew kernel input coverage differs")
        outputs = {(str(target / (role + ".json")), "physical-input-document-v1") for role in _REPORTS}
        for row in operation["outputs"]:
            key = row["path"], row["schema"]
            if key not in outputs or native_file_generation(row["path"]) != tuple(row["generation"][field] for field in _FIELDS):
                raise ValidationError("native Chimew kernel output generation differs")
            outputs.remove(key)
        if outputs:
            raise ValidationError("native Chimew kernel output coverage differs")
        summary = target / "kernel_reports.json"
        if summary.stat().st_size > 262144:
            raise ValidationError("native Chimew kernel summary exceeds bound")
        result = json.loads(summary.read_text())
        if result.get("schema") != "emuflow.chimew-kernel-reports/v1" or result.get("status") != "pass":
            raise ValidationError("native Chimew kernel summary identity differs")
        return result
