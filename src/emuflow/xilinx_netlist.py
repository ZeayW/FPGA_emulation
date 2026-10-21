"""Provider-neutral EmuIR to normalized Xilinx Yosys JSON lowering."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping as MappingABC
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .emuir_compact import compact_metadata, is_compact_emuir
from .emuir_storage import _RowsForOwner, _decode, open_emuir_sqlite_read_only
from .equivalence import _lut_definition
from .errors import ValidationError
from .io import file_sha256, write_json
from .ir import EmuIR
from .resources import ResourceVector
from .vivado_netlist import lower_vivado_primitives
from .xilinx_primitives import (
    DEFAULT_XILINX_PRIMITIVE_LIBRARY,
    XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
    audit_xilinx_mapped_json,
    load_xilinx_primitive_library,
)


XILINX_MAPPED_NETLIST_REPORT_SCHEMA = (
    "emuflow.xilinx-mapped-netlist-report/v1"
)
XILINX_MAPPED_INDEX_SCHEMA_V1 = "emuflow.xilinx-mapped-index/v1"
XILINX_MAPPED_INDEX_SCHEMA = "emuflow.xilinx-mapped-index/v2"
_XILINX_MAPPED_INDEX_SCHEMAS = {
    XILINX_MAPPED_INDEX_SCHEMA_V1, XILINX_MAPPED_INDEX_SCHEMA,
}
_NATIVE_FFS = {"FDCE", "FDPE", "FDRE", "FDSE"}
_VTR_HARD_MACROS = {"VTR_MULTIPLY", "VTR_SP_RAM", "VTR_DP_RAM"}


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


class _IndexedRecords(MappingABC[str, Mapping[str, Any]]):
    """Read-only mapped-netlist records backed by the active scratch index."""

    def __init__(self, connection: sqlite3.Connection, table: str):
        self.connection = connection
        self.table = table
        self.count = int(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        )

    def __getitem__(self, key: str) -> Mapping[str, Any]:
        row = self.connection.execute(
            f"SELECT record_json FROM {self.table} WHERE id=?", (key,)
        ).fetchone()
        if row is None:
            raise KeyError(key)
        value = json.loads(row[0])
        if not isinstance(value, dict):
            raise ValidationError(f"indexed mapped record {key!r} is invalid")
        return value

    def __iter__(self) -> Iterator[str]:
        for row in self.connection.execute(
            f"SELECT id FROM {self.table} ORDER BY seq"
        ):
            yield str(row[0])

    def __len__(self) -> int:
        return self.count

    def items(self) -> Iterator[Tuple[str, Mapping[str, Any]]]:  # type: ignore[override]
        for row in self.connection.execute(
            f"SELECT id,record_json FROM {self.table} ORDER BY seq"
        ):
            value = json.loads(row[1])
            if not isinstance(value, dict):
                raise ValidationError(
                    f"indexed mapped record {row[0]!r} is invalid"
                )
            yield str(row[0]), value

    def items_by_id(self) -> Iterator[Tuple[str, Mapping[str, Any]]]:
        """Stream records in canonical identity order without sorting in Python."""

        for row in self.connection.execute(
            f"SELECT id,record_json FROM {self.table} ORDER BY id"
        ):
            value = json.loads(row[1])
            if not isinstance(value, dict):
                raise ValidationError(
                    f"indexed mapped record {row[0]!r} is invalid"
                )
            yield str(row[0]), value

    def values(self) -> Iterator[Mapping[str, Any]]:  # type: ignore[override]
        for _name, value in self.items():
            yield value


class _IndexedCells(_IndexedRecords):
    """Reconstruct compatibility cell connections from typed pin rows."""

    _DIRECTION = {0: "input", 1: "output", 2: "inout"}

    @classmethod
    def _with_pins(
        cls, value: Mapping[str, Any], rows: Iterator[sqlite3.Row]
    ) -> Mapping[str, Any]:
        cell = dict(value)
        connections: Dict[str, list[Any]] = {}
        directions: Dict[str, str] = {}
        for row in rows:
            port = str(row["port"])
            width = int(row["port_width"])
            values = connections.setdefault(port, [None] * width)
            bit = int(row["port_bit"])
            if bit < 0 or bit >= width or values[bit] is not None:
                raise ValidationError("indexed mapped pin row is invalid")
            values[bit] = (
                int(row["net_bit"])
                if int(row["value_kind"]) == 0 else str(row["constant"])
            )
            direction = cls._DIRECTION.get(int(row["direction"]))
            if direction is None:
                raise ValidationError("indexed mapped pin direction is invalid")
            previous = directions.setdefault(port, direction)
            if previous != direction:
                raise ValidationError("indexed mapped port directions disagree")
        if any(any(value is None for value in values) for values in connections.values()):
            raise ValidationError("indexed mapped port is incomplete")
        cell["connections"] = connections
        cell["port_directions"] = directions
        return cell

    def __getitem__(self, key: str) -> Mapping[str, Any]:
        row = self.connection.execute(
            "SELECT record_json FROM cells WHERE id=?", (key,)
        ).fetchone()
        if row is None:
            raise KeyError(key)
        value = json.loads(row[0])
        if not isinstance(value, dict):
            raise ValidationError(f"indexed mapped record {key!r} is invalid")
        pins = iter(self.connection.execute(
            "SELECT * FROM cell_pins WHERE cell_id=? ORDER BY port,port_bit",
            (key,),
        ))
        return self._with_pins(value, pins)

    def _items(self, order: str) -> Iterator[Tuple[str, Mapping[str, Any]]]:
        owner = "cell_seq" if order == "seq" else "cell_id"
        pins = _RowsForOwner(
            self.connection.execute(
                f"SELECT * FROM cell_pins ORDER BY {owner},port,port_bit"
            ),
            owner,
        )
        for row in self.connection.execute(
            f"SELECT id,seq,record_json FROM cells ORDER BY {order}"
        ):
            value = json.loads(row["record_json"])
            if not isinstance(value, dict):
                raise ValidationError(
                    f"indexed mapped record {row['id']!r} is invalid"
                )
            owner_value = int(row["seq"]) if order == "seq" else str(row["id"])
            yield str(row["id"]), self._with_pins(
                value, iter(pins.take(owner_value))
            )
        pins.finish()

    def items(self) -> Iterator[Tuple[str, Mapping[str, Any]]]:  # type: ignore[override]
        yield from self._items("seq")

    def items_by_id(self) -> Iterator[Tuple[str, Mapping[str, Any]]]:
        yield from self._items("id")

    def values(self) -> Iterator[Mapping[str, Any]]:  # type: ignore[override]
        for _name, value in self.items():
            yield value


class IndexedXilinxMappedNetlist(MappingABC[str, Any]):
    """Bounded read interface for a production Xilinx mapped netlist."""

    def __init__(self, path: Path):
        self.path = path
        self.connection = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro&immutable=1",
            uri=True,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key='schema'"
        ).fetchone()
        if row is None or row[0] not in _XILINX_MAPPED_INDEX_SCHEMAS:
            self.connection.close()
            raise ValidationError("Xilinx mapped index schema is invalid")
        self.schema = str(row[0])
        metadata = dict(self.connection.execute("SELECT key,value FROM metadata"))
        self.top = json.loads(metadata["top_json"])
        self.creator = json.loads(metadata["creator_json"])
        self.ports = json.loads(metadata["ports_json"])
        if not isinstance(self.top, str) or not isinstance(self.ports, dict):
            self.connection.close()
            raise ValidationError("Xilinx mapped index metadata is invalid")
        self.cells = (
            _IndexedCells(self.connection, "cells")
            if self.schema == XILINX_MAPPED_INDEX_SCHEMA
            else _IndexedRecords(self.connection, "cells")
        )
        self.netnames = _IndexedRecords(self.connection, "netnames")
        self.module: Mapping[str, Any] = {
            "attributes": {"top": "1"},
            "parameter_default_values": {},
            "ports": self.ports,
            "cells": self.cells,
            "memories": {},
            "netnames": self.netnames,
        }
        self.modules: Mapping[str, Any] = {self.top: self.module}

    def __getitem__(self, key: str) -> Any:
        if key == "creator":
            return self.creator
        if key == "modules":
            return self.modules
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        yield "creator"
        yield "modules"

    def __len__(self) -> int:
        return 2

    def iter_pins_by_net(
        self,
    ) -> Iterator[Tuple[int, str, str, int, int, str]]:
        """Stream typed non-constant cell pins in routed-net order."""

        if self.schema != XILINX_MAPPED_INDEX_SCHEMA:
            raise ValidationError(
                "legacy Xilinx mapped index has no typed pin relation"
            )
        direction_names = {0: "input", 1: "output", 2: "inout"}
        for row in self.connection.execute(
            """
            SELECT net_bit,cell_id,port,port_bit,port_width,direction
              FROM cell_pins
             WHERE value_kind=0
             ORDER BY net_bit,cell_id,port,port_bit
            """
        ):
            direction = direction_names.get(int(row["direction"]))
            if direction is None:
                raise ValidationError("indexed mapped pin direction is invalid")
            yield (
                int(row["net_bit"]), str(row["cell_id"]), str(row["port"]),
                int(row["port_bit"]), int(row["port_width"]), direction,
            )

    def close(self) -> None:
        connection = getattr(self, "connection", None)
        if connection is not None:
            connection.close()
            self.connection = None  # type: ignore[assignment]

    def __del__(self) -> None:
        self.close()

    def __enter__(self) -> "IndexedXilinxMappedNetlist":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


def open_xilinx_mapped_index(path: Path) -> IndexedXilinxMappedNetlist:
    return IndexedXilinxMappedNetlist(path)


def is_xilinx_mapped_index(path: Path) -> bool:
    """Return whether ``path`` is the canonical mapped-netlist SQLite store."""

    try:
        connection = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro&immutable=1", uri=True
        )
    except sqlite3.Error:
        return False
    try:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='schema'"
        ).fetchone()
        return row is not None and row[0] in _XILINX_MAPPED_INDEX_SCHEMAS
    except sqlite3.Error:
        return False
    finally:
        connection.close()


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


def _lower_instance(
    instance: Mapping[str, Any], native_xilinx_cells: set[str]
) -> Tuple[Dict[str, Any], Dict[Tuple[str, int], Tuple[str, int]]]:
    """Lower one cell without retaining the complete EmuIR graph."""

    lowered = {
        "id": instance["id"],
        "type": instance["type"],
        "parameters": dict(instance.get("parameters", {})),
        "attributes": dict(instance.get("attributes", {})),
        "constant_connections": [
            dict(item) for item in instance.get("constant_connections", [])
        ],
    }
    remap: Dict[Tuple[str, int], Tuple[str, int]] = {}
    cell_type = str(lowered["type"])
    if cell_type in native_xilinx_cells:
        return lowered, remap
    if cell_type.startswith("LUT") or cell_type in {"$lut", "$_LUT_"}:
        width, input_port, output_port, truth = _lut_definition(lowered)
        if width < 1 or width > 6:
            raise ValidationError(
                f"Vivado LUT {instance['id']!r} has unsupported width {width}"
            )
        lowered["type"] = f"LUT{width}"
        lowered["parameters"] = {"INIT": format(truth, f"0{1 << width}b")}
        for bit in range(width):
            source_bit = 0 if input_port == "I" else bit
            source_port = f"I{bit}" if input_port == "I" else input_port
            remap[(source_port, source_bit)] = (f"I{bit}", 0)
        remap[(output_port, 0)] = ("O", 0)
        return lowered, remap
    if cell_type in {"$_DFF_P_", "$_DFF_N_"}:
        lowered["type"] = "FDRE"
        lowered["parameters"] = {
            "INIT": "0",
            "IS_C_INVERTED": "1" if cell_type == "$_DFF_N_" else "0",
        }
        lowered["constant_connections"].extend(
            (
                {"port": "CE", "bit": 0, "value": "1"},
                {"port": "R", "bit": 0, "value": "0"},
            )
        )
        return lowered, remap
    if cell_type in _NATIVE_FFS or cell_type in _VTR_HARD_MACROS:
        return lowered, remap
    raise ValidationError(
        f"Vivado backend does not yet lower mapped primitive type {cell_type!r}"
    )


def _write_object_entries(
    stream: Any, entries: Iterator[Tuple[str, Mapping[str, Any]]]
) -> int:
    stream.write("{")
    count = 0
    for name, value in entries:
        if count:
            stream.write(",")
        stream.write(_json(name))
        stream.write(":")
        stream.write(_json(value))
        count += 1
    stream.write("}")
    return count


def _emit_compact_xilinx_mapped_artifacts(
    ir_path: Path,
    output_path: Optional[Path],
    index_path: Path,
) -> Dict[str, Any]:
    """Stream compact EmuIR to one canonical index and optional JSON export.

    The physical flow uses the SQLite index as its sole mapped-netlist
    representation.  A JSON document is emitted only for the explicit export
    API; producing both representations in the Phase 7 hot path duplicates
    graph-sized serialization and I/O without adding information.
    """

    source = open_emuir_sqlite_read_only(ir_path)
    output_temp = (
        output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
        if output_path is not None else None
    )
    index_temp = index_path.with_name(
        f".{index_path.name}.{uuid.uuid4().hex}.tmp"
    )
    index: Optional[sqlite3.Connection] = None
    try:
        metadata = compact_metadata(source)
        design = metadata.get("design")
        if not isinstance(design, dict) or not isinstance(design.get("top"), str):
            raise ValidationError("compact EmuIR design metadata is invalid")
        top = design["top"]
        ports_source = metadata.get("ports")
        if not isinstance(ports_source, list):
            raise ValidationError("compact EmuIR ports metadata is invalid")
        _library, library_contract = load_xilinx_primitive_library()
        contracts = library_contract["cells"]
        native_xilinx_cells = set(contracts)

        net_count = int(
            source.execute("SELECT count(*) FROM net_records").fetchone()[0]
        )
        top_bits: Dict[Tuple[str, int], int] = {}
        for row in source.execute(
            """
            SELECT e.net_seq,e.port,e.bit
              FROM endpoints e
             WHERE e.instance_seq IS NULL
             ORDER BY e.net_seq,e.role,e.ordinal
            """
        ):
            key = (str(row["port"]), int(row["bit"]))
            value = int(row["net_seq"]) + 2
            previous = top_bits.setdefault(key, value)
            if previous != value:
                raise ValidationError(
                    f"Xilinx mapped top pin {key!r} has multiple nets"
                )
        ports: Dict[str, Any] = {}
        unconnected_output_bits = 0
        unconnected_input_bits = 0
        next_dead_input_bit = net_count + 2
        for port in ports_source:
            if not isinstance(port, dict):
                raise ValidationError("compact EmuIR port is invalid")
            constants = {
                int(item["bit"]): item["value"]
                for item in port.get("constant_connections", [])
            }
            bits = []
            for bit in range(int(port["width"])):
                key = (str(port["id"]), bit)
                if key in top_bits:
                    bits.append(top_bits[key])
                elif bit in constants:
                    bits.append(constants[bit])
                elif port["direction"] == "output":
                    bits.append("x")
                    unconnected_output_bits += 1
                elif port["direction"] == "input":
                    bits.append(next_dead_input_bit)
                    next_dead_input_bit += 1
                    unconnected_input_bits += 1
                else:
                    raise ValidationError(
                        f"Xilinx mapped top pin {key!r} is unconnected"
                    )
            ports[str(port["id"])] = {
                "direction": port["direction"], "bits": bits,
            }

        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index = sqlite3.connect(index_temp)
        index.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)
              WITHOUT ROWID;
            CREATE TABLE cells(
              id TEXT PRIMARY KEY,seq INTEGER NOT NULL UNIQUE,type TEXT NOT NULL,
              record_json TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE cell_pins(
              cell_seq INTEGER NOT NULL,
              cell_id TEXT NOT NULL,
              port TEXT NOT NULL,
              port_bit INTEGER NOT NULL,
              port_width INTEGER NOT NULL,
              direction INTEGER NOT NULL CHECK(direction BETWEEN 0 AND 2),
              value_kind INTEGER NOT NULL CHECK(value_kind IN (0,1)),
              net_bit INTEGER,
              constant TEXT,
              PRIMARY KEY(cell_id,port,port_bit),
              CHECK(
                (value_kind=0 AND net_bit IS NOT NULL AND constant IS NULL)
                OR (value_kind=1 AND net_bit IS NULL AND constant IS NOT NULL)
              )
            ) WITHOUT ROWID;
            CREATE INDEX cell_pins_by_seq
              ON cell_pins(cell_seq,port,port_bit);
            CREATE INDEX cell_pins_by_net
              ON cell_pins(net_bit,direction,cell_id,port,port_bit)
              WHERE value_kind=0;
            CREATE TABLE netnames(
              id TEXT PRIMARY KEY,seq INTEGER NOT NULL UNIQUE,
              record_json TEXT NOT NULL
            ) WITHOUT ROWID;
            """
        )
        index.executemany(
            "INSERT INTO metadata VALUES (?,?)",
            (
                ("schema", XILINX_MAPPED_INDEX_SCHEMA),
                ("top_json", _json(top)),
                ("creator_json", _json("EmuFlow Xilinx mapped-netlist exporter")),
                ("ports_json", _json(ports)),
            ),
        )

        endpoint_rows = _RowsForOwner(
            source.execute(
                """
                SELECT instance_seq,net_seq,role,port,bit
                  FROM endpoints
                 WHERE instance_seq IS NOT NULL
                 ORDER BY instance_seq,role,ordinal
                """
            ),
            "instance_seq",
        )
        inventory: Counter[str] = Counter()
        hard_blocks: Counter[str] = Counter()

        def cells() -> Iterator[Tuple[str, Mapping[str, Any]]]:
            rows = source.execute(
                """
                SELECT c.seq,c.id,c.type,p.value AS parameters_json,
                       a.value AS attributes_json,k.value AS constants_json
                  FROM cell_records c
             LEFT JOIN payloads p ON p.seq=c.parameters_seq
             LEFT JOIN payloads a ON a.seq=c.attributes_seq
             LEFT JOIN payloads k ON k.seq=c.constants_seq
                 ORDER BY c.seq
                """
            )
            for row in rows:
                instance = {
                    "id": str(row["id"]),
                    "type": str(row["type"]),
                    "parameters": (
                        _decode(row["parameters_json"], "parameters")
                        if row["parameters_json"] is not None else {}
                    ),
                    "attributes": (
                        _decode(row["attributes_json"], "attributes")
                        if row["attributes_json"] is not None else {}
                    ),
                    "constant_connections": _decode(
                        row["constants_json"], "constants"
                    ),
                }
                lowered, remap = _lower_instance(instance, native_xilinx_cells)
                pin_values: Dict[str, Dict[int, Any]] = defaultdict(dict)
                pin_directions: Dict[str, Dict[int, str]] = defaultdict(dict)
                for endpoint in endpoint_rows.take(int(row["seq"])):
                    port, bit = remap.get(
                        (str(endpoint["port"]), int(endpoint["bit"])),
                        (str(endpoint["port"]), int(endpoint["bit"])),
                    )
                    value = int(endpoint["net_seq"]) + 2
                    previous = pin_values[port].setdefault(bit, value)
                    if previous != value:
                        raise ValidationError(
                            f"Xilinx mapped cell {row['id']!r}.{port}[{bit}] "
                            "has multiple nets"
                        )
                    direction = (
                        "output" if int(endpoint["role"]) == 0 else "input"
                    )
                    observed = pin_directions[port].setdefault(bit, direction)
                    if observed != direction:
                        pin_directions[port][bit] = "inout"
                for constant in lowered["constant_connections"]:
                    port, bit = remap.get(
                        (str(constant["port"]), int(constant["bit"])),
                        (str(constant["port"]), int(constant["bit"])),
                    )
                    previous = pin_values[port].setdefault(bit, constant["value"])
                    if previous != constant["value"]:
                        raise ValidationError(
                            f"Xilinx mapped cell {row['id']!r}.{port}[{bit}] "
                            "has conflicting constant and net"
                        )
                    pin_directions[port].setdefault(bit, "input")
                connections: Dict[str, Any] = {}
                directions: Dict[str, str] = {}
                for port, bit_values in sorted(pin_values.items()):
                    width = max(bit_values) + 1
                    if set(bit_values) != set(range(width)):
                        raise ValidationError(
                            f"Xilinx mapped cell {row['id']!r}.{port} is not contiguous"
                        )
                    connections[port] = [bit_values[bit] for bit in range(width)]
                    observed = set(pin_directions[port].values())
                    directions[port] = (
                        "inout" if "inout" in observed or len(observed) > 1
                        else next(iter(observed))
                    )
                cell = {
                    "hide_name": 0,
                    "type": lowered["type"],
                    "parameters": lowered["parameters"],
                    "attributes": lowered["attributes"],
                    "port_directions": directions,
                    "connections": connections,
                }
                cell_type = str(cell["type"])
                contract = contracts.get(cell_type)
                if contract is None:
                    raise ValidationError(
                        f"unsupported Xilinx primitive {cell_type!r} at {row['id']!r}"
                    )
                inventory[cell_type] += 1
                if contract["hard_block"]:
                    hard_blocks[cell_type] += 1
                indexed_cell = {
                    key: value for key, value in cell.items()
                    if key not in {"connections", "port_directions"}
                }
                index.execute(
                    "INSERT INTO cells VALUES (?,?,?,?)",
                    (
                        str(row["id"]), int(row["seq"]), cell_type,
                        _json(indexed_cell),
                    ),
                )
                direction_code = {"input": 0, "output": 1, "inout": 2}
                index.executemany(
                    "INSERT INTO cell_pins VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        (
                            int(row["seq"]), str(row["id"]), port, bit,
                            len(values), direction_code[directions[port]],
                            0 if isinstance(value, int) else 1,
                            value if isinstance(value, int) else None,
                            None if isinstance(value, int) else str(value),
                        )
                        for port, values in connections.items()
                        for bit, value in enumerate(values)
                    ),
                )
                yield str(row["id"]), cell
            endpoint_rows.finish()

        def netnames() -> Iterator[Tuple[str, Mapping[str, Any]]]:
            for row in source.execute(
                "SELECT seq,id FROM net_records ORDER BY seq"
            ):
                value = {
                    "hide_name": 0,
                    "bits": [int(row["seq"]) + 2],
                    "attributes": {},
                }
                index.execute(
                    "INSERT INTO netnames VALUES (?,?,?)",
                    (str(row["id"]), int(row["seq"]), _json(value)),
                )
                yield str(row["id"]), value

        if output_temp is None:
            cell_count = sum(1 for _name, _value in cells())
            emitted_net_count = sum(1 for _name, _value in netnames())
        else:
            with output_temp.open("w", encoding="utf-8") as stream:
                stream.write(
                    '{"creator":"EmuFlow Xilinx mapped-netlist exporter",'
                )
                stream.write('"modules":{')
                stream.write(_json(top))
                stream.write(':{"attributes":{"top":"1"},')
                stream.write('"parameter_default_values":{},"ports":')
                stream.write(_json(ports))
                stream.write(',"cells":')
                cell_count = _write_object_entries(stream, cells())
                stream.write(',"memories":{},"netnames":')
                emitted_net_count = _write_object_entries(stream, netnames())
                stream.write("}}}\n")
                stream.flush()
        index.commit()
        index.close()
        index = None
        if output_temp is not None:
            with output_temp.open("rb") as stream:
                os.fsync(stream.fileno())
        with index_temp.open("rb") as stream:
            os.fsync(stream.fileno())
        if output_temp is not None and output_path is not None:
            os.replace(output_temp, output_path)
        os.replace(index_temp, index_path)

        resources = ResourceVector.sum(
            ResourceVector.from_mapping(
                {
                    field: value * count
                    for field, value in contracts[cell_type]["resources"].items()
                },
                f"primitive {cell_type!r} resources",
            )
            for cell_type, count in inventory.items()
        )
        canonical_path = output_path if output_path is not None else index_path
        output_sha256 = file_sha256(canonical_path)
        representation = (
            "yosys-json+sqlite-index"
            if output_path is not None else "sqlite-index"
        )
        audit = {
            "status": "pass",
            "schema": "emuflow.xilinx-primitive-audit/v1",
            "mapping_profile": XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
            "top": top,
            "instances": sum(inventory.values()),
            "cell_types": dict(sorted(inventory.items())),
            "hard_block_cells": dict(sorted(hard_blocks.items())),
            "resource_totals": resources.to_dict(include_zeros=False),
            "unknown_cell_types": {},
            "external_macros": {},
            "mapped_sha256": output_sha256,
            "mapped_json_sha256": (
                output_sha256 if output_path is not None else None
            ),
            "mapped_representation": representation,
            "primitive_library_sha256": file_sha256(
                DEFAULT_XILINX_PRIMITIVE_LIBRARY
            ),
        }
        return {
            "top": top,
            "cells": cell_count,
            "nets": emitted_net_count,
            "ports": len(ports),
            "unconnected_input_bits": unconnected_input_bits,
            "unconnected_output_bits": unconnected_output_bits,
            "output_sha256": output_sha256,
            "index": str(index_path),
            "index_sha256": file_sha256(index_path),
            "audit": audit,
        }
    finally:
        source.close()
        if index is not None:
            index.close()
        if output_temp is not None:
            output_temp.unlink(missing_ok=True)
        index_temp.unlink(missing_ok=True)


