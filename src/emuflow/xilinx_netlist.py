"""Provider-neutral EmuIR to normalized Xilinx Yosys JSON lowering."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

from .errors import ValidationError
from .io import write_json
from .ir import EmuIR
from .vivado_netlist import lower_vivado_primitives
from .xilinx_primitives import (
    XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
    audit_xilinx_mapped_json,
)


XILINX_MAPPED_NETLIST_REPORT_SCHEMA = (
    "emuflow.xilinx-mapped-netlist-report/v1"
)


def _pin_inventory(ir: EmuIR) -> Tuple[
    Dict[str, Dict[str, Dict[int, str]]],
    Dict[Tuple[str, str, int], str],
]:
    pins: Dict[str, Dict[str, Dict[int, str]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    constants: Dict[Tuple[str, str, int], str] = {}
    for net in ir.value["nets"]:
        for collection, direction in (("drivers", "output"), ("sinks", "input")):
            for endpoint in net[collection]:
                instance = endpoint["instance"]
                if instance is None:
                    continue
                port = endpoint["port"]
                bit = endpoint["bit"]
                previous = pins[instance][port].setdefault(bit, direction)
                if previous != direction:
                    pins[instance][port][bit] = "inout"
    for instance in ir.value["instances"]:
        for connection in instance.get("constant_connections", []):
            key = (instance["id"], connection["port"], connection["bit"])
            if key in constants:
                raise ValidationError(
                    f"Xilinx mapped netlist repeats constant pin {key!r}"
                )
            constants[key] = connection["value"]
            pins[instance["id"]][connection["port"]].setdefault(
                connection["bit"], "input"
            )
    return pins, constants


def emit_xilinx_mapped_json(
    ir_path: Path,
    output_path: Path,
    report_path: Path | None = None,
) -> Mapping[str, Any]:
    """Emit a normalized, primitive-only Yosys JSON netlist.

    This is the inverse provider boundary of :func:`import_yosys_json`; it
    does not resynthesize or reinterpret the design.  It preserves each
    EmuIR net and cell identity while lowering only provider-neutral LUT/FF
    spellings into the explicit UltraScale+ primitive namespace.
    """

    source = EmuIR.load(ir_path)
    ir = lower_vivado_primitives(source)
    pin_inventory, constants = _pin_inventory(ir)
    net_bits = {
        net["id"]: index + 2 for index, net in enumerate(ir.value["nets"])
    }
    pin_bits: Dict[Tuple[str, str, int], Any] = {}
    for net in ir.value["nets"]:
        value = net_bits[net["id"]]
        for collection in ("drivers", "sinks"):
            for endpoint in net[collection]:
                if endpoint["instance"] is not None:
                    pin_bits[
                        (
                            endpoint["instance"], endpoint["port"],
                            endpoint["bit"],
                        )
                    ] = value

    cells: Dict[str, Any] = {}
    for instance in sorted(ir.value["instances"], key=lambda item: item["id"]):
        name = instance["id"]
        ports: Dict[str, Any] = {}
        directions: Dict[str, str] = {}
        for port, bits in sorted(pin_inventory.get(name, {}).items()):
            width = max(bits) + 1
            if set(bits) != set(range(width)):
                raise ValidationError(
                    f"Xilinx mapped cell {name!r}.{port} is not contiguous"
                )
            values = []
            for bit in range(width):
                key = (name, port, bit)
                if key in pin_bits:
                    values.append(pin_bits[key])
                elif key in constants:
                    values.append(constants[key])
                else:
                    raise ValidationError(
                        f"Xilinx mapped cell pin {key!r} is unconnected"
                    )
            ports[port] = values
            observed = set(bits.values())
            directions[port] = (
                "inout" if "inout" in observed or len(observed) > 1
                else next(iter(observed))
            )
        cells[name] = {
            "hide_name": 0,
            "type": instance["type"],
            "parameters": dict(instance.get("parameters", {})),
            "attributes": dict(instance.get("attributes", {})),
            "port_directions": directions,
            "connections": ports,
        }

    top_port_bits: Dict[Tuple[str, int], int] = {}
    for net in ir.value["nets"]:
        value = net_bits[net["id"]]
        for collection in ("drivers", "sinks"):
            for endpoint in net[collection]:
                if endpoint["instance"] is None:
                    key = (endpoint["port"], endpoint["bit"])
                    previous = top_port_bits.setdefault(key, value)
                    if previous != value:
                        raise ValidationError(
                            f"Xilinx mapped top pin {key!r} has multiple nets"
                        )
    ports = {}
    for port in ir.value["ports"]:
        bits = []
        for bit in range(port["width"]):
            key = (port["id"], bit)
            if key not in top_port_bits:
                raise ValidationError(
                    f"Xilinx mapped top pin {key!r} is unconnected"
                )
            bits.append(top_port_bits[key])
        ports[port["id"]] = {
            "direction": port["direction"],
            "bits": bits,
        }
    netnames = {
        net["id"]: {
            "hide_name": 0,
            "bits": [net_bits[net["id"]]],
            "attributes": {},
        }
        for net in ir.value["nets"]
    }
    top = ir.value["design"]["top"]
    mapped = {
        "creator": "EmuFlow Xilinx mapped-netlist exporter",
        "modules": {
            top: {
                "attributes": {"top": "1"},
                "parameter_default_values": {},
                "ports": ports,
                "cells": cells,
                "memories": {},
                "netnames": netnames,
            }
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_path, mapped, compact=True)
    audit = audit_xilinx_mapped_json(output_path, top=top)
    report = {
        "schema": XILINX_MAPPED_NETLIST_REPORT_SCHEMA,
        "status": "pass",
        "provider": "emuflow-emuir-to-xilinx-yosys-json-v1",
        "mapping_profile": XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
        "top": top,
        "cells": len(cells),
        "nets": len(netnames),
        "ports": len(ports),
        "output": str(output_path),
        "audit": audit,
    }
    if report_path is not None:
        write_json(report_path, report)
    return report
