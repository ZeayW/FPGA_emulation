"""Lower provider-neutral mapped EmuIR into Vivado primitive Verilog."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .equivalence import _lut_definition
from .errors import ValidationError
from .io import write_json
from .ir import EmuIR
from .xilinx_primitives import load_xilinx_primitive_library
from .verilog import mapped_verilog


VIVADO_NETLIST_REPORT_SCHEMA = "emuflow.vivado-netlist-report/v1"
_NATIVE_FFS = {"FDCE", "FDPE", "FDRE", "FDSE"}
_VTR_HARD_MACROS = {"VTR_MULTIPLY", "VTR_SP_RAM", "VTR_DP_RAM"}


_VIVADO_HARD_MACRO_MODELS = r'''
(* KEEP_HIERARCHY = "yes" *)
module VTR_MULTIPLY #(
  parameter integer A_WIDTH = 1,
  parameter integer B_WIDTH = 1,
  parameter integer Y_WIDTH = A_WIDTH + B_WIDTH
) (
  input  wire [A_WIDTH-1:0] a,
  input  wire [B_WIDTH-1:0] b,
  output wire [Y_WIDTH-1:0] out
);
  (* use_dsp = "yes" *) wire [Y_WIDTH-1:0] product = a * b;
  assign out = product;
endmodule

(* KEEP_HIERARCHY = "yes" *)
module VTR_SP_RAM #(
  parameter integer ADDR_WIDTH = 10,
  parameter integer DATA_WIDTH = 32,
  parameter integer DEPTH = (1 << ADDR_WIDTH),
  parameter READ_DURING_WRITE = "old"
) (
  input  wire                  clk,
  input  wire [ADDR_WIDTH-1:0] addr,
  input  wire [DATA_WIDTH-1:0] data,
  input  wire                  we,
  output reg  [DATA_WIDTH-1:0] out
);
  (* ram_style = "block" *) reg [DATA_WIDTH-1:0] memory [0:DEPTH-1];
  always @(posedge clk) begin
    if (we)
      memory[addr] <= data;
    out <= memory[addr];
  end
endmodule

(* KEEP_HIERARCHY = "yes" *)
module VTR_DP_RAM #(
  parameter integer ADDR_WIDTH = 10,
  parameter integer DATA_WIDTH = 32,
  parameter integer DEPTH = (1 << ADDR_WIDTH),
  parameter READ_DURING_WRITE = "old"
) (
  input  wire                  clk,
  input  wire [ADDR_WIDTH-1:0] addr1,
  input  wire [ADDR_WIDTH-1:0] addr2,
  input  wire [DATA_WIDTH-1:0] data1,
  input  wire [DATA_WIDTH-1:0] data2,
  input  wire                  we1,
  input  wire                  we2,
  output reg  [DATA_WIDTH-1:0] out1,
  output reg  [DATA_WIDTH-1:0] out2
);
  (* ram_style = "block" *) reg [DATA_WIDTH-1:0] memory [0:DEPTH-1];
  always @(posedge clk) begin
    if (we1)
      memory[addr1] <= data1;
    out1 <= memory[addr1];
  end
  always @(posedge clk) begin
    if (we2)
      memory[addr2] <= data2;
    out2 <= memory[addr2];
  end
endmodule
'''.lstrip()


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
            or cell_type in native_xilinx_cells
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
    source = EmuIR.load(ir_path)
    lowered = lower_vivado_primitives(source)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    source_inventory = Counter(
        item["type"] for item in source.value["instances"]
    )
    output_inventory = Counter(
        item["type"] for item in lowered.value["instances"]
    )
    netlist = mapped_verilog(
        lowered,
        allow_bus_pins=True,
        synthesized_macro_types=_VTR_HARD_MACROS,
    )
    hard_macros = sorted(
        set(output_inventory) & _VTR_HARD_MACROS
    )
    if hard_macros:
        netlist += "\n" + _VIVADO_HARD_MACRO_MODELS
    output_path.write_text(netlist, encoding="utf-8")
    report = {
        "schema": VIVADO_NETLIST_REPORT_SCHEMA,
        "status": "pass",
        "provider": "emuflow-vivado-primitive-lowering-v1",
        "design": source.value["design"]["name"],
        "top": source.value["design"]["top"],
        "source_instances": len(source.value["instances"]),
        "emitted_instances": len(lowered.value["instances"]),
        "source_inventory": dict(sorted(source_inventory.items())),
        "emitted_inventory": dict(sorted(output_inventory.items())),
        "hard_macro_models": hard_macros,
        "output": str(output_path),
    }
    if report_path is not None:
        write_json(report_path, report)
    return report
