"""Provider-neutral EmuIR to normalized Xilinx Yosys JSON lowering."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping as MappingABC
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from .sqlite_artifacts import immutable_sqlite_uri
from .emuir_compact import compact_metadata, is_compact_emuir
from .emuir_storage import _RowsForOwner, _decode, open_emuir_sqlite_read_only
from .equivalence import _lut_definition
from .errors import ValidationError
from .store_publication import (
    StoreSource,
    bind_store_source,
    publish_sqlite_store,
    require_store_sources_unchanged,
    validate_store_destination,
)
from .io import file_sha256, write_json
from .native_tools import native_file_generation
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

    def types_by_id(self) -> Iterator[Tuple[str, str]]:
        """Stream the source-owned cell identity/type projection only."""

        for row in self.connection.execute(
            "SELECT id,type FROM cells ORDER BY id"
        ):
            yield str(row[0]), str(row[1])

    def items_of_types(
        self, cell_types: Iterable[str]
    ) -> Iterator[Tuple[str, Mapping[str, Any]]]:
        """Reconstruct only a requested physical subgraph from its typed owner."""
        kinds = tuple(sorted(set(cell_types)))
        if not kinds:
            return
        if len(kinds) > 64 or any(
            not isinstance(kind, str) or not kind for kind in kinds
        ):
            raise ValidationError("mapped primitive projection is invalid")
        slots = ",".join("?" for _ in kinds)
        pins = _RowsForOwner(
            self.connection.execute(
                "SELECT p.* FROM cell_pins p JOIN cells c ON c.seq=p.cell_seq "
                f"WHERE c.type IN ({slots}) ORDER BY p.cell_seq,p.port,p.port_bit",
                kinds,
            ),
            "cell_seq",
        )
        for row in self.connection.execute(
            f"SELECT id,seq,record_json FROM cells WHERE type IN ({slots}) ORDER BY seq",
            kinds,
        ):
            value = json.loads(row["record_json"])
            if not isinstance(value, dict):
                raise ValidationError(f"indexed mapped record {row['id']!r} is invalid")
            yield str(row["id"]), self._with_pins(
                value, iter(pins.take(int(row["seq"])))
            )
        pins.finish()

    def output_drivers(self, required_bits: Iterable[int]) -> Dict[int, str]:
        """Read the bounded topology subgraph after complete native validation."""
        drivers = {}
        for bit in sorted(required_bits):
            rows = self.connection.execute(
                "SELECT cell_id FROM cell_pins WHERE value_kind=0 AND direction=1 AND net_bit=? LIMIT 2",
                (bit,),
            ).fetchall()
            if len(rows) > 1:
                raise ValidationError(f"mapped net bit {bit} has multiple drivers")
            if rows:
                drivers[bit] = str(rows[0][0])
        return drivers


class IndexedXilinxMappedNetlist(MappingABC[str, Any]):
    """Bounded read interface for a production Xilinx mapped netlist."""

    def __init__(self, path: Path):
        self.path = path
        self.source_generation = native_file_generation(path)
        self.connection = sqlite3.connect(
            immutable_sqlite_uri(path),
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
        self._validated_physical_paths: set[Tuple[Path, int, int]] = set()
        # A private, detached structural result for this immutable reader only.
        # It is never serialized or shared with another reader/experiment.
        self._derived_physical_macro_contract: Optional[Dict[str, Any]] = None
        try:
            self.require_source_unchanged()
        except BaseException:
            self.connection.close()
            raise

    @property
    def store_path(self):
        return self.path.resolve()

    def bound_store_sources(self):
        if self.connection is None:
            raise ValidationError("Xilinx mapped index is closed")
        self.require_source_unchanged()
        if self.schema != XILINX_MAPPED_INDEX_SCHEMA:
            raise ValidationError(
                "typed publication requires the current mapped-index owner"
            )
        return (
            StoreSource(
                self.store_path, "xilinx-mapped-index-v2", self.source_generation
            ),
        )

    def require_source_unchanged(self) -> None:
        """A leased reader must still refer to the generation at its path."""
        try:
            if native_file_generation(self.path) != self.source_generation:
                raise ValidationError("indexed mapped source generation changed")
        except OSError as error:
            raise ValidationError("indexed mapped source generation changed") from error

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

    def iter_cells_by_type(
        self, cell_type: str,
    ) -> Iterator[Tuple[str, Mapping[str, Any]]]:
        """Stream one primitive family without reconstructing every cell."""

        if not isinstance(cell_type, str) or not cell_type:
            raise ValidationError("indexed mapped cell type is invalid")
        if self.schema != XILINX_MAPPED_INDEX_SCHEMA:
            for name, cell in self.cells.items():
                if cell.get("type") == cell_type:
                    yield name, cell
            return
        pins = _RowsForOwner(
            self.connection.execute(
                """
                SELECT p.*
                  FROM cell_pins AS p
                  JOIN cells AS c ON c.seq=p.cell_seq
                 WHERE c.type=?
                 ORDER BY p.cell_seq,p.port,p.port_bit
                """,
                (cell_type,),
            ),
            "cell_seq",
        )
        for row in self.connection.execute(
            """
            SELECT id,seq,record_json
              FROM cells
             WHERE type=?
             ORDER BY seq
            """,
            (cell_type,),
        ):
            value = json.loads(row["record_json"])
            if not isinstance(value, dict):
                raise ValidationError(
                    f"indexed mapped record {row['id']!r} is invalid"
                )
            yield str(row["id"]), _IndexedCells._with_pins(
                value, iter(pins.take(int(row["seq"])))
            )
        pins.finish()

    def cell_type_counts(self) -> Dict[str, int]:
        """Return the typed primitive inventory without rebuilding cell pins."""

        if self.schema == XILINX_MAPPED_INDEX_SCHEMA:
            return {
                str(row["type"]): int(row["records"])
                for row in self.connection.execute(
                    """
                    SELECT type,count(*) AS records
                      FROM cells
                     GROUP BY type
                     ORDER BY type
                    """
                )
            }
        counts: Counter[str] = Counter()
        for cell in self.cells.values():
            cell_type = cell.get("type")
            if not isinstance(cell_type, str) or not cell_type:
                raise ValidationError("indexed mapped cell type is invalid")
            counts[cell_type] += 1
        return dict(counts)

    def cell_type_and_parameters(
        self, name: str,
    ) -> Tuple[str, Mapping[str, Any]]:
        """Read only the sparse logical metadata needed by physical adapters."""

        if not isinstance(name, str) or not name:
            raise ValidationError("indexed mapped cell identity is invalid")
        if self.schema != XILINX_MAPPED_INDEX_SCHEMA:
            cell = self.cells[name]
            cell_type = cell.get("type")
            parameters = cell.get("parameters", {})
        else:
            row = self.connection.execute(
                "SELECT type,record_json FROM cells WHERE id=?", (name,)
            ).fetchone()
            if row is None:
                raise KeyError(name)
            record = json.loads(row["record_json"])
            if not isinstance(record, dict):
                raise ValidationError(
                    f"indexed mapped record {name!r} is invalid"
                )
            cell_type = str(row["type"])
            if record.get("type") != cell_type:
                raise ValidationError(
                    f"indexed mapped record {name!r} type projection disagrees"
                )
            parameters = record.get("parameters", {})
        if not isinstance(cell_type, str) or not cell_type:
            raise ValidationError("indexed mapped cell type is invalid")
        if not isinstance(parameters, MappingABC):
            raise ValidationError(
                f"indexed mapped record {name!r} parameters are invalid"
            )
        return cell_type, parameters

    def _physical_connection(self, handoff_path: Path) -> sqlite3.Connection:
        if self.schema != XILINX_MAPPED_INDEX_SCHEMA:
            raise ValidationError(
                "legacy Xilinx mapped index has no typed pin relation"
            )
        connection = sqlite3.connect(
            immutable_sqlite_uri(self.path), uri=True
        )
        connection.row_factory = sqlite3.Row
        try:
            handoff_stat = handoff_path.stat()
            handoff_key = (
                handoff_path.resolve(), handoff_stat.st_size,
                handoff_stat.st_mtime_ns,
            )
            connection.execute(
                "ATTACH DATABASE ? AS physical",
                (
                    immutable_sqlite_uri(handoff_path),
                ),
            )
            if handoff_key not in self._validated_physical_paths:
                assignments = int(connection.execute(
                    "SELECT count(*) FROM physical.assignment"
                ).fetchone()[0])
                joined, mismatched = connection.execute(
                    """
                    SELECT count(*),
                           COALESCE(sum(c.type != a.cell_type),0)
                      FROM physical.assignment AS a
                      JOIN cells AS c ON c.id=a.instance
                    """
                ).fetchone()
                if int(joined) != assignments or int(mismatched) != 0:
                    raise ValidationError(
                        "mapped index and physical handoff cell ownership disagree"
                    )
                self._validated_physical_paths.add(handoff_key)
        except BaseException:
            connection.close()
            raise
        return connection

    def iter_physical_cells(
        self, handoff_path: Path,
    ) -> Iterator[Tuple[str, str, str, str]]:
        """Stream mapped cell type plus exact site/BEL from one handoff."""

        connection = self._physical_connection(handoff_path)
        try:
            for row in connection.execute(
                """
                SELECT a.instance,c.type,a.physical_site,a.bel
                  FROM physical.assignment AS a
                  JOIN cells AS c ON c.id=a.instance
                 ORDER BY a.instance
                """
            ):
                yield (
                    str(row["instance"]), str(row["type"]),
                    str(row["physical_site"]), str(row["bel"]),
                )
        finally:
            connection.close()

    def iter_physical_pins_by_net(
        self, handoff_path: Path,
    ) -> Iterator[Tuple[int, str, str, int, int, str, str]]:
        """Stream placed typed pins by routed net without Python ownership."""

        connection = self._physical_connection(handoff_path)
        direction_names = {0: "input", 1: "output", 2: "inout"}
        try:
            for row in connection.execute(
                """
                SELECT p.net_bit,p.cell_id,p.port,p.port_bit,p.port_width,
                       p.direction,a.physical_site
                  FROM cell_pins AS p
                  JOIN physical.assignment AS a ON a.instance=p.cell_id
                 WHERE p.value_kind=0
                 ORDER BY p.net_bit,p.cell_id,p.port,p.port_bit
                """
            ):
                direction = direction_names.get(int(row["direction"]))
                if direction is None:
                    raise ValidationError(
                        "indexed mapped pin direction is invalid"
                    )
                yield (
                    int(row["net_bit"]), str(row["cell_id"]),
                    str(row["port"]), int(row["port_bit"]),
                    int(row["port_width"]), direction,
                    str(row["physical_site"]),
                )
        finally:
            connection.close()

    def iter_physical_route_pins_by_net(
        self, handoff_path: Path, *, ordinal_offset: int = 0,
    ) -> Iterator[
        Tuple[int, str, str, int, int, str, str, Optional[int], str]
    ]:
        """Stream placed pins with their deterministic RWRoute cell ordinal.

        The physical assignment and mapped cell tables are already ordered by
        instance identity.  Compute the cumulative route-cell count in SQLite
        so the production exporter does not retain a graph-sized
        ``instance -> cN`` dictionary merely to refer to cells again while
        emitting nets.  LUT6_2 occupies two consecutive RapidWright cells;
        its four routable logical pins are mapped to the same O5/O6 split used
        by the CELL rows.  A null ordinal deliberately preserves fail-closed
        handling for any other non-constant LUT6_2 pin.
        """

        for (
            bit, cell_id, _cell_type, port, port_bit, port_width, direction,
            physical_site, route_ordinal, route_pin,
        ) in self.iter_physical_route_endpoints_by_net(
            handoff_path, ordinal_offset=ordinal_offset
        ):
            yield (
                bit, cell_id, port, port_bit, port_width, direction,
                physical_site, route_ordinal, route_pin,
            )

    def iter_physical_route_endpoints_by_net(
        self, handoff_path: Path, *, ordinal_offset: int = 0,
    ) -> Iterator[
        Tuple[int, str, str, str, int, int, str, str, Optional[int], str]
    ]:
        """Stream placed route pins together with their typed cell identity.

        RWRoute needs the driver primitive type for CARRY8 exit validation.
        Carry it in the existing mapped/physical join so the net loop never
        performs a per-net cell JSON lookup or reconstructs that cell's pins.
        """

        if (
            isinstance(ordinal_offset, bool)
            or not isinstance(ordinal_offset, int)
            or ordinal_offset < 0
        ):
            raise ValidationError("RWRoute ordinal offset is invalid")
        connection = self._physical_connection(handoff_path)
        direction_names = {0: "input", 1: "output", 2: "inout"}
        try:
            for row in connection.execute(
                """
                WITH physical_cells AS (
                  SELECT a.instance,c.type,a.physical_site,
                         ? + sum(CASE WHEN c.type='LUT6_2' THEN 2 ELSE 1 END)
                           OVER (
                             ORDER BY a.instance
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                           )
                           - CASE WHEN c.type='LUT6_2' THEN 2 ELSE 1 END
                           AS route_ordinal
                    FROM physical.assignment AS a
                    JOIN cells AS c ON c.id=a.instance
                )
                SELECT p.net_bit,p.cell_id,p.port,p.port_bit,p.port_width,
                       p.direction,r.physical_site,r.type,r.route_ordinal
                  FROM cell_pins AS p
                  JOIN physical_cells AS r ON r.instance=p.cell_id
                 WHERE p.value_kind=0
                 ORDER BY p.net_bit,p.cell_id,p.port,p.port_bit
                """,
                (ordinal_offset,),
            ):
                direction = direction_names.get(int(row["direction"]))
                if direction is None:
                    raise ValidationError(
                        "indexed mapped pin direction is invalid"
                    )
                cell_type = str(row["type"])
                port = str(row["port"])
                route_ordinal: Optional[int] = int(row["route_ordinal"])
                route_pin = (
                    port if int(row["port_width"]) == 1
                    else f"{port}[{int(row['port_bit'])}]"
                )
                if cell_type == "LUT6_2":
                    if port == "I0":
                        route_pin = "I0"
                    elif port == "O5":
                        route_pin = "O"
                    elif port == "I1":
                        route_ordinal += 1
                        route_pin = "I1"
                    elif port == "O6":
                        route_ordinal += 1
                        route_pin = "O"
                    else:
                        route_ordinal = None
                yield (
                    int(row["net_bit"]), str(row["cell_id"]), cell_type, port,
                    int(row["port_bit"]), int(row["port_width"]), direction,
                    str(row["physical_site"]), route_ordinal, route_pin,
                )
        finally:
            connection.close()

    def iter_qualified_lut_clock_gates(
        self,
        handoff_path: Path,
        *,
        ordinal_offset: int = 0,
        io_endpoints: Iterable[Tuple[int, str, str, str]] = (),
    ) -> Iterator[Tuple[str, int]]:
        """Qualify routed LUT2 clock gates with file-backed set operations.

        The compatibility exporter reconstructs candidate cells and all pins
        on their three nets in Python.  On a large mapped design that creates
        another LUT/endpoint-sized object graph immediately before RWRoute.
        The indexed production path already owns the same facts in ``cells``,
        ``cell_pins`` and the physical handoff.  Keep candidates and the
        transitive closed-chain qualification in SQLite and return only the
        gates that RapidWright must treat specially.
        """

        if (
            isinstance(ordinal_offset, bool)
            or not isinstance(ordinal_offset, int)
            or ordinal_offset < 0
        ):
            raise ValidationError("RWRoute ordinal offset is invalid")
        connection = self._physical_connection(handoff_path)
        try:
            connection.execute("PRAGMA temp_store=FILE")
            connection.executescript(
                """
                CREATE TEMP TABLE rwroute_io_endpoints(
                  net_bit INTEGER NOT NULL,
                  seq INTEGER NOT NULL,
                  cell_id TEXT NOT NULL,
                  pin TEXT NOT NULL,
                  role INTEGER NOT NULL CHECK(role IN (0,1)),
                  PRIMARY KEY(net_bit,seq)
                ) WITHOUT ROWID;
                CREATE TEMP TABLE rwroute_clock_candidates(
                  name TEXT PRIMARY KEY,
                  root_bit INTEGER NOT NULL,
                  enable_bit INTEGER NOT NULL,
                  output_bit INTEGER NOT NULL,
                  route_ordinal INTEGER NOT NULL
                ) WITHOUT ROWID;
                """
            )
            io_batch = []
            for seq, endpoint in enumerate(io_endpoints):
                if (
                    not isinstance(endpoint, tuple)
                    or len(endpoint) != 4
                ):
                    raise ValidationError(
                        "RWRoute indexed I/O endpoint is invalid"
                    )
                bit, cell_id, pin, role = endpoint
                if (
                    isinstance(bit, bool)
                    or not isinstance(bit, int)
                    or not isinstance(cell_id, str)
                    or not isinstance(pin, str)
                    or role not in {"driver", "sink"}
                ):
                    raise ValidationError(
                        "RWRoute indexed I/O endpoint is invalid"
                    )
                io_batch.append(
                    (bit, seq, cell_id, pin, 0 if role == "driver" else 1)
                )
            connection.executemany(
                "INSERT INTO rwroute_io_endpoints VALUES (?,?,?,?,?)",
                io_batch,
            )

            # Parse only one LUT2 row at a time.  ``record_json`` contains the
            # small parameter payload while the routed pin identities remain
            # typed columns.  The window expression exactly matches the CELL
            # ordinal stream, including the two slots consumed by LUT6_2.
            candidate_batch = []
            rows = connection.execute(
                """
                WITH physical_cells AS (
                  SELECT a.instance,c.type,
                         ? + sum(CASE WHEN c.type='LUT6_2' THEN 2 ELSE 1 END)
                           OVER (
                             ORDER BY a.instance
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                           )
                           - CASE WHEN c.type='LUT6_2' THEN 2 ELSE 1 END
                           AS route_ordinal
                    FROM physical.assignment AS a
                    JOIN cells AS c ON c.id=a.instance
                )
                SELECT c.id,c.record_json,r.route_ordinal,
                       sum(CASE WHEN p.port='I0' THEN 1 ELSE 0 END) AS i0_rows,
                       sum(CASE WHEN p.port='I1' THEN 1 ELSE 0 END) AS i1_rows,
                       sum(CASE WHEN p.port='O' THEN 1 ELSE 0 END) AS o_rows,
                       max(CASE WHEN p.port='I0' AND p.port_width=1
                                     AND p.port_bit=0 AND p.value_kind=0
                                THEN p.net_bit END) AS root_bit,
                       max(CASE WHEN p.port='I1' AND p.port_width=1
                                     AND p.port_bit=0 AND p.value_kind=0
                                THEN p.net_bit END) AS enable_bit,
                       max(CASE WHEN p.port='O' AND p.port_width=1
                                     AND p.port_bit=0 AND p.value_kind=0
                                THEN p.net_bit END) AS output_bit
                  FROM cells AS c
             LEFT JOIN physical_cells AS r ON r.instance=c.id
             LEFT JOIN cell_pins AS p ON p.cell_id=c.id
                 WHERE c.type='LUT2'
                 GROUP BY c.id,c.record_json,r.route_ordinal
                 ORDER BY c.id
                """,
                (ordinal_offset,),
            )
            for row in rows:
                record = json.loads(row["record_json"])
                if not isinstance(record, dict):
                    raise ValidationError(
                        f"indexed mapped record {row['id']!r} is invalid"
                    )
                parameters = record.get("parameters", {})
                if not isinstance(parameters, MappingABC):
                    raise ValidationError(
                        "primitive parameter 'INIT' has invalid type"
                    )
                value = parameters.get("INIT")
                if value is None:
                    continue
                if isinstance(value, bool):
                    normalized = "1" if value else "0"
                elif isinstance(value, int):
                    normalized = str(value)
                elif isinstance(value, str):
                    if value and set(value) <= {"0", "1"}:
                        normalized = str(int(value, 2))
                    elif any(character in value for character in "\t\r\n"):
                        raise ValidationError(
                            "primitive parameter 'INIT' is not TSV-safe"
                        )
                    else:
                        normalized = value
                else:
                    raise ValidationError(
                        "primitive parameter 'INIT' has invalid type"
                    )
                if normalized != "8":
                    continue
                if (
                    row["route_ordinal"] is None
                    or int(row["i0_rows"]) != 1
                    or int(row["i1_rows"]) != 1
                    or int(row["o_rows"]) != 1
                    or row["root_bit"] is None
                    or row["enable_bit"] is None
                    or row["output_bit"] is None
                ):
                    continue
                candidate_batch.append(
                    (
                        str(row["id"]), int(row["root_bit"]),
                        int(row["enable_bit"]), int(row["output_bit"]),
                        int(row["route_ordinal"]),
                    )
                )
                if len(candidate_batch) >= 4096:
                    connection.executemany(
                        "INSERT INTO rwroute_clock_candidates VALUES (?,?,?,?,?)",
                        candidate_batch,
                    )
                    candidate_batch.clear()
            connection.executemany(
                "INSERT INTO rwroute_clock_candidates VALUES (?,?,?,?,?)",
                candidate_batch,
            )
            connection.executescript(
                """
                CREATE TEMP VIEW rwroute_clock_endpoints AS
                SELECT p.net_bit,p.cell_id,
                       CASE WHEN p.port_width=1 THEN p.port
                            ELSE p.port || '[' || p.port_bit || ']' END AS pin,
                       CASE WHEN p.direction IN (1,2) THEN 0 ELSE 1 END AS role
                  FROM cell_pins AS p
                  JOIN physical.assignment AS a ON a.instance=p.cell_id
                 WHERE p.value_kind=0
                UNION ALL
                SELECT net_bit,cell_id,pin,role
                  FROM rwroute_io_endpoints;

                DELETE FROM rwroute_clock_candidates AS c
                 WHERE (SELECT count(*) FROM rwroute_clock_endpoints AS e
                         WHERE e.net_bit=c.enable_bit AND e.role=0) != 1
                    OR NOT EXISTS (
                         SELECT 1
                           FROM rwroute_clock_endpoints AS e
                           JOIN cells AS d ON d.id=e.cell_id
                          WHERE e.net_bit=c.enable_bit AND e.role=0
                            AND d.type='FDRE' AND e.pin='Q'
                       );

                CREATE TEMP TABLE rwroute_qualified_clock_gates(
                  name TEXT PRIMARY KEY,
                  root_bit INTEGER NOT NULL,
                  output_bit INTEGER NOT NULL,
                  route_ordinal INTEGER NOT NULL
                ) WITHOUT ROWID;
                INSERT INTO rwroute_qualified_clock_gates
                SELECT c.name,c.root_bit,c.output_bit,c.route_ordinal
                  FROM rwroute_clock_candidates AS c
                 WHERE (
                       NOT EXISTS (
                         SELECT 1 FROM rwroute_clock_endpoints AS e
                          WHERE e.net_bit=c.root_bit AND e.role=0
                       )
                       OR (
                         (SELECT count(*) FROM rwroute_clock_endpoints AS e
                           WHERE e.net_bit=c.root_bit AND e.role=0) = 1
                         AND EXISTS (
                           SELECT 1
                             FROM rwroute_clock_endpoints AS e
                             JOIN rwroute_clock_candidates AS n
                               ON n.name=e.cell_id
                            WHERE e.net_bit=c.root_bit AND e.role=0
                              AND e.pin='O'
                         )
                       )
                     )
                   AND EXISTS (
                         SELECT 1 FROM rwroute_clock_endpoints AS e
                          WHERE e.net_bit=c.output_bit AND e.role=1
                       )
                   AND NOT EXISTS (
                         SELECT 1
                           FROM rwroute_clock_endpoints AS e
                          WHERE e.net_bit=c.output_bit AND e.role=1
                            AND e.pin NOT IN ('C','CLK','CLKARDCLK','CLKBWRCLK')
                            AND NOT EXISTS (
                              SELECT 1
                                FROM rwroute_clock_candidates AS n
                               WHERE n.name=e.cell_id AND e.pin='I0'
                            )
                       );
                """
            )

            # Preserve only closed candidate chains.  Each delete shrinks the
            # typed relation; Python never retains the candidate graph.
            while True:
                connection.execute(
                    """
                    DELETE FROM rwroute_qualified_clock_gates AS q
                     WHERE EXISTS (
                           SELECT 1
                             FROM rwroute_clock_endpoints AS e
                             JOIN rwroute_clock_candidates AS n
                               ON n.name=e.cell_id
                            WHERE e.net_bit=q.root_bit AND e.role=0
                              AND e.pin='O'
                              AND NOT EXISTS (
                                SELECT 1
                                  FROM rwroute_qualified_clock_gates AS keep
                                 WHERE keep.name=e.cell_id
                              )
                         )
                        OR EXISTS (
                           SELECT 1
                             FROM rwroute_clock_endpoints AS e
                             JOIN rwroute_clock_candidates AS n
                               ON n.name=e.cell_id
                            WHERE e.net_bit=q.output_bit AND e.role=1
                              AND e.pin='I0'
                              AND NOT EXISTS (
                                SELECT 1
                                  FROM rwroute_qualified_clock_gates AS keep
                                 WHERE keep.name=e.cell_id
                              )
                         )
                    """
                )
                if int(connection.execute("SELECT changes()").fetchone()[0]) == 0:
                    break
            for row in connection.execute(
                """SELECT name,route_ordinal
                     FROM rwroute_qualified_clock_gates ORDER BY name"""
            ):
                yield str(row[0]), int(row[1])
        finally:
            connection.close()

    def close(self) -> None:
        self._derived_physical_macro_contract = None
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
            immutable_sqlite_uri(path), uri=True
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


def _emit_native_xilinx_mapped_index_sql(
    index: sqlite3.Connection,
    ir_path: Path,
    contracts: Mapping[str, Mapping[str, Any]],
) -> Tuple[int, int, Counter[str], Counter[str]]:
    """Project native primitive rows into the mapped index in SQLite.

    Compact EmuIR already owns canonical cell payloads and integer endpoint
    identities.  When every cell type is already a supported Xilinx primitive,
    decoding every row into Python only to serialize it back into another
    SQLite file duplicates the largest Phase 7 mapping loop.  This projection
    preserves the v2 mapped-index rows while SQLite performs ownership,
    conflict, contiguity and coverage checks over the attached read-only
    source.
    """

    source_uri = immutable_sqlite_uri(ir_path)
    index.execute("ATTACH DATABASE ? AS source_emuir", (source_uri,))
    try:
        invalid_payload = index.execute(
            """
            SELECT c.id
              FROM source_emuir.cell_records AS c
         LEFT JOIN source_emuir.payloads AS p ON p.seq=c.parameters_seq
         LEFT JOIN source_emuir.payloads AS a ON a.seq=c.attributes_seq
              JOIN source_emuir.payloads AS k ON k.seq=c.constants_seq
             WHERE (p.value IS NOT NULL AND (
                       json_valid(p.value)=0 OR json_type(p.value)!='object'))
                OR (a.value IS NOT NULL AND (
                       json_valid(a.value)=0 OR json_type(a.value)!='object'))
                OR json_valid(k.value)=0 OR json_type(k.value)!='array'
             LIMIT 1
            """
        ).fetchone()
        if invalid_payload is not None:
            raise ValidationError(
                f"Xilinx mapped cell {invalid_payload[0]!r} has invalid payloads"
            )
        orphan_endpoint = index.execute(
            """
            SELECT e.net_seq,e.instance_seq
              FROM source_emuir.endpoint_records AS e
         LEFT JOIN source_emuir.net_records AS n ON n.seq=e.net_seq
         LEFT JOIN source_emuir.cell_records AS c ON c.seq=e.instance_seq
         LEFT JOIN source_emuir.pin_names AS p ON p.seq=e.port_seq
             WHERE n.seq IS NULL
                OR e.role NOT IN (0,1)
                OR e.bit < 0
                OR p.seq IS NULL
                OR (e.instance_seq IS NOT NULL AND c.seq IS NULL)
             LIMIT 1
            """
        ).fetchone()
        if orphan_endpoint is not None:
            raise ValidationError("compact EmuIR endpoint relation is invalid")
        invalid_constant = index.execute(
            """
            SELECT c.id
              FROM source_emuir.cell_records AS c
              JOIN source_emuir.payloads AS k ON k.seq=c.constants_seq,
                   json_each(k.value) AS item
             WHERE json_type(item.value)!='object'
                OR json_type(item.value,'$.port')!='text'
                OR json_extract(item.value,'$.port')=''
                OR json_type(item.value,'$.bit')!='integer'
                OR json_extract(item.value,'$.bit') < 0
                OR json_type(item.value,'$.value')!='text'
             LIMIT 1
            """
        ).fetchone()
        if invalid_constant is not None:
            raise ValidationError(
                f"Xilinx mapped cell {invalid_constant[0]!r} has invalid constants"
            )

        endpoint_conflict = index.execute(
            """
            SELECT c.id,p.name,e.bit
              FROM source_emuir.endpoint_records AS e
              JOIN source_emuir.cell_records AS c ON c.seq=e.instance_seq
              JOIN source_emuir.pin_names AS p ON p.seq=e.port_seq
             WHERE e.instance_seq IS NOT NULL
             GROUP BY e.instance_seq,e.port_seq,e.bit
            HAVING min(e.net_seq) != max(e.net_seq)
             LIMIT 1
            """
        ).fetchone()
        if endpoint_conflict is not None:
            raise ValidationError(
                f"Xilinx mapped cell {endpoint_conflict[0]!r}."
                f"{endpoint_conflict[1]}[{endpoint_conflict[2]}] "
                "has multiple nets"
            )
        constant_conflict = index.execute(
            """
            SELECT c.id,json_extract(item.value,'$.port'),
                   json_extract(item.value,'$.bit')
              FROM source_emuir.cell_records AS c
              JOIN source_emuir.payloads AS k ON k.seq=c.constants_seq,
                   json_each(k.value) AS item
             GROUP BY c.seq,json_extract(item.value,'$.port'),
                      json_extract(item.value,'$.bit')
            HAVING min(json_extract(item.value,'$.value')) !=
                   max(json_extract(item.value,'$.value'))
             LIMIT 1
            """
        ).fetchone()
        if constant_conflict is not None:
            raise ValidationError(
                f"Xilinx mapped cell {constant_conflict[0]!r}."
                f"{constant_conflict[1]}[{constant_conflict[2]}] "
                "has conflicting constants"
            )
        mixed_conflict = index.execute(
            """
            SELECT c.id,json_extract(item.value,'$.port'),
                   json_extract(item.value,'$.bit')
              FROM source_emuir.cell_records AS c
              JOIN source_emuir.payloads AS k ON k.seq=c.constants_seq,
                   json_each(k.value) AS item
              JOIN source_emuir.pin_names AS p
                ON p.name=json_extract(item.value,'$.port')
              JOIN source_emuir.endpoint_records AS e
                ON e.instance_seq=c.seq AND e.port_seq=p.seq
               AND e.bit=json_extract(item.value,'$.bit')
             LIMIT 1
            """
        ).fetchone()
        if mixed_conflict is not None:
            raise ValidationError(
                f"Xilinx mapped cell {mixed_conflict[0]!r}."
                f"{mixed_conflict[1]}[{mixed_conflict[2]}] "
                "has conflicting constant and net"
            )

        index.execute(
            """
            INSERT INTO cells(id,seq,type,record_json)
            SELECT c.id,c.seq,c.type,
                   '{"attributes":' || coalesce(a.value,'{}') ||
                   ',"hide_name":0,"parameters":' || coalesce(p.value,'{}') ||
                   ',"type":' || json_quote(c.type) || '}'
              FROM source_emuir.cell_records AS c
         LEFT JOIN source_emuir.payloads AS p ON p.seq=c.parameters_seq
         LEFT JOIN source_emuir.payloads AS a ON a.seq=c.attributes_seq
             ORDER BY c.seq
            """
        )
        index.execute(
            """
            WITH pin_values AS (
              SELECT e.instance_seq AS cell_seq,c.id AS cell_id,
                     e.port_seq,p.name AS port,e.bit AS port_bit,
                     CASE WHEN min(e.role) != max(e.role) THEN 2
                          ELSE 1 - min(e.role) END AS bit_direction,
                     min(e.net_seq) + 2 AS net_bit
                FROM source_emuir.endpoint_records AS e
                JOIN source_emuir.cell_records AS c ON c.seq=e.instance_seq
                JOIN source_emuir.pin_names AS p ON p.seq=e.port_seq
               WHERE e.instance_seq IS NOT NULL
               GROUP BY e.instance_seq,e.port_seq,e.bit
            ), pins AS (
              SELECT cell_seq,cell_id,port,port_bit,
                     max(port_bit) OVER (
                       PARTITION BY cell_seq,port_seq
                     ) + 1 AS port_width,
                     CASE
                       WHEN min(bit_direction) OVER (
                              PARTITION BY cell_seq,port_seq
                            ) != max(bit_direction) OVER (
                              PARTITION BY cell_seq,port_seq
                            )
                         OR max(bit_direction) OVER (
                              PARTITION BY cell_seq,port_seq
                            ) = 2
                       THEN 2 ELSE bit_direction END AS direction,
                     net_bit
                FROM pin_values
            )
            INSERT INTO cell_pins(
              cell_seq,cell_id,port,port_bit,port_width,direction,
              value_kind,net_bit,constant)
            SELECT cell_seq,cell_id,port,port_bit,port_width,direction,
                   0,net_bit,NULL
              FROM pins ORDER BY cell_seq,port,port_bit
            """
        )
        index.executescript(
            """
            CREATE TEMP TABLE native_constant_ports(
              cell_seq INTEGER NOT NULL,port TEXT NOT NULL,
              PRIMARY KEY(cell_seq,port)
            ) WITHOUT ROWID;
            INSERT INTO native_constant_ports
            SELECT DISTINCT c.seq,json_extract(item.value,'$.port')
              FROM source_emuir.cell_records AS c
              JOIN source_emuir.payloads AS k ON k.seq=c.constants_seq,
                   json_each(k.value) AS item;
            """
        )
        index.execute(
            """
            WITH constant_values AS (
              SELECT c.seq AS cell_seq,c.id AS cell_id,
                     json_extract(item.value,'$.port') AS port,
                     json_extract(item.value,'$.bit') AS port_bit,
                     min(json_extract(item.value,'$.value')) AS constant
                FROM source_emuir.cell_records AS c
                JOIN source_emuir.payloads AS k ON k.seq=c.constants_seq,
                     json_each(k.value) AS item
               GROUP BY c.seq,port,port_bit
            ), pins AS (
              SELECT cell_seq,cell_id,port,port_bit,
                     max(port_bit) OVER (
                       PARTITION BY cell_seq,port
                     ) + 1 AS port_width,constant
                FROM constant_values
            )
            INSERT INTO cell_pins(
              cell_seq,cell_id,port,port_bit,port_width,direction,
              value_kind,net_bit,constant)
            SELECT cell_seq,cell_id,port,port_bit,port_width,0,
                   1,NULL,constant
              FROM pins ORDER BY cell_seq,port,port_bit
            """
        )
        index.execute(
            """
            UPDATE cell_pins AS pin
               SET port_width=(
                     SELECT max(peer.port_bit) + 1 FROM cell_pins AS peer
                      WHERE peer.cell_seq=pin.cell_seq AND peer.port=pin.port
                   ),
                   direction=(
                     SELECT CASE WHEN min(peer.direction) != max(peer.direction)
                                      OR max(peer.direction) = 2
                                 THEN 2 ELSE min(peer.direction) END
                       FROM cell_pins AS peer
                      WHERE peer.cell_seq=pin.cell_seq AND peer.port=pin.port
                   )
             WHERE (pin.cell_seq,pin.port) IN (
                     SELECT cell_seq,port FROM native_constant_ports
                   )
            """
        )
        noncontiguous = index.execute(
            """
            SELECT cell_id,port
              FROM cell_pins
             GROUP BY cell_seq,cell_id,port
            HAVING min(port_bit) != 0 OR count(*) != max(port_bit) + 1
             LIMIT 1
            """
        ).fetchone()
        if noncontiguous is not None:
            raise ValidationError(
                f"Xilinx mapped cell {noncontiguous[0]!r}."
                f"{noncontiguous[1]} is not contiguous"
            )
        index.execute(
            """
            INSERT INTO netnames(id,seq,record_json)
            SELECT id,seq,'{"attributes":{},"bits":[' || (seq + 2) ||
                          '],"hide_name":0}'
              FROM source_emuir.net_records ORDER BY seq
            """
        )
        cell_count = int(index.execute(
            "SELECT count(*) FROM cells"
        ).fetchone()[0])
        net_count = int(index.execute(
            "SELECT count(*) FROM netnames"
        ).fetchone()[0])
        source_cell_count = int(index.execute(
            "SELECT count(*) FROM source_emuir.cell_records"
        ).fetchone()[0])
        source_net_count = int(index.execute(
            "SELECT count(*) FROM source_emuir.net_records"
        ).fetchone()[0])
        if cell_count != source_cell_count or net_count != source_net_count:
            raise ValidationError("Xilinx mapped index coverage is incomplete")

        inventory = Counter(
            {
                str(row[0]): int(row[1])
                for row in index.execute(
                    "SELECT type,count(*) FROM cells GROUP BY type ORDER BY type"
                )
            }
        )
        unknown = sorted(set(inventory) - set(contracts))
        if unknown:
            raise ValidationError(
                f"unsupported Xilinx primitive {unknown[0]!r}"
            )
        hard_blocks = Counter(
            {
                cell_type: count
                for cell_type, count in inventory.items()
                if contracts[cell_type]["hard_block"]
            }
        )
        index.execute("DROP TABLE native_constant_ports")
        index.commit()
        return cell_count, net_count, inventory, hard_blocks
    except BaseException:
        index.rollback()
        raise
    finally:
        try:
            index.execute("DETACH DATABASE source_emuir")
        except sqlite3.Error:
            pass


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

    source_bindings = validate_store_destination(
        index_path, (bind_store_source(ir_path, "emuir-compact-v2"),)
    )
    if output_path is not None:
        validate_store_destination(output_path, source_bindings)
        if output_path.resolve() == index_path.resolve():
            raise ValidationError("mapped JSON and index outputs alias")
    source: Optional[sqlite3.Connection] = open_emuir_sqlite_read_only(ir_path)
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
        # SQL-materialized stores can use one-based or sparse primary keys.
        # Fresh port identities must be beyond the maximum REAL net ordinal,
        # rather than beyond its row count.
        max_net_seq = source.execute("SELECT max(seq) FROM net_records").fetchone()[0]
        next_dead_input_bit = (int(max_net_seq) + 3) if max_net_seq is not None else 2
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
        index = sqlite3.connect(index_temp, uri=True)
        index.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            PRAGMA cache_size=-131072;
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

        native_sql_projection = (
            output_temp is None
            and not any(
                row[0] not in native_xilinx_cells
                for row in source.execute(
                    "SELECT DISTINCT type FROM cell_records"
                )
            )
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

        if native_sql_projection:
            # The set projection attaches the same immutable source to the
            # destination connection.  Release the metadata reader first so
            # the process does not retain two independent SQLite page caches.
            source.close()
            source = None
            (
                cell_count,
                emitted_net_count,
                inventory,
                hard_blocks,
            ) = _emit_native_xilinx_mapped_index_sql(
                index, ir_path, contracts
            )
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
        # No consumer reads this unpublished database during construction.
        # Build auxiliary pin orders in bulk rather than updating a random
        # net-order B-tree for every pin on the shared physical-flow filesystem.
        # Primary/unique keys and pin checks remain enforced while inserting.
        index.executescript("""
            CREATE INDEX cell_pins_by_seq
              ON cell_pins(cell_seq,port,port_bit);
            CREATE INDEX cell_pins_by_net
              ON cell_pins(net_bit,direction,cell_id,port,port_bit)
              WHERE value_kind=0;
        """)
        index.commit()
        index.close()
        index = None
        if output_temp is not None:
            with output_temp.open("rb") as stream:
                os.fsync(stream.fileno())
        require_store_sources_unchanged(source_bindings)
        if output_temp is not None and output_path is not None:
            os.replace(output_temp, output_path)
        publish_sqlite_store(
            index_temp, index_path, "xilinx-mapped-index-v2", sources=source_bindings
        )

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
            "index_builder": (
                "sqlite-native-primitive-v1"
                if native_sql_projection
                else "python-lowering-v2"
            ),
            "audit": audit,
        }
    finally:
        if source is not None:
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
