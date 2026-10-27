"""Connect the current Phase-6 fixed LVDS adapter to a mapped partition."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from emuflow.common.errors import ValidationError
from emuflow.ir.indexed_physical_ir import IndexedPhysicalIR
from emuflow.common.io import file_sha256
from emuflow.frontend.verilog import _identifier


def write_vivado_physical_top(
    ir_path: Path, output_path: Path, contract: Mapping[str, Any],
) -> dict[str, Any]:
    if contract.get("status") != "pass":
        raise ValidationError("Vivado virtual I/O contract did not pass")
    for owner in ("xdc", "adapter"):
        artifact = contract[owner]
        path = Path(artifact["path"])
        if not path.is_file() or file_sha256(path) != artifact["sha256"]:
            raise ValidationError(f"Vivado virtual I/O {owner} source seal disagrees")
    with IndexedPhysicalIR(ir_path) as ir:
        source_top = ir.value["design"]["top"]
        ports = {port["id"]: port for port in ir.value["ports"]}
    buses = {}
    physical = {}
    used = set()
    for entry in contract["entries"]:
        bus, bit = entry["logical_port"], entry["logical_bit"]
        width = entry["logical_port_width"]
        if entry["direction"] not in {"tx", "rx"}:
            raise ValidationError("Vivado fixed I/O direction is invalid")
        direction = "output" if entry["direction"] == "tx" else "input"
        port = ports.get(bus)
        if (port is None or port["width"] != width or port["direction"] != direction
                or not 0 <= bit < width or (bus, bit) in used):
            raise ValidationError("Vivado fixed I/O logical bus binding is invalid")
        used.add((bus, bit))
        buses[bus] = width
        for polarity in ("p", "n"):
            name = entry[polarity]["port"]
            if name in physical or name in ports:
                raise ValidationError("Vivado fixed I/O physical port is duplicated")
            physical[name] = direction
    if len(physical) != contract["metrics"]["physical_ports"]:
        raise ValidationError("Vivado fixed I/O physical port count disagrees")
    top = "__emuflow_physical_top"
    if source_top == top:
        raise ValidationError("Vivado physical top collides with partition top")
    declarations = []
    for name, port in ports.items():
        if name in buses:
            continue
        dimension = f"[{port['width'] - 1}:0] " if port["width"] != 1 else ""
        declarations.append(f"  {port['direction']} wire {dimension}{_identifier(name)}")
    declarations.extend(f"  {direction} wire {_identifier(name)}" for name, direction in physical.items())
    lines = [f"module {top} (", ",\n".join(declarations), ");"]
    lines.extend(f"  wire [{width - 1}:0] {_identifier(bus)};" for bus, width in buses.items())
    lines.extend([
        f"  {_identifier(source_top)} mapped_partition (",
        ",\n".join(f"    .{_identifier(name)}({_identifier(name)})" for name in ports),
        "  );",
        f"  {_identifier(contract['adapter']['module'])} virtual_io (",
        ",\n".join(f"    .{_identifier(name)}({_identifier(name)})" for name in (*buses, *physical)),
        "  );", "endmodule", "",
    ])
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return {"status": "pass", "top": top, "hierarchy_prefix": "mapped_partition",
            "output": str(output_path), "differential_pairs": len(contract["entries"]),
            "physical_ports": len(physical)}
