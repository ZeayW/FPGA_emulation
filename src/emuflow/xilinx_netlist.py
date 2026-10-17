"""Provider-neutral EmuIR to normalized Xilinx Yosys JSON lowering."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping as MappingABC
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

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
XILINX_MAPPED_INDEX_SCHEMA = "emuflow.xilinx-mapped-index/v2"
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

    def packing_records(self, names: Sequence[str]) -> Mapping[str, Mapping[str, Any]]:
        """Project one site's validation facts, never complete cell payloads.

        The normalized cell/pin rows own connectivity. Only FF polarity
        parameters affect packing; LUT INIT, RAM contents and other synthesis
        metadata must not pass through the Python packing checker.
        """
        if self.table != "cells":
            raise ValidationError("packing projection requires cells")
        result: Dict[str, Dict[str, Any]] = {}
        for offset in range(0, len(names), 256):
            batch = tuple(names[offset:offset + 256])
            if not batch:
                continue
            placeholders = ",".join("?" for _ in batch)
            for name, cell_type in self.connection.execute(
                f"SELECT id,type FROM cells WHERE id IN ({placeholders})", batch,
            ):
                result[name] = {"type": cell_type, "connections": {}, "parameters": {}}
            for name, port, bit in self.connection.execute(
                "SELECT c.id,p.port,COALESCE(p.net_bit,p.constant) "
                "FROM cells c JOIN cell_pins p ON p.cell_seq=c.seq "
                f"WHERE c.id IN ({placeholders}) AND ("
                "(c.type IN ('FDCE','FDPE','FDRE','FDSE') "
                "AND p.port IN ('C','CE','CLR','R','PRE','S','D')) OR "
                "(c.type IN ('MUXF7','MUXF8','MUXF9') AND p.port IN ('I0','I1','S')) OR "
                "(c.type='CARRY8' AND p.port IN ('DI','S'))) "
                "ORDER BY c.seq,p.pin_seq", batch,
            ):
                result[name]["connections"].setdefault(port, []).append(bit)
            for name, parameters in self.connection.execute(
                "SELECT c.id,json_extract(c.record_json,'$.parameters') FROM cells c "
                f"WHERE c.id IN ({placeholders}) "
                "AND c.type IN ('FDCE','FDPE','FDRE','FDSE')", batch,
            ):
                # Decode the small FF parameter object, not the cell. Keep JSON
                # booleans/large integers exact (json_each coerces these).
                value = json.loads(parameters) if parameters is not None else {}
                if isinstance(value, dict):
                    result[name]["parameters"] = {
                        key: item for key, item in value.items() if key.startswith("IS_")
                    }
        return result

    def packing_cascade_items(
        self, cascade_ports: Mapping[str, Sequence[Tuple[str, str, str, Optional[int]]]],
    ) -> Iterator[Tuple[str, Mapping[str, Any]]]:
        """Stream dedicated-cascade pins without decoding hard-block INITs."""
        ports = sorted({
            port for specs in cascade_ports.values()
            for _, output, input_, _ in specs for port in (output, input_)
        })
        port_placeholders = ",".join("?" for _ in ports)
        type_placeholders = ",".join("?" for _ in cascade_ports)
        rows = self.connection.execute(
            "SELECT c.id,c.type,p.port,COALESCE(p.net_bit,p.constant) FROM cells c "
            "LEFT JOIN cell_pins p ON p.cell_seq=c.seq "
            f"AND p.port IN ({port_placeholders}) WHERE c.type IN ({type_placeholders}) "
            "ORDER BY c.id,p.pin_seq", (*ports, *cascade_ports),
        )
        previous = None
        record: Dict[str, Any] = {}
        for name, cell_type, port, bit in rows:
            if name != previous:
                if previous is not None:
                    yield previous, record
                previous = name
                record = {"type": cell_type, "connections": {}}
            if port is not None:
                record["connections"].setdefault(port, []).append(bit)
        if previous is not None:
            yield previous, record

    def packing_drivers(self) -> Mapping[int, str]:
        duplicate = self.connection.execute(
            "SELECT net_bit FROM cell_pins WHERE net_bit IS NOT NULL "
            "AND direction='output' GROUP BY net_bit HAVING count(*)>1 LIMIT 1"
        ).fetchone()
        if duplicate is not None:
            raise ValidationError(f"mapped net bit {duplicate[0]} has multiple drivers")
        return _IndexedDrivers(self.connection)

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

    def type_items_by_id(self) -> Iterator[Tuple[str, str]]:
        """Project canonical singleton identity without decoding cell payloads."""
        if self.table != "cells":
            raise ValidationError("primitive type projection requires cells")
        for name, cell_type in self.connection.execute(
            "SELECT id,type FROM cells ORDER BY id"
        ):
            yield str(name), str(cell_type)

    def values(self) -> Iterator[Mapping[str, Any]]:  # type: ignore[override]
        for _name, value in self.items():
            yield value

    def type_counts(self) -> Mapping[str, int]:
        if self.table != "cells":
            raise ValidationError("primitive inventory requires cells")
        return dict(self.connection.execute(
            "SELECT type,count(*) FROM cells GROUP BY type ORDER BY type"
        ))

    def audit_type_counts(self) -> Mapping[str, int]:
        """Audit primitive record shape in SQLite, returning only the inventory."""
        if self.table != "cells":
            raise ValidationError("primitive audit requires cells")
        invalid = self.connection.execute("""
            SELECT id, problem FROM (
                SELECT id, CASE
                    WHEN NOT json_valid(record_json) THEN 'record'
                    WHEN json_type(record_json) != 'object' THEN 'record'
                    WHEN COALESCE(json_type(record_json,'$.type'),'') != 'text'
                        OR json_extract(record_json,'$.type') = '' THEN 'type'
                    WHEN json_extract(record_json,'$.type') != type THEN 'type index'
                    WHEN COALESCE(json_type(record_json,'$.port_directions'),'') != 'object'
                        OR COALESCE(json_type(record_json,'$.connections'),'') != 'object'
                        THEN 'port metadata'
                END AS problem FROM cells
            ) WHERE problem IS NOT NULL LIMIT 1
        """).fetchone()
        if invalid is not None:
            raise ValidationError(f"mapped cell {invalid[0]!r} has invalid {invalid[1]}")
        invalid = self.connection.execute("""
            SELECT c.id FROM cells c,
                json_each(c.record_json,'$.port_directions') AS p
            WHERE p.type != 'text' OR p.value NOT IN ('input','output','inout')
            LIMIT 1
        """).fetchone()
        if invalid is not None:
            raise ValidationError(f"mapped cell {invalid[0]!r} has invalid port directions")
        return self.type_counts()

    def items_of_types(
        self, cell_types: set[str],
    ) -> Iterator[Tuple[str, Mapping[str, Any]]]:
        if not cell_types:
            return
        placeholders = ",".join("?" for _ in cell_types)
        for name, record in self.connection.execute(
            f"SELECT id,record_json FROM cells WHERE type IN ({placeholders}) ORDER BY id",
            tuple(sorted(cell_types)),
        ):
            yield str(name), json.loads(record)

    def physical_macro_projection(
        self, root_types: set[str],
    ) -> Mapping[str, Mapping[str, Any]]:
        """Resolve exceptional physical macros in SQL before decoding records.

        MUXF9 requires at most three driver steps to its LUT leaves; a CARRY8
        requires one step to its LUT6_2 adapters. All dedicated hard blocks
        are roots. Ordinary logic never enters this projection.
        """
        if self.table != "cells":
            raise ValidationError("physical macro projection requires cells")
        duplicate = self.connection.execute(
            "SELECT net_bit FROM cell_pins WHERE net_bit IS NOT NULL "
            "AND direction='output' GROUP BY net_bit HAVING count(*)>1 LIMIT 1"
        ).fetchone()
        if duplicate is not None:
            raise ValidationError(
                f"mapped net bit {duplicate[0]} has multiple drivers"
            )
        placeholders = ",".join("?" for _ in root_types)
        return {
            str(name): json.loads(record)
            for name, record in self.connection.execute(
                "WITH RECURSIVE members(seq,depth) AS ("
                f"SELECT seq,0 FROM cells WHERE type IN ({placeholders}) "
                "UNION SELECT d.cell_seq,m.depth+1 FROM members m "
                "JOIN cells c ON c.seq=m.seq "
                "JOIN cell_pins p ON p.cell_seq=m.seq "
                "JOIN cell_pins d ON d.net_bit=p.net_bit AND d.direction='output' "
                "AND d.net_bit IS NOT NULL "
                "WHERE m.depth<3 AND p.direction='input' AND p.net_bit IS NOT NULL "
                "AND ((c.type IN ('MUXF7','MUXF8','MUXF9') AND p.port IN ('I0','I1')) "
                "OR (c.type='CARRY8' AND p.port IN ('DI','S')))) "
                "SELECT DISTINCT c.id,c.record_json FROM members m "
                "JOIN cells c ON c.seq=m.seq ORDER BY c.id",
                tuple(sorted(root_types)),
            )
        }


class _IndexedDrivers(MappingABC[int, str]):
    """Macro-only point queries into the normalized output-pin index."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def __getitem__(self, bit: int) -> str:
        if not isinstance(bit, int):
            # SQLite numeric affinity must not turn a constant token "0" into
            # integer net 0; the reference driver map only owns integer nets.
            raise KeyError(bit)
        row = self.connection.execute(
            "SELECT c.id FROM cell_pins p JOIN cells c ON c.seq=p.cell_seq "
            "WHERE p.net_bit=? AND p.direction='output'", (bit,),
        ).fetchone()
        if row is None:
            raise KeyError(bit)
        return str(row[0])

    def __iter__(self) -> Iterator[int]:
        for row in self.connection.execute(
            "SELECT net_bit FROM cell_pins WHERE net_bit IS NOT NULL "
            "AND direction='output' ORDER BY net_bit"
        ):
            yield int(row[0])

    def __len__(self) -> int:
        return int(self.connection.execute(
            "SELECT count(*) FROM cell_pins WHERE net_bit IS NOT NULL AND direction='output'"
        ).fetchone()[0])


