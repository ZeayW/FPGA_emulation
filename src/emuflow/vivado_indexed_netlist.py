"""Bounded host for shared-core Vivado RTL/source-function export."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .errors import ValidationError
from .store_publication import (
    bind_store_source, publish_derived_outputs, require_store_sources_unchanged,
    validate_store_destination,
)
from .io import file_sha256, read_json, write_json
from .native_protocol import CheckedRequestWriter
from .native_tools import require_native_schema, resolve_native_executable
from .tool_runtime import NativeToolProcess
from .vivado_lineage import LINEAGE_POLICY


def emit_indexed_vivado_verilog(
    ir_path: Path, output_path: Path, report_path: Path | None = None,
) -> Mapping[str, Any]:
    from .xilinx_netlist import emit_xilinx_mapped_index
    from .vivado_source import validate_vivado_source

    ir_source = bind_store_source(ir_path, "emuir-compact-v2")
    index_path = output_path.with_suffix(".mapped.sqlite")
    functions_path = output_path.with_suffix(".functions.sqlite")
    source_report = output_path.with_suffix(".source-check.json")
    destinations = [output_path, index_path, functions_path, source_report]
    if report_path is not None:
        destinations.append(report_path)
    for number, path in enumerate(destinations):
        validate_store_destination(path, (ir_source,))
        for other in destinations[:number]:
            if path.resolve() == other.resolve() or (
                path.exists() and other.exists() and os.path.samefile(path, other)
            ):
                raise ValidationError("Vivado export destinations alias each other")
    require_native_schema("vivado-export-request-v1")
    executable = resolve_native_executable("emuflow-native")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    source_validation = validate_vivado_source(ir_path, source_report)
    mapped_report = emit_xilinx_mapped_index(ir_path, index_path)
    sources = (ir_source, bind_store_source(index_path, "xilinx-mapped-index-v2"))
    staged = []
    try:
        with tempfile.TemporaryDirectory(prefix=".vivado-export.", dir=output_path.parent) as temporary:
            root = Path(temporary)
            for suffix in (".v", ".sqlite"):
                descriptor, name = tempfile.mkstemp(prefix=".vivado-export.", suffix=suffix, dir=output_path.parent)
                os.close(descriptor)
                staged.append(Path(name))
            rtl, functions = staged
            request, summary = root / "request.bin", root / "summary.json"
            writer = CheckedRequestWriter(request)
            try:
                writer.put("QI", 0x31585656464d5545, 1)
                writer.put("3Q4q", *sources[1].generation)
                writer.text(mapped_report["index_sha256"])
                writer.put("Q", mapped_report["cells"])
                writer.finish()
            finally:
                writer.close()
            require_store_sources_unchanged(sources)
            with NativeToolProcess(
                executable, [executable, "phase7", "export-vivado", str(index_path.resolve()),
                             str(request), str(rtl), str(functions), str(summary)],
                environment=dict(os.environ), scratch_parent=root,
            ) as process:
                completed = process.finish()
            if completed.returncode:
                raise ValidationError("native Vivado export failed: " + completed.stdout)
            require_store_sources_unchanged(sources)
            if summary.stat().st_size > 65536:
                raise ValidationError("native Vivado export summary exceeds bound")
            result = read_json(summary)
            if (
                not isinstance(result, dict)
                or set(result) != {"schema", "status", "implementation_id", "top", "cells", "mapped_index_sha256", "rtl_sha256"}
                or result["schema"] != "emuflow.vivado-export-summary/v1"
                or result["status"] != "pass"
                or result["implementation_id"] != "native-vivado-export-v1"
                or result["top"] != mapped_report["top"]
                or type(result["cells"]) is not int
                or result["cells"] != mapped_report["cells"]
                or result["mapped_index_sha256"] != mapped_report["index_sha256"]
                or not isinstance(result["rtl_sha256"], str)
                or len(result["rtl_sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in result["rtl_sha256"])
            ):
                raise ValidationError("native Vivado export summary disagrees")
            # Native production and validation completed before either public
            # output changes. The bounded report is published last by the host.
            publish_derived_outputs(
                ((rtl, output_path, None), (functions, functions_path, "vivado-source-functions-v1")),
                sources=sources,
            )
    finally:
        for path in staged:
            path.unlink(missing_ok=True)
    report = {
        "schema": "emuflow.vivado-netlist-report/v1", "status": "pass",
        "provider": "compact-emuir-to-vivado-indexed-primitives-v1",
        "implementation_id": "native-vivado-export-v1",
        "top": mapped_report["top"],
        "source_instances": mapped_report["cells"],
        "emitted_instances": result["cells"],
        "emitted_inventory": mapped_report["audit"]["cell_types"],
        "output": str(output_path), "output_sha256": result["rtl_sha256"],
        "mapped_index": {"path": str(index_path), "sha256": mapped_report["index_sha256"]},
        "source_functions": {"path": str(functions_path), "sha256": file_sha256(functions_path)},
        "optimization_policy": LINEAGE_POLICY,
        "primitive_audit": mapped_report["audit"],
        "source_validation": source_validation,
    }
    if report_path is not None:
        write_json(report_path, report, compact=True)
    return report
