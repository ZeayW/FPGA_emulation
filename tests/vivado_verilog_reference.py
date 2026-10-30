"""Independent small-fixture RTL/signature oracle; never installed or selected by production."""
from __future__ import annotations

import os
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Mapping

from emuflow.common.errors import ValidationError
from emuflow.storage.store_publication import (
    bind_store_source,
    publish_sqlite_store,
    validate_store_destination,
)
from emuflow.common.json_io import file_sha256, write_json
from emuflow_tools.frontend.verilog import _emittable_parameters, _identifier, _parameter
from emuflow_tools.physical.vivado.lineage_contract import LINEAGE_POLICY


def primitive_function_signature(cell: Mapping[str, Any]) -> str:
    """Only identical primitives, parameters and input bits can alias here.

    Outputs have independent net names, but must have identical pin interfaces.
    This is deliberately not a heuristic equivalence or a retiming proof.
    """
    value = {"type": cell["type"], "parameters": cell.get("parameters", {}),
             "pins": {port: {"direction": direction,
                              "bits": cell["connections"][port] if direction != "output"
                              else len(cell["connections"][port])}
                      for port, direction in cell["port_directions"].items()}}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def emit_indexed_vivado_verilog_reference(
    ir_path: Path, output_path: Path, report_path: Path | None = None, *, mapped_index=None,
) -> Mapping[str, Any]:
    # Lazy import avoids the legacy lowerer's import cycle.
    from emuflow_tools.physical.xilinx_netlist import emit_xilinx_mapped_index, open_xilinx_mapped_index
    from emuflow_tools.physical.vivado.vivado_source import validate_vivado_source

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if mapped_index is None:
        source_validation = validate_vivado_source(ir_path, output_path.with_suffix(".source-check.json"))
        index_path = output_path.with_suffix(".mapped.sqlite")
        mapped_report = emit_xilinx_mapped_index(ir_path, index_path)
    else:
        index_path = mapped_index
        with open_xilinx_mapped_index(index_path) as value:
            mapped_report = {"top": value.top, "cells": len(value.cells), "index_sha256": file_sha256(index_path),
                             "audit": {"cell_types": {}}}
        source_validation = {}
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    functions_path = output_path.with_suffix(".functions.sqlite")
    functions_temporary = temporary.with_suffix(".sqlite")
    source_bindings = validate_store_destination(
        functions_path, (bind_store_source(index_path, "xilinx-mapped-index-v2"),)
    )
    functions = sqlite3.connect(functions_temporary)
    functions.executescript("PRAGMA application_id=0x454D5646; PRAGMA user_version=1;")
    functions.execute("CREATE TABLE signatures(seq INTEGER PRIMARY KEY,signature TEXT NOT NULL)")
    functions.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    functions.execute("INSERT INTO metadata VALUES ('schema','emuflow.vivado-source-functions/v1')")
    functions.execute("INSERT INTO metadata VALUES ('mapped_index_sha256',?)", (mapped_report["index_sha256"],))

    def bit(value: Any) -> str:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 2:
            return f"__emuflow_net_{value - 2}"
        if isinstance(value, str) and value.lower() in {"0", "1", "x", "z"}:
            return f"1'b{value.lower()}"
        raise ValidationError("Vivado mapped pin has an invalid bit value")

    try:
        with open_xilinx_mapped_index(index_path) as mapped, temporary.open(
            "w", encoding="utf-8"
        ) as stream:
            def line(value: str) -> None:
                stream.write(value + "\n")

            line("`default_nettype none")
            line(f"module {_identifier(mapped.top)}(")
            line(",\n".join("  " + _identifier(name) for name in mapped.ports))
            line(");")
            for name, port in mapped.ports.items():
                width = len(port["bits"])
                dimension = f"[{width - 1}:0] " if width != 1 else ""
                if port["direction"] not in {"input", "output", "inout"}:
                    raise ValidationError("Vivado top port direction is invalid")
                line(f"  {port['direction']} wire {dimension}{_identifier(name)};")
            # Preserve actual source ordinals, including one-based/sparse SQL
            # keys. Extra IDs belong to disconnected input ports.
            largest_net_bit = 1
            for row in mapped.connection.execute("SELECT seq FROM netnames ORDER BY seq"):
                largest_net_bit = int(row[0]) + 2
                line(f"  wire __emuflow_net_{int(row[0])};")
            for name, port in mapped.ports.items():
                for index, value in enumerate(port["bits"]):
                    if isinstance(value, int) and value > largest_net_bit:
                        line(f"  wire {bit(value)};")
                    target = _identifier(name)
                    if len(port["bits"]) != 1:
                        target += f"[{index}]"
                    if port["direction"] == "input":
                        if isinstance(value, int):
                            line(f"  assign {bit(value)} = {target};")
                    elif port["direction"] == "output":
                        line(f"  assign {target} = {bit(value)};")
                    else:
                        raise ValidationError("Vivado indexed top-level inout is unsupported")
            source_ordinals = iter(mapped.connection.execute("SELECT id,seq FROM cells ORDER BY seq"))
            for name, cell in mapped.cells.items():
                source_name, source_seq = next(source_ordinals)
                if source_name != name:
                    raise ValidationError("Vivado source ordinal stream disagrees")
                functions.execute("INSERT INTO signatures VALUES (?,?)", (source_seq, primitive_function_signature(cell)))
                parameters = _emittable_parameters(cell)
                parameter_text = ""
                if parameters:
                    parameter_text = " #(\n" + ",\n".join(
                        f"    .{_identifier(key)}({_parameter(value)})"
                        for key, value in sorted(parameters.items())
                    ) + "\n  )"
                # Metadata tracks surviving/replicated objects; it must never
                # constrain synthesis or physical optimization.
                line(f'  (* EMUFLOW_MAPPED = "yes", EMUFLOW_SOURCE = "s{source_seq}" *)')
                line(f"  {_identifier(cell['type'])}{parameter_text} {_identifier(name)}(")
                connections = []
                for port, values in sorted(cell["connections"].items()):
                    expression = bit(values[0]) if len(values) == 1 else (
                        "{" + ", ".join(bit(value) for value in reversed(values)) + "}"
                    )
                    connections.append(f"    .{_identifier(port)}({expression})")
                line(",\n".join(connections))
                line("  );")
            line("endmodule")
            line("`default_nettype wire")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output_path)
        functions.execute("CREATE INDEX signatures_function ON signatures(signature,seq)")
        functions.commit()
        functions.close()
        publish_sqlite_store(
            functions_temporary,
            functions_path,
            "vivado-source-functions-v1",
            sources=source_bindings,
        )
    finally:
        functions.close()
        temporary.unlink(missing_ok=True)
        functions_temporary.unlink(missing_ok=True)
    report = {
        "schema": "emuflow.vivado-netlist-report/v1", "status": "pass",
        "provider": "compact-emuir-to-vivado-indexed-primitives-v1",
        "top": mapped_report["top"],
        "source_instances": mapped_report["cells"],
        "emitted_instances": mapped_report["cells"],
        "emitted_inventory": mapped_report["audit"]["cell_types"],
        "output": str(output_path), "output_sha256": file_sha256(output_path),
        "mapped_index": {"path": str(index_path), "sha256": mapped_report["index_sha256"]},
        "source_functions": {"path": str(functions_path), "sha256": file_sha256(functions_path)},
        "optimization_policy": LINEAGE_POLICY,
        "primitive_audit": mapped_report["audit"],
        "source_validation": source_validation,
    }
    if report_path is not None:
        write_json(report_path, report, compact=True)
    return report
