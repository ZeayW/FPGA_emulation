"""Bounded source host for native compact-to-mapped physical input production."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import (
    native_file_generation,
    require_native_schema,
    resolve_native_executable,
)
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.physical.handoff import _small
from emuflow.storage.store_publication import (
    StoreSource,
    bind_store_source,
    require_store_sources_unchanged,
    validate_store_destination,
)


def _write_mapped_request(path, sources):
    writer = CheckedRequestWriter(path)
    try:
        writer.put("QI", 0x31584D50464D5545, 1)
        for source in sources:
            writer.put("3Q4q", *source.generation)
        writer.finish()
    finally:
        writer.close()


def emit_native_mapped_index(ir_path, index_path, report_path=None):
    from emuflow.physical.xilinx_primitives import DEFAULT_XILINX_PRIMITIVE_LIBRARY

    require_native_schema("xilinx-mapped-index-request-v1")
    sources = (
        bind_store_source(Path(ir_path).resolve(), "emuir-compact-v2"),
        bind_store_source(
            DEFAULT_XILINX_PRIMITIVE_LIBRARY.resolve(), "physical-input-document-v1"
        ),
    )
    output = Path(index_path).resolve()
    validate_store_destination(output, sources)
    if report_path is not None:
        report_path = Path(report_path).resolve()
        validate_store_destination(report_path, sources)
        if output == report_path or (
            output.exists()
            and report_path.exists()
            and os.path.samefile(output, report_path)
        ):
            raise ValidationError("mapped-index report aliases its output")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".mapped-input-", dir=output.parent
    ) as temporary:
        root = Path(temporary).resolve()
        request, operation = root / "request.bin", root / "operation.json"
        summary = report_path if report_path is not None else root / "summary.json"
        _write_mapped_request(request, sources)
        require_store_sources_unchanged(sources)
        executable = resolve_native_executable("emuflow-native")
        command = [
            executable,
            "--report",
            str(operation),
            "physical",
            "build-mapped-index",
            *(str(source.path) for source in sources),
            str(request),
            str(output),
            str(summary),
        ]
        with NativeToolProcess(
            executable, command, environment=dict(os.environ), scratch_parent=root
        ) as process:
            result = process.finish()
        if result.returncode:
            raise ValidationError(
                "native physical mapped input failed: " + result.stdout
            )
        proof = _small(operation, 262144)
        outputs = proof.get("outputs", [])
        if (
            proof.get("stage") != "physical.build-mapped-index"
            or proof.get("status") != "pass"
            or len(outputs) != 2
        ):
            raise ValidationError("native mapped input proof disagrees")
        fields = (
            "device",
            "inode",
            "bytes",
            "mtime_seconds",
            "mtime_nanoseconds",
            "ctime_seconds",
            "ctime_nanoseconds",
        )
        produced = []
        for row, path, schema in zip(
            outputs,
            (output, summary),
            ("xilinx-mapped-index-v2", "xilinx-mapped-index-report-v1"),
        ):
            generation = tuple(row.get("generation", {}).get(key) for key in fields)
            if (
                row.get("path") != str(path)
                or row.get("schema") != schema
                or any(type(value) is not int for value in generation)
                or generation != native_file_generation(path)
            ):
                raise ValidationError("native mapped input generation changed")
            produced.append(StoreSource(path, schema, generation))
        report = _small(summary, 262144)
        if (
            report.get("schema") != "emuflow.xilinx-mapped-netlist-report/v1"
            or report.get("status") != "pass"
            or report.get("implementation_id") != "native-xilinx-mapped-index-v1"
            or report.get("index") != str(output)
        ):
            raise ValidationError("native mapped input summary disagrees")
        require_store_sources_unchanged((*sources, *produced))
        return report