def emit_xilinx_mapped_index(
    ir_path: Path,
    index_path: Path,
    report_path: Path | None = None,
) -> Mapping[str, Any]:
    """Emit the canonical production mapped-netlist SQLite index.

    Phase 7 consumers share this read-only artifact.  The routine deliberately
    has no JSON fallback: production placement inputs must be compact EmuIR so
    a large design cannot silently re-enter the graph-sized Python path.
    """

    if not is_compact_emuir(ir_path):
        raise ValidationError(
            "Xilinx mapped-index production input must be compact EmuIR"
        )
    streamed = _emit_compact_xilinx_mapped_artifacts(
        ir_path, None, index_path
    )
    report = {
        "schema": XILINX_MAPPED_NETLIST_REPORT_SCHEMA,
        "status": "pass",
        "provider": "emuflow-emuir-to-xilinx-sqlite-index-v2",
        "mapping_profile": XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
        **streamed,
        "output": str(index_path),
    }
    if report_path is not None:
        write_json(report_path, report)
    return report


def emit_xilinx_mapped_json(
    ir_path: Path,
    output_path: Path,
    report_path: Path | None = None,
    *,
    index_path: Optional[Path] = None,
) -> Mapping[str, Any]:
    """Emit a normalized, primitive-only Yosys JSON netlist.

    This is the inverse provider boundary of :func:`import_yosys_json`; it
    does not resynthesize or reinterpret the design.  It preserves each
    EmuIR net and cell identity while lowering only provider-neutral LUT/FF
    spellings into the explicit UltraScale+ primitive namespace.
    """

    if index_path is not None and is_compact_emuir(ir_path):
        streamed = _emit_compact_xilinx_mapped_artifacts(
            ir_path, output_path, index_path
        )
        report = {
            "schema": XILINX_MAPPED_NETLIST_REPORT_SCHEMA,
            "status": "pass",
            "provider": "emuflow-emuir-to-xilinx-yosys-json-indexed-v1",
            "mapping_profile": XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
            **streamed,
            "output": str(output_path),
        }
        if report_path is not None:
            write_json(report_path, report)
        return report

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
    unconnected_output_bits = 0
    unconnected_input_bits = 0
    next_dead_input_bit = max(net_bits.values(), default=1) + 1
    for port in ir.value["ports"]:
        port_constants = {
            item["bit"]: item["value"]
            for item in port.get("constant_connections", [])
        }
        bits = []
        for bit in range(port["width"]):
            key = (port["id"], bit)
            if key in top_port_bits:
                bits.append(top_port_bits[key])
            elif bit in port_constants:
                bits.append(port_constants[bit])
            elif port["direction"] == "output":
                # A split partition retains the original top-level vector
                # shape even when another partition owns some output bits.
                # Yosys JSON represents such genuinely undriven output bits
                # with the constant ``x`` token.  They are not physical nets
                # and therefore must not be fabricated merely to satisfy the
                # vector width.
                bits.append("x")
                unconnected_output_bits += 1
            elif port["direction"] == "input":
                # An unused input bit is still a legal top-level input in
                # Yosys JSON.  Give it a private, sinkless signal identifier;
                # physical routing only consumes cell-to-cell nets, so this
                # preserves the interface without inventing a routed load.
                bits.append(next_dead_input_bit)
                next_dead_input_bit += 1
                unconnected_input_bits += 1
            else:
                raise ValidationError(
                    f"Xilinx mapped top pin {key!r} is unconnected"
                )
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
        "unconnected_input_bits": unconnected_input_bits,
        "unconnected_output_bits": unconnected_output_bits,
        "output": str(output_path),
        "output_sha256": file_sha256(output_path),
        "audit": audit,
    }
    if report_path is not None:
        write_json(report_path, report)
    return report