class IndexedXilinxMappedNetlist(MappingABC[str, Any]):
    """Bounded read interface for a production Xilinx mapped netlist."""

    def __init__(self, path: Path):
        self.path = path
        self.connection = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro&immutable=1",
            uri=True,
            check_same_thread=False,
        )
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key='schema'"
        ).fetchone()
        if row is None or row[0] != XILINX_MAPPED_INDEX_SCHEMA:
            self.connection.close()
            raise ValidationError("Xilinx mapped index schema is invalid")
        metadata = dict(self.connection.execute("SELECT key,value FROM metadata"))
        self.top = json.loads(metadata["top_json"])
        self.creator = json.loads(metadata["creator_json"])
        self.ports = json.loads(metadata["ports_json"])
        if not isinstance(self.top, str) or not isinstance(self.ports, dict):
            self.connection.close()
            raise ValidationError("Xilinx mapped index metadata is invalid")
        self.cells = _IndexedRecords(self.connection, "cells")
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
        return row is not None and row[0] == XILINX_MAPPED_INDEX_SCHEMA
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


def _populate_native_xilinx_index(
    index: sqlite3.Connection, ir_path: Path,
) -> Tuple[int, int]:
    """Bulk-project an already mapped circuit without Python cell records."""

    index.execute(
        "ATTACH DATABASE ? AS emuir_source",
        (f"file:{ir_path.resolve()}?mode=ro&immutable=1",),
    )
    index.execute("PRAGMA emuir_source.cache_size=-65536")
    orphan = index.execute(
        "SELECT e.instance_seq FROM emuir_source.endpoint_records e "
        "LEFT JOIN emuir_source.cell_records c ON c.seq=e.instance_seq "
        "WHERE e.instance_seq IS NOT NULL AND c.seq IS NULL LIMIT 1"
    ).fetchone()
    if orphan is not None:
        raise ValidationError("Xilinx mapped endpoint has an orphaned instance")
    index.executescript("""
        BEGIN;
        CREATE TEMP TABLE native_raw_pins AS
        SELECT e.instance_seq AS cell_seq,p.name AS port,e.bit AS bit_index,
               e.net_seq+2 AS net_bit,NULL AS constant,
               CASE WHEN e.role=0 THEN 'output' ELSE 'input' END AS direction
        FROM emuir_source.endpoint_records e
        JOIN emuir_source.pin_names p ON p.seq=e.port_seq
        WHERE e.instance_seq IS NOT NULL
        UNION ALL
        SELECT c.seq,json_extract(k.value,'$.port'),
               json_extract(k.value,'$.bit'),NULL,
               json_extract(k.value,'$.value'),'input'
        FROM emuir_source.cell_records c
        JOIN emuir_source.payloads p ON p.seq=c.constants_seq,
             json_each(p.value) k;
        CREATE TEMP TABLE native_pins AS
        SELECT cell_seq,port,bit_index,min(net_bit) AS net_bit,
               min(constant) AS constant,
               CASE WHEN count(DISTINCT direction)>1 THEN 'inout'
                    ELSE min(direction) END AS direction,
               count(DISTINCT net_bit) AS net_variants,
               count(DISTINCT constant) AS constant_variants
        FROM native_raw_pins GROUP BY cell_seq,port,bit_index;
        DROP TABLE native_raw_pins;
    """)
    conflict = index.execute(
        "SELECT cell_seq,port,bit_index FROM native_pins "
        "WHERE net_variants>1 OR constant_variants>1 "
        "OR (net_bit IS NOT NULL AND constant IS NOT NULL) LIMIT 1"
    ).fetchone()
    if conflict is not None:
        raise ValidationError(
            "Xilinx mapped pin has multiple nets or conflicting constant and net: "
            f"{tuple(conflict)!r}"
        )
    gap = index.execute(
        "SELECT cell_seq,port FROM native_pins GROUP BY cell_seq,port "
        "HAVING min(bit_index)<>0 OR max(bit_index)+1<>count(*) LIMIT 1"
    ).fetchone()
    if gap is not None:
        raise ValidationError(f"Xilinx mapped port is not contiguous: {tuple(gap)!r}")
    index.executescript("""
        CREATE TEMP TABLE native_ports AS
        SELECT cell_seq,port,max(bit_index)+1 AS width,
               json_group_array(CASE WHEN net_bit IS NOT NULL THEN net_bit
                                     ELSE constant END) AS connections_json,
               CASE WHEN count(DISTINCT direction)>1 THEN 'inout'
                    ELSE min(direction) END AS direction
        FROM (SELECT * FROM native_pins ORDER BY cell_seq,port,bit_index)
        GROUP BY cell_seq,port;
        CREATE UNIQUE INDEX native_ports_owner ON native_ports(cell_seq,port);
        INSERT INTO cell_pins
        SELECT p.cell_seq,
               row_number() OVER (PARTITION BY p.cell_seq ORDER BY p.port,p.bit_index)-1,
               p.port,p.bit_index,
               CASE WHEN r.width=1 THEN p.port
                    ELSE p.port||'['||p.bit_index||']' END,
               r.direction,p.net_bit,p.constant
        FROM native_pins p JOIN native_ports r USING(cell_seq,port)
        ORDER BY p.cell_seq,p.port,p.bit_index;
        CREATE TEMP TABLE native_cell_ports AS
        SELECT cell_seq,
               json_group_object(port,json(connections_json)) AS connections_json,
               json_group_object(port,direction) AS directions_json
        FROM (SELECT * FROM native_ports ORDER BY cell_seq,port)
        GROUP BY cell_seq;
        CREATE UNIQUE INDEX native_cell_ports_owner ON native_cell_ports(cell_seq);
        INSERT INTO cells
        SELECT c.id,c.seq,c.type,
               json_object(
                   'attributes',json(COALESCE(a.value,'{}')),
                   'connections',json(COALESCE(r.connections_json,'{}')),
                   'hide_name',0,
                   'parameters',json(COALESCE(p.value,'{}')),
                   'port_directions',json(COALESCE(r.directions_json,'{}')),
                   'type',c.type)
        FROM emuir_source.cell_records c
        LEFT JOIN emuir_source.payloads p ON p.seq=c.parameters_seq
        LEFT JOIN emuir_source.payloads a ON a.seq=c.attributes_seq
        LEFT JOIN native_cell_ports r ON r.cell_seq=c.seq
        ORDER BY c.seq;
        INSERT INTO netnames
        SELECT id,seq,json_object('attributes',json('{}'),'bits',json_array(seq+2),
                                  'hide_name',0)
        FROM emuir_source.net_records ORDER BY seq;
        DROP TABLE native_cell_ports;
        DROP TABLE native_ports;
        DROP TABLE native_pins;
    """)
    return (
        int(index.execute("SELECT count(*) FROM cells").fetchone()[0]),
        int(index.execute("SELECT count(*) FROM netnames").fetchone()[0]),
    )


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
            PRAGMA cache_size=-65536;
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)
              WITHOUT ROWID;
            CREATE TABLE cells(
              id TEXT PRIMARY KEY,seq INTEGER NOT NULL UNIQUE,type TEXT NOT NULL,
              record_json TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE cell_pins(
              cell_seq INTEGER NOT NULL,
              pin_seq INTEGER NOT NULL,
              port TEXT NOT NULL,
              bit_index INTEGER NOT NULL,
              pin_name TEXT NOT NULL,
              direction TEXT NOT NULL,
              net_bit INTEGER,
              constant TEXT,
              PRIMARY KEY(cell_seq,pin_seq),
              CHECK((net_bit IS NULL) != (constant IS NULL))
            ) WITHOUT ROWID;
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

        inventory: Counter[str] = Counter()
        hard_blocks: Counter[str] = Counter()

        def cells() -> Iterator[Tuple[str, Mapping[str, Any]]]:
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
                index.execute(
                    "INSERT INTO cells VALUES (?,?,?,?)",
                    (str(row["id"]), int(row["seq"]), cell_type, _json(cell)),
                )
                pin_rows = []
                pin_seq = 0
                for port, values in sorted(connections.items()):
                    direction = directions[port]
                    for bit_index, value in enumerate(values):
                        pin_rows.append((
                            int(row["seq"]), pin_seq, port, bit_index,
                            (
                                port if len(values) == 1
                                else f"{port}[{bit_index}]"
                            ),
                            direction,
                            (
                                value
                                if isinstance(value, int)
                                and not isinstance(value, bool) else None
                            ),
                            (
                                None
                                if isinstance(value, int)
                                and not isinstance(value, bool) else str(value)
                            ),
                        ))
                        pin_seq += 1
                index.executemany(
                    "INSERT INTO cell_pins VALUES (?,?,?,?,?,?,?,?)",
                    pin_rows,
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

        source_inventory = Counter(dict(source.execute(
            "SELECT type,count(*) FROM cell_records GROUP BY type"
        )))
        if output_temp is None and set(source_inventory) <= native_xilinx_cells:
            cell_count, emitted_net_count = _populate_native_xilinx_index(index, ir_path)
            inventory.update(source_inventory)
            hard_blocks.update({
                cell_type: count for cell_type, count in inventory.items()
                if contracts[cell_type]["hard_block"]
            })
        elif output_temp is None:
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
        # Build net-order B-trees after the sequential pin import. Maintaining
        # this index for every pin causes random NFS page updates in the bulk
        # producer just as it did in the old Python per-cell loop.
        index.execute(
            "CREATE INDEX cell_pins_by_net ON cell_pins(net_bit,cell_seq,pin_seq) "
            "WHERE net_bit IS NOT NULL"
        )
        index.execute("CREATE INDEX cells_by_type ON cells(type,id)")
        index.execute(
            "CREATE INDEX cell_pins_by_driver ON cell_pins(net_bit,cell_seq,pin_seq) "
            "WHERE net_bit IS NOT NULL AND direction='output'"
        )
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
            "index_sha256": (
                output_sha256 if output_path is None else file_sha256(index_path)
            ),
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
