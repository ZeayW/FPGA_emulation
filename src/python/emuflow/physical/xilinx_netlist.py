"""Compact EmuIR to the canonical indexed Xilinx mapped netlist."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterator, Mapping as MappingABC
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri
from emuflow.ir.emuir_compact import is_compact_emuir
from emuflow.ir.emuir_storage import _RowsForOwner
from emuflow.common.errors import ValidationError
from emuflow.storage.store_publication import StoreSource
from emuflow.common.native_tools import native_file_generation


XILINX_MAPPED_NETLIST_REPORT_SCHEMA = (
    "emuflow.xilinx-mapped-netlist-report/v1"
)
XILINX_MAPPED_INDEX_SCHEMA_V1 = "emuflow.xilinx-mapped-index/v1"
XILINX_MAPPED_INDEX_SCHEMA = "emuflow.xilinx-mapped-index/v2"
_XILINX_MAPPED_INDEX_SCHEMAS = {
    XILINX_MAPPED_INDEX_SCHEMA_V1, XILINX_MAPPED_INDEX_SCHEMA,
}


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


def emit_xilinx_mapped_index(ir_path: Path, index_path: Path, report_path: Path | None = None) -> Mapping[str, Any]:
    """Produce the canonical mapped physical owner in the shared native core."""
    if not is_compact_emuir(ir_path):
        raise ValidationError("Xilinx mapped-index production input must be compact EmuIR")
    from emuflow.physical.mapped_index_native import emit_native_mapped_index
    return emit_native_mapped_index(ir_path, index_path, report_path)
