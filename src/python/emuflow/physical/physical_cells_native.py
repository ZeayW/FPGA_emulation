"""Bounded control adapter for independent typed mapped/physical checks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import native_file_generation, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess


def _run_check(operation, magic, inputs, text_fields, *, scratch_parent):
    inputs = tuple(Path(path) for path in inputs)
    generations = tuple(native_file_generation(path) for path in inputs)
    executable = resolve_native_executable("emuflow-native")
    with tempfile.TemporaryDirectory(
        prefix=".physical-cells-", dir=Path(scratch_parent)
    ) as temporary:
        root = Path(temporary)
        request, summary, report = (
            root / "request.bin",
            root / "summary.json",
            root / "operation.json",
        )
        writer = CheckedRequestWriter(request)
        try:
            writer.put("QI", magic, 1)
            for generation in generations:
                writer.put("3Q4q", *generation)
            for value in text_fields:
                writer.text(value)
            writer.finish()
        finally:
            writer.close()
        command = [
            executable,
            "--report",
            str(report),
            "phase7",
            operation,
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
            raise ValidationError("native physical check failed: " + completed.stdout)
        if tuple(native_file_generation(path) for path in inputs) != generations:
            raise ValidationError("physical check source generation changed")
        if summary.stat().st_size > 32768 or report.stat().st_size > 32768:
            raise ValidationError("native physical population summary exceeds bound")
        result = json.loads(summary.read_text(encoding="utf-8"))
        common = json.loads(report.read_text(encoding="utf-8"))
        if (
            common.get("schema") != "emuflow.native-stage-result/v1"
            or common.get("stage") != "phase7." + operation
            or common.get("status") != "pass"
        ):
            raise ValidationError("native physical operation result disagrees")
        result["native_metrics"] = common["metrics"]
        return result


def validate_native_physical_cells(mapped_path, physical_path, *, mapped_sha256, top):
    result = _run_check(
        "validate-physical-cells",
        0x31564350464D5545,
        (mapped_path, physical_path),
        (mapped_sha256, top),
        scratch_parent=Path(physical_path).parent,
    )
    fields = ("cells", "assignments", "constants", "clusters", "pins")
    if (
        result.get("schema") != "emuflow.physical-cell-validation/v1"
        or result.get("status") != "pass"
        or result.get("implementation_id") != "native-typed-physical-cells-v1"
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
    return result


def validate_native_atomic_ownership(
    mapped_path,
    source_path,
    certificate_path,
    *,
    mapped_sha256,
    source_logical_sha256,
    certificate_logical_sha256,
    top,
    scratch_parent,
):
    result = _run_check(
        "validate-atomic-ownership",
        0x314F5341464D5545,
        (mapped_path, source_path, certificate_path),
        (mapped_sha256, source_logical_sha256, certificate_logical_sha256, top),
        scratch_parent=scratch_parent,
    )
    fields = (
        "cells",
        "assignments",
        "constants",
        "macro_assignments",
        "singleton_clusters",
        "ownership_snapshots",
    )
    if (
        result.get("schema") != "emuflow.atomic-ownership-validation/v1"
        or result.get("status") != "pass"
        or result.get("implementation_id") != "native-atomic-ownership-merge-v1"
        or any(type(result.get(key)) is not int or result[key] < 0 for key in fields)
        or result["cells"] != result["assignments"] + result["constants"]
        or result["assignments"]
        != result["macro_assignments"] + result["singleton_clusters"]
        or result["ownership_snapshots"] != 0
        or result.get("mapped_sha256") != mapped_sha256
        or result.get("source_logical_sha256") != source_logical_sha256
        or result.get("certificate_logical_sha256") != certificate_logical_sha256
    ):
        raise ValidationError("native atomic ownership result disagrees")
    return result
