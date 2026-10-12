from __future__ import annotations

import json
import gzip
import re
import sqlite3
import tempfile
from collections import defaultdict
from contextlib import nullcontext
from itertools import groupby
from pathlib import Path
from typing import (
    Any,
    Callable,
    DefaultDict,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    TextIO,
    Tuple,
)

from .errors import ImportError
from .emuir_storage import write_emuir_sqlite
from .io import read_json
from .ir import EMUIR_SCHEMA, EmuIR
from .resources import ResourceVector, classify_primitive_resources
from .streaming_json import StreamingJSONObjectReader


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
    path: Path,
    top: Optional[str] = None,
    clocks: Iterable[str] = (),
    *,
    _source_value: Optional[Mapping[str, Any]] = None,
) -> EmuIR:
    source = read_json(path) if _source_value is None else _source_value
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


def _indexed_endpoint_groups(
    connection: sqlite3.Connection,
) -> Iterator[Tuple[int, List[sqlite3.Row]]]:
    rows = connection.execute(
        """
        SELECT e.yosys_bit, e.role, e.seq, e.instance_id, e.port,
               e.endpoint_bit, i.type AS instance_type,
               i.ff AS instance_ff, i.bram AS instance_bram
          FROM staging_endpoints AS e
          LEFT JOIN staging_instances AS i ON i.id = e.instance_id
         ORDER BY e.yosys_bit, e.role, e.seq
        """
    )
    for bit, grouped in groupby(rows, key=lambda row: row["yosys_bit"]):
        yield int(bit), list(grouped)


def _indexed_name_groups(
    connection: sqlite3.Connection,
) -> Iterator[Tuple[int, List[sqlite3.Row]]]:
    rows = connection.execute(
        """
        SELECT yosys_bit, hidden, name_length, name, bus_index, bus_width
          FROM staging_names
         ORDER BY yosys_bit, hidden, name_length, name, bus_index, bus_width
        """
    )
    for bit, grouped in groupby(rows, key=lambda row: row["yosys_bit"]):
        yield int(bit), list(grouped)


def _advance_name_group(
    iterator: Iterator[Tuple[int, List[sqlite3.Row]]],
    current: Optional[Tuple[int, List[sqlite3.Row]]],
    bit: int,
) -> Optional[Tuple[int, List[sqlite3.Row]]]:
    while current is not None and current[0] < bit:
        current = next(iterator, None)
    return current


