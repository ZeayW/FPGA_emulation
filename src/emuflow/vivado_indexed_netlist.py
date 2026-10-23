"""Stream current Phase-7 compact EmuIR into native Vivado primitive RTL.

The mapped SQLite index owns cell/pin identities.  Only one primitive and its
pins are reconstructed at a time; no complete Python circuit or JSON export is
created.  Net ordinals deliberately agree with the physical timing queries.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Mapping

from .errors import ValidationError
from .io import file_sha256, write_json
from .verilog import _emittable_parameters, _identifier, _parameter


def emit_indexed_vivado_verilog(
    ir_path: Path, output_path: Path, report_path: Path | None = None,
) -> Mapping[str, Any]:
    # Lazy import avoids the legacy lowerer's import cycle.
    from .xilinx_netlist import emit_xilinx_mapped_index, open_xilinx_mapped_index
    from .vivado_source import validate_vivado_source

    output_path.parent.mkdir(parents=True, exist_ok=True)
    source_validation = validate_vivado_source(ir_path, output_path.with_suffix(".source-check.json"))
    index_path = output_path.with_suffix(".mapped.sqlite")
    mapped_report = emit_xilinx_mapped_index(ir_path, index_path)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")

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
            for name, cell in mapped.cells.items():
                parameters = _emittable_parameters(cell)
                parameter_text = ""
                if parameters:
                    parameter_text = " #(\n" + ",\n".join(
                        f"    .{_identifier(key)}({_parameter(value)})"
                        for key, value in sorted(parameters.items())
                    ) + "\n  )"
                # Stable primitive identity is required by source-bound timing
                # and cell accounting. Added clock/I/O cells are untagged.
                line('  (* KEEP = "yes", DONT_TOUCH = "yes", EMUFLOW_MAPPED = "yes" *)')
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
    finally:
        temporary.unlink(missing_ok=True)
    report = {
        "schema": "emuflow.vivado-netlist-report/v1", "status": "pass",
        "provider": "compact-emuir-to-vivado-indexed-primitives-v1",
        "top": mapped_report["top"],
        "source_instances": mapped_report["cells"],
        "emitted_instances": mapped_report["cells"],
        "emitted_inventory": mapped_report["audit"]["cell_types"],
        "output": str(output_path), "output_sha256": file_sha256(output_path),
        "mapped_index": {"path": str(index_path), "sha256": mapped_report["index_sha256"]},
        "primitive_audit": mapped_report["audit"],
        "source_validation": source_validation,
    }
    if report_path is not None:
        write_json(report_path, report, compact=True)
    return report
