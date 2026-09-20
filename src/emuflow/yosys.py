from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterable, List, Mapping, Optional, Tuple

from .errors import ImportError
from .io import read_json
from .ir import EMUIR_SCHEMA, EmuIR
from .resources import ResourceVector, classify_primitive_resources


_CLOCK_NAME = re.compile(r"(^|[_/])(clk|clock)([_/]|$)", re.IGNORECASE)
_RESET_NAME = re.compile(r"(^|[_/])(rst|reset|aresetn?)([_/]|$)", re.IGNORECASE)
_SEQUENTIAL_TYPES = {"FDCE", "FDPE", "FDRE", "FDSE"}
_TRANSPORT_SAFE_SEQUENTIAL_INPUTS = {
    # FDRE/FDSE sample R/S on the active clock edge, so these synchronous
    # controls have the same paused-clock transport semantics as D and CE.
    # FDCE.CLR and FDPE.PRE are asynchronous and deliberately remain absent.
    "FDCE": {"D", "CE"},
    "FDPE": {"D", "CE"},
    "FDRE": {"D", "CE", "R"},
    "FDSE": {"D", "CE", "S"},
}
_VTR_RAM_INPUTS = {
    "VTR_SP_RAM": {"addr", "data", "we"},
    "VTR_DP_RAM": {"addr1", "addr2", "data1", "data2", "we1", "we2"},
}
_NON_HARDWARE_CELL_TYPES = {"$scopeinfo"}


def _is_transport_safe_sequential_input(
    cell_type: str,
    port: str,
) -> bool:
    if cell_type in _SEQUENTIAL_TYPES:
        return port in _TRANSPORT_SAFE_SEQUENTIAL_INPUTS[cell_type]
    if cell_type.startswith("$_DFF_"):
        return port == "D"
    return port in _VTR_RAM_INPUTS.get(cell_type, set())


def _select_top_module(
    modules: Mapping[str, Any], requested_top: Optional[str]
) -> Tuple[str, Mapping[str, Any]]:
    if requested_top is not None:
        if requested_top not in modules:
            raise ImportError(
                f"Yosys JSON does not contain requested top module {requested_top!r}"
            )
        module = modules[requested_top]
        if not isinstance(module, dict):
            raise ImportError(f"Yosys module {requested_top!r} is not an object")
        return requested_top, module

    marked = []
    for name, raw_module in modules.items():
        if not isinstance(raw_module, dict):
            continue
        attributes = raw_module.get("attributes", {})
        if isinstance(attributes, dict) and str(attributes.get("top", "0")) not in {
            "0",
            "",
        }:
            marked.append(name)
    if len(marked) == 1:
        return marked[0], modules[marked[0]]
    if len(modules) == 1:
        name = next(iter(modules))
        return name, modules[name]
    if not modules:
        raise ImportError("Yosys JSON contains no modules")
    raise ImportError(
        "unable to infer top module; pass --top (top-marked modules: "
        f"{marked})"
    )


def _endpoint(instance: Optional[str], port: str, bit: int) -> Dict[str, Any]:
    return {"instance": instance, "port": port, "bit": bit}


def _roles(direction: str, is_top_port: bool) -> Tuple[str, ...]:
    if is_top_port:
        if direction == "input":
            return ("driver",)
        if direction == "output":
            return ("sink",)
    else:
        if direction == "input":
            return ("sink",)
        if direction == "output":
            return ("driver",)
    if direction == "inout":
        return ("driver", "sink")
    return ("sink",)


def _canonical_net_name(
    bit: int, candidates: Iterable[Tuple[int, int, str, int, int]]
) -> Tuple[str, List[str], int]:
    choices = sorted(candidates)
    if not choices:
        return f"$bit_{bit}", [], 0
    _, _, name, bus_index, bus_width = choices[0]
    aliases = sorted({candidate[2] for candidate in choices})
    canonical = name if bus_width == 1 else f"{name}[{bus_index}]"
    return canonical, aliases, bus_index


