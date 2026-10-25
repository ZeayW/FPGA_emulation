"""Bounded control adapter for independent typed mapped/physical checks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from .errors import ValidationError
from .native_protocol import CheckedRequestWriter
from .native_tools import native_file_generation, resolve_native_executable
from .tool_runtime import NativeToolProcess


def validate_native_physical_cells(mapped_path, physical_path, *, mapped_sha256, top):
    inputs = (Path(mapped_path), Path(physical_path))
    generations = tuple(native_file_generation(path) for path in inputs)
    executable = resolve_native_executable("emuflow-native")
    with tempfile.TemporaryDirectory(
        prefix=".physical-cells-", dir=inputs[1].parent
    ) as temporary:
        root = Path(temporary)
        request, summary, operation = (
            root / "request.bin",
            root / "summary.json",
            root / "operation.json",
        )
        writer = CheckedRequestWriter(request)
        try:
            writer.put("QI", 0x31564350464D5545, 1)
            for generation in generations:
                writer.put("3Q4q", *generation)
            writer.text(mapped_sha256)
            writer.text(top)
            writer.finish()
        finally:
            writer.close()
        command = [
            executable,
            "--report",
            str(operation),
            "phase7",
            "validate-physical-cells",
            *(str(path) for path in inputs),
            str(request),
            str(summary),
        ]
        with NativeToolProcess(
            executable,
            command,
            environment=dict(os.environ),
            timeout_ms=21600000,
            scratch_parent=root,
        ) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError(
                "native physical population validation failed: " + completed.stdout
            )
        if tuple(native_file_generation(path) for path in inputs) != generations:
            raise ValidationError("physical population source generation changed")
        if summary.stat().st_size > 32768 or operation.stat().st_size > 32768:
            raise ValidationError("native physical population summary exceeds bound")
        result = json.loads(summary.read_text(encoding="utf-8"))
        common = json.loads(operation.read_text(encoding="utf-8"))
        fields = ("cells", "assignments", "constants", "clusters", "pins")
        if (
            result.get("schema") != "emuflow.physical-cell-validation/v1"
            or result.get("status") != "pass"
            or result.get("implementation_id") != "native-typed-physical-cells-v1"
            or common.get("schema") != "emuflow.native-stage-result/v1"
            or common.get("stage") != "phase7.validate-physical-cells"
            or common.get("status") != "pass"
            or any(
                isinstance(result.get(key), bool)
                or not isinstance(result.get(key), int)
                or result[key] < 0
                for key in fields
            )
            or result["cells"] != result["assignments"] + result["constants"]
            or result.get("driver_uniqueness") != "complete-mapped-output-population"
        ):
            raise ValidationError("native physical population result disagrees")
        result["native_metrics"] = common["metrics"]
        return result
