"""Lower provider-neutral mapped EmuIR into Vivado primitive Verilog."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .equivalence import _lut_definition
from .errors import ValidationError
from .ir import EmuIR
from .xilinx_primitives import load_xilinx_primitive_library


_NATIVE_FFS = {"FDCE", "FDPE", "FDRE", "FDSE"}
_VTR_HARD_MACROS = {"VTR_MULTIPLY", "VTR_SP_RAM", "VTR_DP_RAM"}



def lower_vivado_primitives(ir: EmuIR) -> EmuIR:
    value = deepcopy(ir.value)
    instances = {item["id"]: item for item in value["instances"]}
    pin_remap: Dict[tuple[str, str, int], tuple[str, int]] = {}
    _library, library_contract = load_xilinx_primitive_library()
    native_xilinx_cells = set(library_contract["cells"])
    unsupported = []
    for instance in value["instances"]:
        instance_id = instance["id"]
        cell_type = instance["type"]
        if cell_type in native_xilinx_cells:
            continue
        if cell_type.startswith("LUT") or cell_type in {"$lut", "$_LUT_"}:
            width, input_port, output_port, truth = _lut_definition(instance)
            if width < 1 or width > 6:
                raise ValidationError(
                    f"Vivado LUT {instance_id!r} has unsupported width {width}"
                )
            instance["type"] = f"LUT{width}"
            instance["parameters"] = {
                "INIT": format(truth, f"0{1 << width}b")
            }
            for bit in range(width):
                source_bit = 0 if input_port == "I" else bit
                source_port = f"I{bit}" if input_port == "I" else input_port
                pin_remap[(instance_id, source_port, source_bit)] = (
                    f"I{bit}",
                    0,
                )
            pin_remap[(instance_id, output_port, 0)] = ("O", 0)
        elif cell_type in {"$_DFF_P_", "$_DFF_N_"}:
            instance["type"] = "FDRE"
            instance["parameters"] = {
                "INIT": "0",
                "IS_C_INVERTED": "1" if cell_type == "$_DFF_N_" else "0",
            }
            constants = instance.setdefault("constant_connections", [])
            constants.extend(
                (
                    {"port": "CE", "bit": 0, "value": "1"},
                    {"port": "R", "bit": 0, "value": "0"},
                )
            )
        elif (
            cell_type in _NATIVE_FFS
            or cell_type in _VTR_HARD_MACROS
        ):
            continue
        else:
            unsupported.append(cell_type)
    if unsupported:
        raise ValidationError(
            "Vivado backend does not yet lower mapped primitive types: "
            + ", ".join(sorted(set(unsupported)))
        )

    for net in value["nets"]:
        for collection in ("drivers", "sinks"):
            for endpoint in net[collection]:
                instance_id = endpoint["instance"]
                if instance_id is None:
                    continue
                remapped = pin_remap.get(
                    (instance_id, endpoint["port"], endpoint["bit"])
                )
                if remapped is not None:
                    endpoint["port"], endpoint["bit"] = remapped
    for instance_id, instance in instances.items():
        for connection in instance.get("constant_connections", []):
            remapped = pin_remap.get(
                (instance_id, connection["port"], connection["bit"])
            )
            if remapped is not None:
                connection["port"], connection["bit"] = remapped
    return EmuIR(value)


def emit_vivado_mapped_verilog(
    ir_path: Path,
    output_path: Path,
    report_path: Optional[Path] = None,
) -> Mapping[str, Any]:
    """Export the current indexed Xilinx input; no whole-EmuIR fallback."""
    from .emuir_compact import is_compact_emuir
    from .emuir_storage import is_emuir_sqlite
    from .vivado_indexed_netlist import emit_indexed_vivado_verilog

    if not is_emuir_sqlite(ir_path) or not is_compact_emuir(ir_path):
        raise ValidationError("Vivado export requires current indexed EmuIR")
    return emit_indexed_vivado_verilog(ir_path, output_path, report_path)