def _stream_yosys_module_records(
    path: Path,
    top: str,
    *,
    on_cell: Any,
    on_netname: Any,
    source_stream: Optional[TextIO] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Stream one flattened Yosys module as bounded leaf records."""

    stream_context = (
        nullcontext(source_stream)
        if source_stream is not None
        else (
            gzip.open(path, "rt", encoding="utf-8")
            if path.suffix == ".gz"
            else path.open("r", encoding="utf-8")
        )
    )
    creator = "unknown"
    ports: Dict[str, Any] = {}
    found_top = False
    with stream_context as stream:
        reader = StreamingJSONObjectReader(stream)
        for root_key in reader.object_keys():
            if root_key == "creator":
                value = reader.read_value()
                creator = value if isinstance(value, str) else "unknown"
            elif root_key == "modules":
                for module_name in reader.object_keys():
                    if module_name != top:
                        reader.skip_value()
                        continue
                    if found_top:
                        raise ImportError(
                            f"{path}: duplicate Yosys top module {top!r}"
                        )
                    found_top = True
                    for module_key in reader.object_keys():
                        if module_key == "ports":
                            for name in reader.object_keys():
                                value = reader.read_value()
                                if not isinstance(value, dict):
                                    raise ImportError(
                                        f"port {name!r}: expected an object"
                                    )
                                ports[name] = value
                        elif module_key == "cells":
                            for name in reader.object_keys():
                                value = reader.read_value()
                                if not isinstance(value, dict):
                                    raise ImportError(
                                        f"cell {name!r}: expected an object"
                                    )
                                on_cell(name, value)
                        elif module_key == "netnames":
                            for name in reader.object_keys():
                                value = reader.read_value()
                                if not isinstance(value, dict):
                                    raise ImportError(
                                        f"net {name!r}: expected an object"
                                    )
                                on_netname(name, value)
                        else:
                            reader.skip_value()
            else:
                reader.skip_value()
        reader.finish()
    if not found_top:
        raise ImportError(f"{path}: missing Yosys top module {top!r}")
    return creator, ports


def _yosys_attribute_enabled(value: Any) -> bool:
    """Interpret the scalar encodings used by Yosys JSON attributes."""

    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    if normalized in {"", "0", "false", "no"}:
        return False
    return any(character == "1" for character in normalized) or normalized in {
        "true",
        "yes",
    }


def _stream_yosys_hierarchy_to_staging(
    path: Path,
    top: str,
    connection: sqlite3.Connection,
    *,
    source_stream: Optional[TextIO] = None,
) -> str:
    """Store hierarchical module definitions as bounded SQLite records.

    The complete Yosys document is never materialized as a Python object or a
    temporary JSON file.  Each module port, cell, and visible net name is
    written once; repeated module instances are expanded only by the indexed
    importer below.
    """

    connection.executescript(
        """
        CREATE TABLE staging_module_defs (
            name TEXT PRIMARY KEY,
            attributes_json TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE staging_module_ports (
            module TEXT NOT NULL,
            name TEXT NOT NULL,
            record_json TEXT NOT NULL,
            PRIMARY KEY (module, name)
        ) WITHOUT ROWID;
        CREATE TABLE staging_module_cells (
            module TEXT NOT NULL,
            name TEXT NOT NULL,
            record_json TEXT NOT NULL,
            PRIMARY KEY (module, name)
        ) WITHOUT ROWID;
        CREATE TABLE staging_module_names (
            module TEXT NOT NULL,
            name TEXT NOT NULL,
            record_json TEXT NOT NULL,
            PRIMARY KEY (module, name)
        ) WITHOUT ROWID;
        """
    )
    stream_context = (
        nullcontext(source_stream)
        if source_stream is not None
        else (
            gzip.open(path, "rt", encoding="utf-8")
            if path.suffix == ".gz"
            else path.open("r", encoding="utf-8")
        )
    )
    creator = "unknown"
    found_top = False
    with stream_context as stream:
        reader = StreamingJSONObjectReader(stream)
        for root_key in reader.object_keys():
            if root_key == "creator":
                value = reader.read_value()
                creator = value if isinstance(value, str) else "unknown"
                continue
            if root_key != "modules":
                reader.skip_value()
                continue
            for module_name in reader.object_keys():
                if module_name == top:
                    found_top = True
                connection.execute(
                    "INSERT INTO staging_module_defs VALUES (?, ?)",
                    (module_name, "{}"),
                )
                for module_key in reader.object_keys():
                    if module_key == "attributes":
                        value = reader.read_value()
                        if not isinstance(value, dict):
                            raise ImportError(
                                f"module {module_name!r}: attributes must be an object"
                            )
                        connection.execute(
                            "UPDATE staging_module_defs SET attributes_json = ? "
                            "WHERE name = ?",
                            (
                                json.dumps(
                                    value,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                    sort_keys=True,
                                ),
                                module_name,
                            ),
                        )
                    elif module_key in {"ports", "cells", "netnames"}:
                        table = {
                            "ports": "staging_module_ports",
                            "cells": "staging_module_cells",
                            "netnames": "staging_module_names",
                        }[module_key]
                        for record_name in reader.object_keys():
                            value = reader.read_value()
                            if not isinstance(value, dict):
                                raise ImportError(
                                    f"module {module_name!r} {module_key[:-1]} "
                                    f"{record_name!r}: expected an object"
                                )
                            connection.execute(
                                f"INSERT INTO {table} VALUES (?, ?, ?)",
                                (
                                    module_name,
                                    record_name,
                                    json.dumps(
                                        value,
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                        sort_keys=True,
                                    ),
                                ),
                            )
                    else:
                        reader.skip_value()
        reader.finish()
    if not found_top:
        raise ImportError(f"{path}: missing Yosys top module {top!r}")
    connection.commit()
    return creator


def _expand_staged_yosys_hierarchy(
    connection: sqlite3.Connection,
    top: str,
    *,
    stage_cell: Callable[[str, Mapping[str, Any]], None],
    stage_netname: Callable[[str, Mapping[str, Any]], None],
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[int, Any]]:
    """Expand mapped hierarchy directly into the flat indexed staging tables."""

    module_attributes = {
        str(row["name"]): json.loads(row["attributes_json"])
        for row in connection.execute(
            "SELECT name, attributes_json FROM staging_module_defs ORDER BY name"
        )
    }

    def is_expandable(module_name: str) -> bool:
        attributes = module_attributes.get(module_name)
        if attributes is None:
            return False
        return not any(
            _yosys_attribute_enabled(attributes.get(attribute))
            for attribute in ("blackbox", "whitebox")
        )

    next_global_bit = 2
    expanded_module_instances = 0
    maximum_depth = 0
    alias_parents: Dict[int, int] = {}
    alias_constants: Dict[int, str] = {}

    def alias_root(bit: int) -> int:
        parent = alias_parents.get(bit, bit)
        if parent != bit:
            parent = alias_root(parent)
            alias_parents[bit] = parent
        return parent

    def canonical_bit(bit: Any) -> Any:
        if not isinstance(bit, int):
            return bit
        root = alias_root(bit)
        return alias_constants.get(root, root)

    def unify_bits(left: Any, right: Any, context: str) -> Any:
        left = canonical_bit(left)
        right = canonical_bit(right)
        if left == right:
            return left
        if not isinstance(left, int) and not isinstance(right, int):
            raise ImportError(
                f"{context}: incompatible constant bindings {left!r} and {right!r}"
            )
        if isinstance(left, int) and not isinstance(right, int):
            alias_constants[alias_root(left)] = str(right)
            return right
        if not isinstance(left, int) and isinstance(right, int):
            alias_constants[alias_root(right)] = str(left)
            return left
        left_root = alias_root(left)
        right_root = alias_root(right)
        if left_root == right_root:
            return canonical_bit(left_root)
        representative = min(left_root, right_root)
        absorbed = max(left_root, right_root)
        left_constant = alias_constants.pop(left_root, None)
        right_constant = alias_constants.pop(right_root, None)
        if (
            left_constant is not None
            and right_constant is not None
            and left_constant != right_constant
        ):
            raise ImportError(
                f"{context}: incompatible constant bindings "
                f"{left_constant!r} and {right_constant!r}"
            )
        alias_parents[absorbed] = representative
        constant = left_constant if left_constant is not None else right_constant
        if constant is not None:
            alias_constants[representative] = constant
        return canonical_bit(representative)

    def allocate_global_bit() -> int:
        nonlocal next_global_bit
        bit = next_global_bit
        next_global_bit += 1
        return bit

    def qualified_name(prefix: str, name: str) -> str:
        return name if not prefix else f"{prefix}.{name}"

    def module_ports(module_name: str) -> List[Tuple[str, Dict[str, Any]]]:
        return [
            (str(row["name"]), json.loads(row["record_json"]))
            for row in connection.execute(
                "SELECT name, record_json FROM staging_module_ports "
                "WHERE module = ? ORDER BY name",
                (module_name,),
            )
        ]

    def expand(
        module_name: str,
        prefix: str,
        bound_ports: Optional[Mapping[str, List[Any]]],
        ancestors: Tuple[str, ...],
    ) -> Dict[str, Any]:
        nonlocal expanded_module_instances, maximum_depth
        if module_name in ancestors:
            chain = " -> ".join((*ancestors, module_name))
            raise ImportError(f"recursive Yosys module hierarchy: {chain}")
        expanded_module_instances += 1
        maximum_depth = max(maximum_depth, len(ancestors))
        local_bits: Dict[int, Any] = {}

        def bind(local_bit: int, global_bit: Any, context: str) -> None:
            previous = local_bits.get(local_bit)
            if previous is not None:
                global_bit = unify_bits(previous, global_bit, context)
            local_bits[local_bit] = canonical_bit(global_bit)

        def resolve(bit: Any) -> Any:
            if not isinstance(bit, int):
                return bit
            if bit not in local_bits:
                local_bits[bit] = allocate_global_bit()
            return canonical_bit(local_bits[bit])

        ports = module_ports(module_name)
        if bound_ports is not None:
            for port_name, raw_port in ports:
                local_port_bits = raw_port.get("bits", [])
                if not isinstance(local_port_bits, list):
                    raise ImportError(
                        f"module {module_name!r} port {port_name!r}: "
                        "bits must be an array"
                    )
                parent_bits = bound_ports.get(port_name)
                if parent_bits is None or not parent_bits:
                    # An open port is not a constant-x binding. Its internal
                    # wire may also feed logic or alias another port. Leave it
                    # local, just as Yosys flatten does. An open input remains
                    # an undriven wire rather than a fabricated constant.
                    direction = raw_port.get("direction")
                    if direction not in {"input", "output", "inout"}:
                        raise ImportError(
                            f"module {module_name!r} port {port_name!r}: "
                            f"invalid direction {direction!r}"
                        )
                    continue
                if len(parent_bits) != len(local_port_bits):
                    raise ImportError(
                        f"module {module_name!r} port {port_name!r}: expected "
                        f"{len(local_port_bits)} connection bits, got "
                        f"{len(parent_bits)}"
                    )
                for index, (local_bit, parent_bit) in enumerate(
                    zip(local_port_bits, parent_bits)
                ):
                    if isinstance(local_bit, int):
                        bind(
                            local_bit,
                            parent_bit,
                            f"module {module_name!r} port {port_name!r}[{index}]",
                        )

        expanded_ports: Dict[str, Any] = {}
        for port_name, raw_port in ports:
            local_port_bits = raw_port.get("bits", [])
            if not isinstance(local_port_bits, list):
                raise ImportError(
                    f"module {module_name!r} port {port_name!r}: "
                    "bits must be an array"
                )
            expanded_port = dict(raw_port)
            expanded_port["bits"] = [resolve(bit) for bit in local_port_bits]
            expanded_ports[port_name] = expanded_port

        for row in connection.execute(
            "SELECT name, record_json FROM staging_module_names "
            "WHERE module = ? ORDER BY name",
            (module_name,),
        ):
            raw_net = json.loads(row["record_json"])
            bits = raw_net.get("bits", [])
            if not isinstance(bits, list):
                continue
            expanded_net = dict(raw_net)
            expanded_net["bits"] = [resolve(bit) for bit in bits]
            stage_netname(
                qualified_name(prefix, str(row["name"])), expanded_net
            )

        next_ancestors = (*ancestors, module_name)
        for row in connection.execute(
            "SELECT name, record_json FROM staging_module_cells "
            "WHERE module = ? ORDER BY name",
            (module_name,),
        ):
            cell_name = str(row["name"])
            raw_cell = json.loads(row["record_json"])
            cell_type = raw_cell.get("type")
            if not isinstance(cell_type, str) or not cell_type:
                raise ImportError(
                    f"module {module_name!r} cell {cell_name!r}: missing type"
                )
            connections = raw_cell.get("connections", {})
            if not isinstance(connections, dict):
                raise ImportError(
                    f"module {module_name!r} cell {cell_name!r}: "
                    "connections must be an object"
                )
            expanded_connections: Dict[str, List[Any]] = {}
            for port_name, bits in sorted(connections.items()):
                if not isinstance(bits, list):
                    raise ImportError(
                        f"module {module_name!r} cell {cell_name!r} port "
                        f"{port_name!r}: bits must be an array"
                    )
                expanded_connections[port_name] = [resolve(bit) for bit in bits]
            expanded_name = qualified_name(prefix, cell_name)
            if is_expandable(cell_type):
                expand(
                    cell_type,
                    expanded_name,
                    expanded_connections,
                    next_ancestors,
                )
                continue
            expanded_cell = dict(raw_cell)
            expanded_cell["connections"] = expanded_connections
            stage_cell(expanded_name, expanded_cell)
        return expanded_ports

    top_ports = expand(top, "", None, ())
    bit_aliases = {
        bit: canonical_bit(bit)
        for bit in range(2, next_global_bit)
        if canonical_bit(bit) != bit
    }
    top_ports = {
        port_name: {
            **raw_port,
            "bits": [canonical_bit(bit) for bit in raw_port.get("bits", [])],
        }
        for port_name, raw_port in top_ports.items()
    }
    return top_ports, {
        "schema": "emuflow.yosys-hierarchical-expansion/v1",
        "top": top,
        "module_definitions": len(module_attributes),
        "expanded_module_instances": expanded_module_instances,
        "maximum_hierarchy_depth": maximum_depth,
        "allocated_signal_bits": next_global_bit - 2,
        "collapsed_signal_bits": len(bit_aliases),
    }, bit_aliases


def import_yosys_json_indexed(
    path: Path,
    output: Path,
    top: Optional[str] = None,
    clocks: Iterable[str] = (),
    *,
    _source_value: Optional[Mapping[str, Any]] = None,
    _source_stream: Optional[TextIO] = None,
    _expand_hierarchy: bool = False,
    staging_transform: Optional[
        Callable[
            [sqlite3.Connection, Mapping[str, Any], Path, str],
            Dict[str, Any],
        ]
    ] = None,
) -> Optional[Dict[str, Any]]:
    """Import Yosys JSON directly into the indexed EmuIR representation.

    Unlike :func:`import_yosys_json`, this production importer never builds a
    second, graph-sized Python ``instances``/``nets`` tree.  With an explicit
    top module, leaf records are decoded directly from the input stream;
    endpoints and name aliases are staged as normalized SQLite rows and the
    canonical EmuIR database is then written one record at a time.
    """

    warnings: List[str] = []
    ports: List[Dict[str, Any]] = []
    creator = "unknown"
    top_name = top
    raw_ports: Dict[str, Any] = {}
    transform_report: Optional[Dict[str, Any]] = None
    hierarchy_report: Optional[Dict[str, Any]] = None
    bit_aliases: Dict[int, Any] = {}

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{output.name}.yosys-index-",
        suffix=".sqlite",
        dir=output.parent,
        delete=False,
    ) as staging_stream:
        staging_path = Path(staging_stream.name)
    staging_path.unlink(missing_ok=True)
    connection: Optional[sqlite3.Connection] = None
    try:
        connection = sqlite3.connect(staging_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=FILE")
        connection.executescript(
            """
            CREATE TABLE staging_endpoints (
                yosys_bit INTEGER NOT NULL,
                role INTEGER NOT NULL,
                seq INTEGER NOT NULL,
                instance_id TEXT,
                port TEXT NOT NULL,
                endpoint_bit INTEGER NOT NULL,
                PRIMARY KEY (yosys_bit, role, seq)
            ) WITHOUT ROWID;
            CREATE TABLE staging_instances (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                ff INTEGER NOT NULL,
                bram INTEGER NOT NULL,
                record_json TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE staging_names (
                yosys_bit INTEGER NOT NULL,
                hidden INTEGER NOT NULL,
                name_length INTEGER NOT NULL,
                name TEXT NOT NULL,
                bus_index INTEGER NOT NULL,
                bus_width INTEGER NOT NULL,
                PRIMARY KEY (
                    yosys_bit, hidden, name_length, name, bus_index, bus_width
                )
            ) WITHOUT ROWID;
            CREATE TABLE staging_nets (
                seq INTEGER PRIMARY KEY,
                yosys_bit INTEGER NOT NULL UNIQUE,
                id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                bus_index INTEGER NOT NULL,
                fanout INTEGER NOT NULL,
                cut_class TEXT NOT NULL,
                aliases_json TEXT NOT NULL
            );
            """
        )

        def stage_cell_record(cell_name: str, raw_cell: Mapping[str, Any]) -> None:
            cell_type = raw_cell.get("type")
            if not isinstance(cell_type, str) or not cell_type:
                raise ImportError(f"cell {cell_name!r}: missing type")
            if cell_type in _NON_HARDWARE_CELL_TYPES:
                return
            resources = classify_primitive_resources(cell_type)
            connection.execute(
                "INSERT INTO staging_instances VALUES (?, ?, ?, ?, ?)",
                (
                    cell_name,
                    cell_type,
                    resources.ff,
                    resources.bram,
                    json.dumps(
                        raw_cell,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )

        def stage_netname_record(
            net_name: str, raw_net: Mapping[str, Any]
        ) -> None:
            bits = raw_net.get("bits", [])
            if not isinstance(bits, list):
                return
            hidden = int(bool(raw_net.get("hide_name", 0)))
            connection.executemany(
                "INSERT OR IGNORE INTO staging_names VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (bit, hidden, len(net_name), net_name, bus_index, len(bits))
                    for bus_index, bit in enumerate(bits)
                    if isinstance(bit, int)
                ),
            )

        if _expand_hierarchy:
            if top is None:
                raise ImportError(
                    "hierarchical indexed Yosys import requires an explicit top"
                )
            if _source_value is not None:
                raise ImportError(
                    "hierarchical indexed Yosys import requires a stream or path"
                )
            creator = _stream_yosys_hierarchy_to_staging(
                path,
                top,
                connection,
                source_stream=_source_stream,
            )
            raw_ports, hierarchy_report, bit_aliases = (
                _expand_staged_yosys_hierarchy(
                    connection,
                    top,
                    stage_cell=stage_cell_record,
                    stage_netname=stage_netname_record,
                )
            )
            if bit_aliases:
                connection.execute(
                    "CREATE TEMP TABLE staging_bit_aliases ("
                    "old_bit INTEGER PRIMARY KEY, canonical_bit INTEGER)"
                )
                connection.executemany(
                    "INSERT INTO staging_bit_aliases VALUES (?, ?)",
                    (
                        (old_bit, canonical if isinstance(canonical, int) else None)
                        for old_bit, canonical in sorted(bit_aliases.items())
                    ),
                )
                connection.executescript(
                    """
                    CREATE TABLE staging_names_canonical (
                        yosys_bit INTEGER NOT NULL,
                        hidden INTEGER NOT NULL,
                        name_length INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        bus_index INTEGER NOT NULL,
                        bus_width INTEGER NOT NULL,
                        PRIMARY KEY (
                            yosys_bit, hidden, name_length, name,
                            bus_index, bus_width
                        )
                    ) WITHOUT ROWID;
                    INSERT OR IGNORE INTO staging_names_canonical
                    SELECT COALESCE(a.canonical_bit, n.yosys_bit),
                           n.hidden, n.name_length, n.name,
                           n.bus_index, n.bus_width
                      FROM staging_names AS n
                 LEFT JOIN staging_bit_aliases AS a
                        ON a.old_bit = n.yosys_bit
                     WHERE a.old_bit IS NULL OR a.canonical_bit IS NOT NULL;
                    DROP TABLE staging_names;
                    ALTER TABLE staging_names_canonical RENAME TO staging_names;
                    DROP TABLE staging_bit_aliases;
                    """
                )
            top_name = top
        elif _source_value is None and top is not None:
            creator, raw_ports = _stream_yosys_module_records(
                path,
                top,
                on_cell=stage_cell_record,
                on_netname=stage_netname_record,
                source_stream=_source_stream,
            )
            top_name = top
        else:
            source = read_json(path) if _source_value is None else _source_value
            modules = source.get("modules")
            if not isinstance(modules, dict):
                raise ImportError(f"{path}: missing Yosys 'modules' object")
            selected_top, module = _select_top_module(modules, top)
            top_name = selected_top
            creator_value = source.get("creator", "unknown")
            creator = creator_value if isinstance(creator_value, str) else "unknown"
            raw_ports_value = module.get("ports", {})
            raw_cells = module.get("cells", {})
            raw_netnames = module.get("netnames", {})
            if not isinstance(raw_ports_value, dict):
                raise ImportError(
                    f"{path}: module {top_name!r} ports must be an object"
                )
            if not isinstance(raw_cells, dict):
                raise ImportError(
                    f"{path}: module {top_name!r} cells must be an object"
                )
            if not isinstance(raw_netnames, dict):
                raise ImportError(
                    f"{path}: module {top_name!r} netnames must be an object"
                )
            raw_ports = raw_ports_value
            for cell_name, raw_cell in sorted(raw_cells.items()):
                if not isinstance(raw_cell, dict):
                    raise ImportError(f"cell {cell_name!r}: expected an object")
                stage_cell_record(cell_name, raw_cell)
            for net_name, raw_net in sorted(raw_netnames.items()):
                if isinstance(raw_net, dict):
                    stage_netname_record(net_name, raw_net)

        if top_name is None:
            raise ImportError(f"{path}: unable to determine top module")
        if staging_transform is not None:
            transform_report = staging_transform(
                connection,
                raw_ports,
                path,
                top_name,
            )
            if hierarchy_report is not None:
                transform_report = {
                    **transform_report,
                    "hierarchical_interchange": hierarchy_report,
                }
        elif hierarchy_report is not None:
            transform_report = {
                "hierarchical_interchange": hierarchy_report,
            }
        explicit_clocks = set(clocks)
        clock_ports = {
            name
            for name in raw_ports
            if name in explicit_clocks or _CLOCK_NAME.search(name)
        }
        reset_ports = {name for name in raw_ports if _RESET_NAME.search(name)}
        endpoint_sequence = 0
        endpoint_buffer: List[Tuple[int, int, int, Optional[str], str, int]] = []

        def flush_endpoints() -> None:
            if not endpoint_buffer:
                return
            connection.executemany(
                "INSERT INTO staging_endpoints VALUES (?, ?, ?, ?, ?, ?)",
                endpoint_buffer,
            )
            endpoint_buffer.clear()

        def stage_endpoint(
            bit: int,
            role: int,
            instance: Optional[str],
            port: str,
            endpoint_bit: int,
        ) -> None:
            nonlocal endpoint_sequence
            normalized_bit = bit_aliases.get(bit, bit)
            if not isinstance(normalized_bit, int):
                return
            endpoint_buffer.append(
                (
                    normalized_bit,
                    role,
                    endpoint_sequence,
                    instance,
                    port,
                    endpoint_bit,
                )
            )
            endpoint_sequence += 1
            if len(endpoint_buffer) >= 16384:
                flush_endpoints()

        for port_name, raw_port in sorted(raw_ports.items()):
            if not isinstance(raw_port, dict):
                raise ImportError(f"port {port_name!r}: expected an object")
            direction = raw_port.get("direction", "unknown")
            bits = raw_port.get("bits", [])
            if not isinstance(bits, list) or not bits:
                raise ImportError(
                    f"port {port_name!r}: expected a non-empty bits array"
                )
            constants = [
                {"bit": bit_index, "value": bit.lower()}
                for bit_index, bit in enumerate(bits)
                if isinstance(bit, str)
            ]
            if constants and direction not in {"output", "inout"}:
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
                    "constant_connections": constants,
                }
            )
            for bit_index, bit in enumerate(bits):
                if not isinstance(bit, int):
                    continue
                for role_name in _roles(direction, is_top_port=True):
                    stage_endpoint(
                        bit,
                        0 if role_name == "driver" else 1,
                        None,
                        port_name,
                        bit_index,
                    )

        for staged_cell in connection.execute(
            "SELECT id, record_json FROM staging_instances ORDER BY id"
        ):
            cell_name = staged_cell["id"]
            raw_cell = json.loads(staged_cell["record_json"])
            directions = raw_cell.get("port_directions", {})
            connections = raw_cell.get("connections", {})
            if not isinstance(directions, dict):
                directions = {}
            if not isinstance(connections, dict):
                raise ImportError(f"cell {cell_name!r}: connections must be an object")
            for port_name, bits in sorted(connections.items()):
                if not isinstance(bits, list):
                    raise ImportError(
                        f"cell {cell_name!r} port {port_name!r}: bits must be an array"
                    )
                direction = directions.get(port_name, "unknown")
                if direction == "unknown":
                    warnings.append(
                        f"cell {cell_name!r} port {port_name!r} has unknown direction"
                    )
                for bit_index, bit in enumerate(bits):
                    if not isinstance(bit, int):
                        continue
                    for role_name in _roles(direction, is_top_port=False):
                        stage_endpoint(
                            bit,
                            0 if role_name == "driver" else 1,
                            cell_name,
                            port_name,
                            bit_index,
                        )

        flush_endpoints()
        connection.commit()

        name_iterator = _indexed_name_groups(connection)
        current_names = next(name_iterator, None)
        net_sequence = 0
        for bit, endpoint_rows in _indexed_endpoint_groups(connection):
            current_names = _advance_name_group(
                name_iterator, current_names, bit
            )
            names = (
                current_names[1]
                if current_names is not None and current_names[0] == bit
                else []
            )
            if names:
                first = names[0]
                name = str(first["name"])
                bus_index = int(first["bus_index"])
                bus_width = int(first["bus_width"])
                canonical = name if bus_width == 1 else f"{name}[{bus_index}]"
                aliases = sorted({str(row["name"]) for row in names})
            else:
                canonical = f"$bit_{bit}"
                aliases = []
                bus_index = 0
            net_id = canonical
            try:
                connection.execute(
                    "INSERT INTO staging_nets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (net_sequence, bit, net_id, canonical, bus_index, 0, "", "[]"),
                )
            except sqlite3.IntegrityError:
                net_id = f"{canonical}#{bit}"
                connection.execute(
                    "INSERT INTO staging_nets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (net_sequence, bit, net_id, canonical, bus_index, 0, "", "[]"),
                )
            drivers = [row for row in endpoint_rows if row["role"] == 0]
            sinks = [row for row in endpoint_rows if row["role"] == 1]
            if any(alias in clock_ports for alias in aliases) or canonical in clock_ports:
                cut_class = "clock"
            elif any(alias in reset_ports for alias in aliases) or canonical in reset_ports:
                cut_class = "reset"
            elif len(drivers) > 1:
                cut_class = "multi_driver"
                warnings.append(f"net {net_id!r} has {len(drivers)} drivers")
            elif not drivers:
                cut_class = "undriven"
            elif drivers[0]["instance_id"] is None:
                cut_class = "primary_input"
            elif bool(drivers[0]["instance_ff"]) or bool(drivers[0]["instance_bram"]):
                cut_class = "register_output"
            elif sinks and all(
                row["instance_id"] is not None
                and _is_transport_safe_sequential_input(
                    str(row["instance_type"]), str(row["port"])
                )
                for row in sinks
            ):
                cut_class = "register_input"
            else:
                cut_class = "combinational"
            connection.execute(
                """
                UPDATE staging_nets
                   SET fanout = ?, cut_class = ?, aliases_json = ?
                 WHERE seq = ?
                """,
                (
                    len(sinks),
                    cut_class,
                    json.dumps(
                        aliases,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    net_sequence,
                ),
            )
            net_sequence += 1
        connection.commit()

        def instances() -> Iterator[Dict[str, Any]]:
            for staged_cell in connection.execute(
                "SELECT id, type, record_json FROM staging_instances ORDER BY id"
            ):
                cell_name = staged_cell["id"]
                cell_type = staged_cell["type"]
                raw_cell = json.loads(staged_cell["record_json"])
                resources = classify_primitive_resources(str(cell_type))
                parameters = raw_cell.get("parameters", {})
                attributes = raw_cell.get("attributes", {})
                constants = []
                connections = raw_cell.get("connections", {})
                if isinstance(connections, dict):
                    for port_name, bits in sorted(connections.items()):
                        if not isinstance(bits, list):
                            continue
                        for bit_index, bit in enumerate(bits):
                            if isinstance(bit, str):
                                constants.append(
                                    {
                                        "port": port_name,
                                        "bit": bit_index,
                                        "value": bit.lower(),
                                    }
                                )
                yield {
                    "id": cell_name,
                    "name": cell_name,
                    "type": cell_type,
                    "resources": resources.to_dict(),
                    "parameters": parameters if isinstance(parameters, dict) else {},
                    "attributes": attributes if isinstance(attributes, dict) else {},
                    "constant_connections": constants,
                }

        def nets() -> Iterator[Dict[str, Any]]:
            endpoints = _indexed_endpoint_groups(connection)
            current_endpoints = next(endpoints, None)
            for row in connection.execute(
                "SELECT * FROM staging_nets ORDER BY seq"
            ):
                bit = int(row["yosys_bit"])
                while current_endpoints is not None and current_endpoints[0] < bit:
                    current_endpoints = next(endpoints, None)
                endpoint_rows = (
                    current_endpoints[1]
                    if current_endpoints is not None and current_endpoints[0] == bit
                    else []
                )

                def records(role: int) -> List[Dict[str, Any]]:
                    return [
                        {
                            "instance": endpoint["instance_id"],
                            "port": endpoint["port"],
                            "bit": endpoint["endpoint_bit"],
                        }
                        for endpoint in endpoint_rows
                        if endpoint["role"] == role
                    ]

                yield {
                    "id": row["id"],
                    "name": row["name"],
                    "yosys_bit": bit,
                    "bus_index": row["bus_index"],
                    "aliases": json.loads(row["aliases_json"]),
                    "drivers": records(0),
                    "sinks": records(1),
                    "fanout": row["fanout"],
                    "cut_class": row["cut_class"],
                }

        value = {
            "schema": EMUIR_SCHEMA,
            "design": {
                "name": top_name,
                "top": top_name,
                "source_format": "yosys-json",
                "source": str(path),
                "creator": creator,
            },
            "ports": ports,
            "instances": instances(),
            "nets": nets(),
            "clocks": [
                {
                    "id": port_name,
                    "name": port_name,
                    "source_port": port_name,
                    "period_ns": None,
                }
                for port_name in sorted(clock_ports)
            ],
            "warnings": sorted(set(warnings)),
        }
        write_emuir_sqlite(output, value)
    finally:
        if connection is not None:
            connection.close()
        staging_path.unlink(missing_ok=True)
    return transform_report
