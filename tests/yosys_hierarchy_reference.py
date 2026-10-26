"""Independent former Python hierarchy producer, validation only."""
from emuflow.streaming_json import StreamingJSONObjectReader
from emuflow.errors import ImportError
from contextlib import nullcontext
from typing import *
from pathlib import Path
import sqlite3, json, gzip

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
                    elif raw_port.get("direction") in {"output", "inout"}:
                        # A mapped child output can already be a constant.
                        # Propagate that driver back to the parent's wire,
                        # including consumers staged before this child.
                        unify_bits(
                            parent_bit, local_bit,
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