def import_yosys_json(
    path: Path, top: Optional[str] = None, clocks: Iterable[str] = ()
) -> EmuIR:
    source = read_json(path)
    modules = source.get("modules")
    if not isinstance(modules, dict):
        raise ImportError(f"{path}: missing Yosys 'modules' object")
    top_name, module = _select_top_module(modules, top)

    raw_ports = module.get("ports", {})
    raw_cells = module.get("cells", {})
    raw_netnames = module.get("netnames", {})
    if not isinstance(raw_ports, dict):
        raise ImportError(f"{path}: module {top_name!r} ports must be an object")
    if not isinstance(raw_cells, dict):
        raise ImportError(f"{path}: module {top_name!r} cells must be an object")
    if not isinstance(raw_netnames, dict):
        raise ImportError(f"{path}: module {top_name!r} netnames must be an object")

    explicit_clocks = set(clocks)
    clock_ports = {
        name for name in raw_ports if name in explicit_clocks or _CLOCK_NAME.search(name)
    }
    reset_ports = {name for name in raw_ports if _RESET_NAME.search(name)}

    ports: List[Dict[str, Any]] = []
    instances: List[Dict[str, Any]] = []
    warnings: List[str] = []
    bit_endpoints: DefaultDict[
        int, Dict[str, List[Dict[str, Any]]]
    ] = defaultdict(lambda: {"drivers": [], "sinks": []})
    bit_names: DefaultDict[
        int, List[Tuple[int, int, str, int, int]]
    ] = defaultdict(list)
    instance_resources: Dict[str, ResourceVector] = {}
    instance_types: Dict[str, str] = {}

    for port_name, raw_port in sorted(raw_ports.items()):
        if not isinstance(raw_port, dict):
            raise ImportError(f"port {port_name!r}: expected an object")
        direction = raw_port.get("direction", "unknown")
        bits = raw_port.get("bits", [])
        if not isinstance(bits, list) or not bits:
            raise ImportError(f"port {port_name!r}: expected a non-empty bits array")
        constant_connections = [
            {"bit": bit_index, "value": bit.lower()}
            for bit_index, bit in enumerate(bits)
            if isinstance(bit, str)
        ]
        if constant_connections and direction not in {"output", "inout"}:
            raise ImportError(
                f"port {port_name!r}: input bits cannot be constants"
            )
        ports.append(
            {
                "id": port_name,
                "name": port_name,
                "direction": direction,
                "width": len(bits),
                "clock": port_name in clock_ports,
                "reset": port_name in reset_ports,
                "constant_connections": constant_connections,
            }
        )
        for bit_index, bit in enumerate(bits):
            if not isinstance(bit, int):
                continue
            for role in _roles(direction, is_top_port=True):
                bit_endpoints[bit][f"{role}s"].append(
                    _endpoint(None, port_name, bit_index)
                )

    for cell_name, raw_cell in sorted(raw_cells.items()):
        if not isinstance(raw_cell, dict):
            raise ImportError(f"cell {cell_name!r}: expected an object")
        cell_type = raw_cell.get("type")
        if not isinstance(cell_type, str) or not cell_type:
            raise ImportError(f"cell {cell_name!r}: missing type")
        # Yosys 0.57+ exports hierarchy provenance as pinless $scopeinfo
        # pseudo-cells by default. They carry no hardware behavior and must
        # not consume capacity or enter the physical instance inventory.
        if cell_type in _NON_HARDWARE_CELL_TYPES:
            continue
        resources = classify_primitive_resources(cell_type)
        instance_resources[cell_name] = resources
        instance_types[cell_name] = cell_type
        parameters = raw_cell.get("parameters", {})
        attributes = raw_cell.get("attributes", {})
        constant_connections = []
        raw_connections = raw_cell.get("connections", {})
        if isinstance(raw_connections, dict):
            for port_name, connection_bits in sorted(raw_connections.items()):
                if not isinstance(connection_bits, list):
                    continue
                for bit_index, bit_value in enumerate(connection_bits):
                    if isinstance(bit_value, str):
                        constant_connections.append(
                            {
                                "port": port_name,
                                "bit": bit_index,
                                "value": bit_value.lower(),
                            }
                        )
        instances.append(
            {
                "id": cell_name,
                "name": cell_name,
                "type": cell_type,
                "resources": resources.to_dict(),
                "parameters": parameters if isinstance(parameters, dict) else {},
                "attributes": attributes if isinstance(attributes, dict) else {},
                "constant_connections": constant_connections,
            }
        )

        port_directions = raw_cell.get("port_directions", {})
        connections = raw_cell.get("connections", {})
        if not isinstance(port_directions, dict):
            port_directions = {}
        if not isinstance(connections, dict):
            raise ImportError(f"cell {cell_name!r}: connections must be an object")
        for port_name, bits in sorted(connections.items()):
            if not isinstance(bits, list):
                raise ImportError(
                    f"cell {cell_name!r} port {port_name!r}: bits must be an array"
                )
            direction = port_directions.get(port_name, "unknown")
            if direction == "unknown":
                warnings.append(
                    f"cell {cell_name!r} port {port_name!r} has unknown direction"
                )
            for bit_index, bit in enumerate(bits):
                if not isinstance(bit, int):
                    continue
                for role in _roles(direction, is_top_port=False):
                    bit_endpoints[bit][f"{role}s"].append(
                        _endpoint(cell_name, port_name, bit_index)
                    )

    for net_name, raw_net in sorted(raw_netnames.items()):
        if not isinstance(raw_net, dict):
            continue
        bits = raw_net.get("bits", [])
        if not isinstance(bits, list):
            continue
        hidden = int(bool(raw_net.get("hide_name", 0)))
        for bus_index, bit in enumerate(bits):
            if isinstance(bit, int):
                bit_names[bit].append(
                    (hidden, len(net_name), net_name, bus_index, len(bits))
                )

    nets: List[Dict[str, Any]] = []
    seen_net_ids = set()
    for bit in sorted(bit_endpoints):
        endpoints = bit_endpoints[bit]
        if not endpoints["drivers"] and not endpoints["sinks"]:
            continue
        name, aliases, bus_index = _canonical_net_name(bit, bit_names[bit])
        net_id = name
        if net_id in seen_net_ids:
            net_id = f"{name}#{bit}"
        seen_net_ids.add(net_id)

        driver_resources = []
        for endpoint_value in endpoints["drivers"]:
            instance = endpoint_value["instance"]
            if instance is not None:
                driver_resources.append(instance_resources[instance])

        if any(alias in clock_ports for alias in aliases) or name in clock_ports:
            cut_class = "clock"
        elif any(alias in reset_ports for alias in aliases) or name in reset_ports:
            cut_class = "reset"
        elif len(endpoints["drivers"]) > 1:
            cut_class = "multi_driver"
            warnings.append(
                f"net {net_id!r} has {len(endpoints['drivers'])} drivers"
            )
        elif not endpoints["drivers"]:
            cut_class = "undriven"
        elif endpoints["drivers"][0]["instance"] is None:
            cut_class = "primary_input"
        elif driver_resources and (
            driver_resources[0].ff or driver_resources[0].bram
        ):
            cut_class = "register_output"
        elif endpoints["sinks"] and all(
            endpoint["instance"] is not None
            and _is_transport_safe_sequential_input(
                instance_types[endpoint["instance"]],
                endpoint["port"],
            )
            for endpoint in endpoints["sinks"]
        ):
            # Synchronous data/control inputs are sampled only at the virtual
            # DUT edge. They can be transported during round 1 of the
            # paused-clock frame, after all remote sequential outputs arrive
            # in round 0. Clock and asynchronous controls remain forbidden.
            cut_class = "register_input"
        else:
            cut_class = "combinational"

        nets.append(
            {
                "id": net_id,
                "name": name,
                "yosys_bit": bit,
                "bus_index": bus_index,
                "aliases": aliases,
                "drivers": endpoints["drivers"],
                "sinks": endpoints["sinks"],
                "fanout": len(endpoints["sinks"]),
                "cut_class": cut_class,
            }
        )

    clocks_ir = [
        {
            "id": port_name,
            "name": port_name,
            "source_port": port_name,
            "period_ns": None,
        }
        for port_name in sorted(clock_ports)
    ]

    emuir = {
        "schema": EMUIR_SCHEMA,
        "design": {
            "name": top_name,
            "top": top_name,
            "source_format": "yosys-json",
            "source": str(path),
            "creator": source.get("creator", "unknown"),
        },
        "ports": ports,
        "instances": instances,
        "nets": nets,
        "clocks": clocks_ir,
        "warnings": sorted(set(warnings)),
    }
    return EmuIR(emuir)
