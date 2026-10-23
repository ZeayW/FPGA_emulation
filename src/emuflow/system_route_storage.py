"""Typed, sealed storage for the Phase 4 system-route contract.

The public ``routes.json`` path is a small relocatable selector.  Demand,
route-tree and timing-path records live in one content-addressed SQLite
sidecar.  Production consumers use :func:`read_system_routes` and therefore
do not parse or retain a graph-sized JSON document.  ``read_json`` remains the
explicit legacy materializer.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .io import file_sha256, write_json
from .routing import SYSTEM_ROUTES_SCHEMA


SYSTEM_ROUTE_STORAGE_SCHEMA = "emuflow.system-route-storage/v1"
SYSTEM_ROUTE_APPLICATION_ID = 0x454D5552  # ASCII "EMUR"
SYSTEM_ROUTE_USER_VERSION = 3
_SUPPORTED_SYSTEM_ROUTE_USER_VERSIONS = {1, 2, SYSTEM_ROUTE_USER_VERSION}
SYSTEM_ROUTE_STORE_PREFIX = "routes."
SYSTEM_ROUTE_STORE_SUFFIX = ".sqlite"


_SELECTOR_SCHEMA_PATTERN = re.compile(
    rb'"schema"\s*:\s*"emuflow\.system-route-storage/v1"'
)


_DDL = """
CREATE TABLE metadata(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE demands(
  seq INTEGER PRIMARY KEY,
  demand_id TEXT NOT NULL UNIQUE,
  route_seq INTEGER NOT NULL UNIQUE
);
CREATE TABLE routes(
  seq INTEGER PRIMARY KEY,
  demand_id TEXT NOT NULL UNIQUE,
  net_id TEXT NOT NULL UNIQUE,
  source_fpga TEXT NOT NULL,
  width_bits INTEGER NOT NULL,
  transport_round INTEGER,
  cut_class TEXT,
  dependency_level INTEGER,
  combinational_dependency_depth INTEGER,
  max_latency_cycles INTEGER,
  predicted_max_delay_ns REAL,
  CHECK(length(demand_id)>0),
  CHECK(length(net_id)>0),
  CHECK(length(source_fpga)>0),
  CHECK(typeof(width_bits)='integer' AND width_bits>0),
  CHECK(transport_round IS NULL OR (
    typeof(transport_round)='integer' AND transport_round>=0
  )),
  CHECK(cut_class IS NULL OR cut_class IN (
    'combinational','register_input','register_output'
  )),
  CHECK((dependency_level IS NULL AND combinational_dependency_depth IS NULL)
    OR (typeof(dependency_level)='integer' AND dependency_level>=0
      AND typeof(combinational_dependency_depth)='integer'
      AND combinational_dependency_depth>=0)),
  CHECK(max_latency_cycles IS NULL OR (
    typeof(max_latency_cycles)='integer' AND max_latency_cycles>=0
  )),
  CHECK(predicted_max_delay_ns IS NULL OR (
    typeof(predicted_max_delay_ns) IN ('integer','real')
    AND predicted_max_delay_ns>=0
  ))
);
CREATE TABLE route_sinks(
  route_seq INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  sink_fpga TEXT NOT NULL,
  PRIMARY KEY(route_seq,seq),
  UNIQUE(route_seq,sink_fpga)
) WITHOUT ROWID;
CREATE TABLE route_edges(
  route_seq INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  link_id TEXT NOT NULL,
  source_fpga TEXT NOT NULL,
  sink_fpga TEXT NOT NULL,
  PRIMARY KEY(route_seq,seq),
  UNIQUE(route_seq,link_id,source_fpga,sink_fpga)
) WITHOUT ROWID;
CREATE TABLE route_predecessors(
  route_seq INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  predecessor_net TEXT NOT NULL,
  PRIMARY KEY(route_seq,seq),
  UNIQUE(route_seq,predecessor_net)
) WITHOUT ROWID;
CREATE TABLE timing_paths(
  seq INTEGER PRIMARY KEY,
  path_id TEXT NOT NULL UNIQUE,
  record_json TEXT NOT NULL,
  CHECK(json_valid(record_json))
);
CREATE TABLE route_master_selections(
  seq INTEGER PRIMARY KEY,
  demand_id TEXT NOT NULL UNIQUE,
  generator TEXT NOT NULL
);
CREATE TABLE refinement_batch_paths(
  seq INTEGER PRIMARY KEY,
  batch_index INTEGER NOT NULL,
  position INTEGER NOT NULL,
  path_index INTEGER NOT NULL UNIQUE,
  UNIQUE(batch_index,position)
);
CREATE INDEX routes_net_order ON routes(net_id,seq);
CREATE INDEX routes_demand_order ON routes(demand_id,seq);
CREATE INDEX route_edges_arc ON route_edges(link_id,source_fpga,sink_fpga);
CREATE INDEX route_predecessors_net ON route_predecessors(predecessor_net);
"""


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )


def _pretty_sha256(value: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(indent=2, sort_keys=True)
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode("utf-8"))
    digest.update(b"\n")
    return digest.hexdigest()


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _content_addressed_store_name(store_sha256: str) -> str:
    return (
        f"{SYSTEM_ROUTE_STORE_PREFIX}{store_sha256}"
        f"{SYSTEM_ROUTE_STORE_SUFFIX}"
    )


def _looks_like_route_selector(path: Path) -> bool:
    """Detect our bounded selector without materializing a legacy route JSON."""

    overlap = b""
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            window = overlap + chunk
            if _SELECTOR_SCHEMA_PATTERN.search(window):
                return True
            overlap = window[-256:]
    return False


def _previous_route_artifact(path: Path) -> Path | None:
    """Validate and resolve a previously published typed sidecar."""

    if not path.is_file() or not _looks_like_route_selector(path):
        return None
    with path.open("r", encoding="utf-8") as stream:
        try:
            previous = json.load(stream)
        except json.JSONDecodeError as error:
            raise ValidationError("previous system-route selector is corrupt") from error
    if not isinstance(previous, dict):
        raise ValidationError("previous system-route selector is malformed")
    if previous.get("schema") != SYSTEM_ROUTE_STORAGE_SCHEMA:
        return None
    artifact = previous.get("artifact")
    store_sha256 = previous.get("store_sha256")
    if (
        not isinstance(artifact, str)
        or Path(artifact).name != artifact
        or not _valid_digest(store_sha256)
        or artifact != _content_addressed_store_name(store_sha256)
    ):
        raise ValidationError("previous system-route artifact path is invalid")
    # Full validation occurs before creating the next generation.  In
    # particular, a forged selector can never make cleanup escape this
    # directory or remove an unvalidated file.
    return SystemRoutesView(path).store_path


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


def _decode_json(payload: str, context: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValidationError(f"{context} payload is corrupt") from error
    if not isinstance(value, dict):
        raise ValidationError(f"{context} payload is not an object")
    return value


class _JsonRecordSequence(list):
    """List-compatible lazy view over one ordered JSON-record relation."""

    def __init__(
        self,
        store_path: Path,
        table: str,
        count: int,
        order: str = "seq",
    ):
        super().__init__()
        self.store_path = store_path
        self.table = table
        self._count = count
        if order not in {"seq", "path"} or (
            order == "path" and table != "timing_paths"
        ):
            raise ValueError(f"unsupported {table} record order {order!r}")
        self.order = order

    def ordered(self, order: str) -> "_JsonRecordSequence":
        """Return another lazy view with a store-owned stable order."""

        return _JsonRecordSequence(
            self.store_path, self.table, self._count, order
        )

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[dict[str, Any]]:
        connection = _connect(self.store_path)
        try:
            observed = 0
            order_sql = "path_id,seq" if self.order == "path" else "seq"
            for row in connection.execute(
                f"SELECT record_json FROM {self.table} ORDER BY {order_sql}"
            ):
                observed += 1
                yield _decode_json(row[0], self.table)
            if observed != self._count:
                raise ValidationError(f"{self.table} record count disagrees")
        finally:
            connection.close()

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self)[index]
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        connection = _connect(self.store_path)
        try:
            if self.order == "seq":
                row = connection.execute(
                    f"SELECT record_json FROM {self.table} WHERE seq=?",
                    (index,),
                ).fetchone()
            else:
                row = connection.execute(
                    f"SELECT record_json FROM {self.table} "
                    "ORDER BY path_id,seq LIMIT 1 OFFSET ?",
                    (index,),
                ).fetchone()
            if row is None:
                raise ValidationError(f"{self.table} sequence is not contiguous")
            return _decode_json(row[0], self.table)
        finally:
            connection.close()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Sequence) and not isinstance(other, (str, bytes)):
            return len(self) == len(other) and all(
                left == right for left, right in zip(self, other)
            )
        return False

    def __ne__(self, other: object) -> bool:
        return not self == other


class _MasterSelectionSequence(list):
    """Lazy master-candidate selections stored outside the route header."""

    def __init__(self, store_path: Path, count: int):
        super().__init__()
        self.store_path = store_path
        self._count = count

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[dict[str, str]]:
        connection = _connect(self.store_path)
        try:
            observed = 0
            for row in connection.execute(
                "SELECT demand_id,generator FROM route_master_selections "
                "ORDER BY seq"
            ):
                observed += 1
                yield {"demand": str(row[0]), "generator": str(row[1])}
            if observed != self._count:
                raise ValidationError(
                    "route master-selection count disagrees"
                )
        finally:
            connection.close()

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self)[index]
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        connection = _connect(self.store_path)
        try:
            row = connection.execute(
                "SELECT demand_id,generator FROM route_master_selections "
                "WHERE seq=?",
                (index,),
            ).fetchone()
            if row is None:
                raise ValidationError(
                    "route master-selection sequence is not contiguous"
                )
            return {"demand": str(row[0]), "generator": str(row[1])}
        finally:
            connection.close()

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Sequence)
            and not isinstance(other, (str, bytes))
            and len(self) == len(other)
            and all(left == right for left, right in zip(self, other))
        )

    def __ne__(self, other: object) -> bool:
        return not self == other


class _RefinementBatchSequence(list):
    """Lazy nested path-index batches stored as one ordered relation."""

    def __init__(self, store_path: Path, batch_count: int, record_count: int):
        super().__init__()
        self.store_path = store_path
        self._count = batch_count
        self._record_count = record_count

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[list[int]]:
        connection = _connect(self.store_path)
        try:
            cursor = iter(
                connection.execute(
                    "SELECT batch_index,position,path_index "
                    "FROM refinement_batch_paths "
                    "ORDER BY batch_index,position"
                )
            )
            row = next(cursor, None)
            observed = 0
            for batch_index in range(self._count):
                batch: list[int] = []
                position = 0
                while row is not None and int(row[0]) == batch_index:
                    if int(row[1]) != position:
                        raise ValidationError(
                            "route refinement batch position is not contiguous"
                        )
                    batch.append(int(row[2]))
                    position += 1
                    observed += 1
                    row = next(cursor, None)
                yield batch
            if row is not None or observed != self._record_count:
                raise ValidationError(
                    "route refinement-batch coverage disagrees"
                )
        finally:
            connection.close()

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self)[index]
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        connection = _connect(self.store_path)
        try:
            rows = connection.execute(
                "SELECT position,path_index FROM refinement_batch_paths "
                "WHERE batch_index=? ORDER BY position",
                (index,),
            )
            batch = []
            for position, path_index in rows:
                if int(position) != len(batch):
                    raise ValidationError(
                        "route refinement batch position is not contiguous"
                    )
                batch.append(int(path_index))
            return batch
        finally:
            connection.close()

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Sequence)
            and not isinstance(other, (str, bytes))
            and len(self) == len(other)
            and all(left == right for left, right in zip(self, other))
        )

    def __ne__(self, other: object) -> bool:
        return not self == other


_LEGACY_ROUTE_COLUMNS = (
    "seq",
    "demand_id",
    "net_id",
    "source_fpga",
    "dependency_level",
    "combinational_dependency_depth",
    "record_json",
)
_TYPED_ROUTE_COLUMNS = (
    "seq",
    "demand_id",
    "net_id",
    "source_fpga",
    "width_bits",
    "transport_round",
    "cut_class",
    "dependency_level",
    "combinational_dependency_depth",
    "max_latency_cycles",
    "predicted_max_delay_ns",
)
_ROUTE_STRUCTURAL_FIELDS = {
    "id",
    "net",
    "source",
    "sinks",
    "tree_edges",
    "predecessor_cut_nets",
    "dependency_level",
    "combinational_dependency_depth",
}
_ROUTE_SCALAR_FIELDS = {
    "width_bits",
    "transport_round",
    "cut_class",
    "max_latency_cycles",
    "predicted_max_delay_ns",
}


def _nonnegative_integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{context} must be a non-negative integer")
    return value


def _typed_route_scalar_values(route: Mapping[str, Any]) -> tuple[Any, ...]:
    unexpected = set(route) - _ROUTE_STRUCTURAL_FIELDS - _ROUTE_SCALAR_FIELDS
    if unexpected:
        raise ValidationError(
            f"system route has unsupported scalar fields: {sorted(unexpected)}"
        )
    width_bits = _nonnegative_integer(
        route.get("width_bits"), "system route width_bits"
    )
    if width_bits == 0:
        raise ValidationError("system route width_bits must be positive")
    transport_round = route.get("transport_round")
    if "transport_round" in route:
        transport_round = _nonnegative_integer(
            transport_round, "system route transport_round"
        )
    cut_class = route.get("cut_class")
    if cut_class is not None and cut_class not in {
        "combinational",
        "register_input",
        "register_output",
    }:
        raise ValidationError("system route cut_class is invalid")
    has_level = "dependency_level" in route
    has_depth = "combinational_dependency_depth" in route
    if has_level != has_depth:
        raise ValidationError("system route dependency metadata is incomplete")
    dependency_level = route.get("dependency_level")
    dependency_depth = route.get("combinational_dependency_depth")
    if has_level:
        dependency_level = _nonnegative_integer(
            dependency_level, "system route dependency_level"
        )
        dependency_depth = _nonnegative_integer(
            dependency_depth, "system route combinational_dependency_depth"
        )
    predecessors = route.get("predecessor_cut_nets", [])
    if not has_level and predecessors:
        raise ValidationError("non-exact route has predecessor records")
    max_latency_cycles = route.get("max_latency_cycles")
    if "max_latency_cycles" in route:
        max_latency_cycles = _nonnegative_integer(
            max_latency_cycles, "system route max_latency_cycles"
        )
    predicted_delay = route.get("predicted_max_delay_ns")
    if "predicted_max_delay_ns" in route:
        if (
            isinstance(predicted_delay, bool)
            or not isinstance(predicted_delay, float)
            or not math.isfinite(predicted_delay)
            or predicted_delay < 0.0
        ):
            raise ValidationError(
                "system route predicted_max_delay_ns must be a finite "
                "non-negative float"
            )
    return (
        width_bits,
        transport_round,
        cut_class,
        dependency_level,
        dependency_depth,
        max_latency_cycles,
        predicted_delay,
    )


def _route_scalars_from_row(
    row: sqlite3.Row, user_version: int
) -> dict[str, Any]:
    if user_version < 3:
        route = _decode_json(row["record_json"], "system route")
        if any(key in route for key in _ROUTE_STRUCTURAL_FIELDS):
            raise ValidationError("system route scalar payload is malformed")
        return route
    route = {"width_bits": int(row["width_bits"])}
    if row["transport_round"] is not None:
        route["transport_round"] = int(row["transport_round"])
    if row["cut_class"] is not None:
        route["cut_class"] = str(row["cut_class"])
    if row["max_latency_cycles"] is not None:
        route["max_latency_cycles"] = int(row["max_latency_cycles"])
    if row["predicted_max_delay_ns"] is not None:
        route["predicted_max_delay_ns"] = float(row["predicted_max_delay_ns"])
    return route


def _route_from_row(
    connection: sqlite3.Connection, row: sqlite3.Row, user_version: int
) -> dict[str, Any]:
    route = _route_scalars_from_row(row, user_version)
    route_seq = int(row["seq"])
    route.update(
        {
            "id": str(row["demand_id"]),
            "net": str(row["net_id"]),
            "source": str(row["source_fpga"]),
        }
    )
    route["sinks"] = [
        str(item[0])
        for item in connection.execute(
            "SELECT sink_fpga FROM route_sinks WHERE route_seq=? ORDER BY seq",
            (route_seq,),
        )
    ]
    route["tree_edges"] = [
        {"link": str(item[0]), "from": str(item[1]), "to": str(item[2])}
        for item in connection.execute(
            "SELECT link_id,source_fpga,sink_fpga FROM route_edges "
            "WHERE route_seq=? ORDER BY seq",
            (route_seq,),
        )
    ]
    predecessors = [
        str(item[0])
        for item in connection.execute(
            "SELECT predecessor_net FROM route_predecessors "
            "WHERE route_seq=? ORDER BY seq",
            (route_seq,),
        )
    ]
    if row["dependency_level"] is not None:
        route["predecessor_cut_nets"] = predecessors
        route["dependency_level"] = int(row["dependency_level"])
        route["combinational_dependency_depth"] = int(
            row["combinational_dependency_depth"]
        )
    elif predecessors:
        raise ValidationError("non-exact route has predecessor records")
    return route


_DEMAND_FIELDS = ("id", "net", "source", "sinks", "width_bits")
_OPTIONAL_DEMAND_FIELDS = (
    "transport_round",
    "cut_class",
    "dependency_level",
    "combinational_dependency_depth",
    "predecessor_cut_nets",
)


def _demand_from_route(route: Mapping[str, Any]) -> dict[str, Any]:
    demand = {field: route[field] for field in _DEMAND_FIELDS}
    for field in _OPTIONAL_DEMAND_FIELDS:
        if field in route:
            value = route[field]
            demand[field] = list(value) if field in {"sinks", "predecessor_cut_nets"} else value
    demand["sinks"] = list(route["sinks"])
    return demand


class SystemDemandSequence(list):
    """Logical demands projected from their authoritative routed records."""

    def __init__(self, store_path: Path, count: int, user_version: int):
        super().__init__()
        self.store_path = store_path
        self._count = count
        self._user_version = user_version

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[dict[str, Any]]:
        for route in SystemRouteSequence(
            self.store_path, self._count, self._user_version
        ):
            yield _demand_from_route(route)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self)[index]
        return _demand_from_route(
            SystemRouteSequence(
                self.store_path, self._count, self._user_version
            )[index]
        )

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Sequence) and not isinstance(other, (str, bytes)):
            return len(self) == len(other) and all(
                left == right for left, right in zip(self, other)
            )
        return False

    def __ne__(self, other: object) -> bool:
        return not self == other


class SystemRouteNetIndex:
    """Read individual normalized routes by net without owning a graph map."""

    def __init__(self, store_path: Path, user_version: int):
        self.store_path = store_path
        self._user_version = user_version
        self._connection = _connect(store_path)

    def __enter__(self) -> "SystemRouteNetIndex":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        connection = self._connection
        if connection is not None:
            connection.close()
            self._connection = None

    def get(
        self, net: str, default: Any = None
    ) -> dict[str, Any] | Any:
        connection = self._connection
        if connection is None:
            raise RuntimeError("system-route net index is closed")
        row = connection.execute(
            "SELECT * FROM routes WHERE net_id=?", (net,)
        ).fetchone()
        if row is None:
            return default
        return _route_from_row(connection, row, self._user_version)


class SystemRouteSequence(list):
    """List-compatible route-tree cursor with stable SQL ordering."""

    def __init__(
        self,
        store_path: Path,
        count: int,
        user_version: int,
        order: str = "seq",
    ):
        super().__init__()
        self.store_path = store_path
        self._count = count
        self._user_version = user_version
        if order not in {"seq", "net", "demand"}:
            raise ValueError(f"unsupported route order {order!r}")
        self.order = order

    def __len__(self) -> int:
        return self._count

    def ordered(self, order: str) -> "SystemRouteSequence":
        return SystemRouteSequence(
            self.store_path, self._count, self._user_version, order
        )

    def open_net_index(self) -> "SystemRouteNetIndex":
        """Open a bounded-lifetime net lookup over the normalized store.

        The returned view owns one read-only SQLite connection.  It fetches
        only the requested route and its child relations, so consumers that
        walk timing paths do not need an R-sized ``net -> route`` dictionary.
        """

        return SystemRouteNetIndex(self.store_path, self._user_version)

    def validate_indexed_exact_metadata(self) -> None:
        """Validate indexed-exact scalar shape without decoding route trees."""

        connection = _connect(self.store_path)
        try:
            cut_class_sql = (
                "cut_class"
                if self._user_version >= 3
                else "json_extract(record_json,'$.cut_class')"
            )
            malformed = connection.execute(
                "SELECT seq FROM routes WHERE length(demand_id)=0 "
                "OR length(net_id)=0 OR length(source_fpga)=0 "
                "OR dependency_level IS NULL OR dependency_level<0 "
                "OR combinational_dependency_depth IS NULL "
                "OR combinational_dependency_depth<0 "
                f"OR {cut_class_sql} IS NULL "
                f"OR {cut_class_sql} NOT IN "
                "('combinational','register_input','register_output') LIMIT 1"
            ).fetchone()
            unknown = connection.execute(
                "SELECT p.predecessor_net FROM route_predecessors p "
                "LEFT JOIN routes r ON r.net_id=p.predecessor_net "
                "WHERE r.seq IS NULL LIMIT 1"
            ).fetchone()
            if malformed is not None or unknown is not None:
                raise ValidationError(
                    "indexed exact route dependency metadata is invalid"
                )
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                "indexed exact route metadata is unreadable"
            ) from error
        finally:
            connection.close()

    def iter_arc_counts(self) -> Iterator[tuple[str, str, str, int]]:
        """Yield distinct directed route arcs and their demand counts."""

        connection = _connect(self.store_path)
        try:
            for row in connection.execute(
                "SELECT link_id,source_fpga,sink_fpga,count(*) "
                "FROM route_edges GROUP BY link_id,source_fpga,sink_fpga "
                "ORDER BY link_id,source_fpga,sink_fpga"
            ):
                yield str(row[0]), str(row[1]), str(row[2]), int(row[3])
        finally:
            connection.close()

    def __iter__(self) -> Iterator[dict[str, Any]]:
        order_sql = {
            "seq": "r.seq",
            "net": "r.net_id,r.seq",
            "demand": "r.demand_id,r.seq",
        }[self.order]
        connection = _connect(self.store_path)
        try:
            route_cursor = connection.execute(
                f"SELECT r.* FROM routes r ORDER BY {order_sql}"
            )
            sink_cursor = iter(connection.execute(
                "SELECT r.seq,s.seq,s.sink_fpga FROM routes r "
                "JOIN route_sinks s ON s.route_seq=r.seq "
                f"ORDER BY {order_sql},s.seq"
            ))
            edge_cursor = iter(connection.execute(
                "SELECT r.seq,e.seq,e.link_id,e.source_fpga,e.sink_fpga "
                "FROM routes r JOIN route_edges e ON e.route_seq=r.seq "
                f"ORDER BY {order_sql},e.seq"
            ))
            predecessor_cursor = iter(connection.execute(
                "SELECT r.seq,p.seq,p.predecessor_net FROM routes r "
                "JOIN route_predecessors p ON p.route_seq=r.seq "
                f"ORDER BY {order_sql},p.seq"
            ))
            sink_row = next(sink_cursor, None)
            edge_row = next(edge_cursor, None)
            predecessor_row = next(predecessor_cursor, None)
            observed = 0
            for row in route_cursor:
                route_seq = int(row["seq"])
                route = _route_scalars_from_row(row, self._user_version)
                route.update(
                    {
                        "id": str(row["demand_id"]),
                        "net": str(row["net_id"]),
                        "source": str(row["source_fpga"]),
                    }
                )
                sinks: list[str] = []
                while sink_row is not None and int(sink_row[0]) == route_seq:
                    sinks.append(str(sink_row[2]))
                    sink_row = next(sink_cursor, None)
                edges: list[dict[str, str]] = []
                while edge_row is not None and int(edge_row[0]) == route_seq:
                    edges.append(
                        {
                            "link": str(edge_row[2]),
                            "from": str(edge_row[3]),
                            "to": str(edge_row[4]),
                        }
                    )
                    edge_row = next(edge_cursor, None)
                predecessors: list[str] = []
                while (
                    predecessor_row is not None
                    and int(predecessor_row[0]) == route_seq
                ):
                    predecessors.append(str(predecessor_row[2]))
                    predecessor_row = next(predecessor_cursor, None)
                route["sinks"] = sinks
                route["tree_edges"] = edges
                if row["dependency_level"] is not None:
                    route["predecessor_cut_nets"] = predecessors
                    route["dependency_level"] = int(row["dependency_level"])
                    route["combinational_dependency_depth"] = int(
                        row["combinational_dependency_depth"]
                    )
                elif predecessors:
                    raise ValidationError("non-exact route has predecessors")
                observed += 1
                yield route
            if (
                observed != self._count
                or sink_row is not None
                or edge_row is not None
                or predecessor_row is not None
            ):
                raise ValidationError("system route count disagrees")
        finally:
            connection.close()

    def iter_native_exact_records(
        self,
    ) -> Iterator[tuple[dict[str, Any], int, list[int]]]:
        """Yield net-ordered routes with SQL-derived compact integer ranks."""

        connection = _connect(self.store_path)
        try:
            route_cursor = connection.execute(
                "WITH ranked AS ("
                " SELECT *,row_number() OVER (ORDER BY net_id,seq)-1 AS net_rank,"
                " row_number() OVER (ORDER BY demand_id,seq)-1 AS demand_rank"
                " FROM routes) SELECT * FROM ranked ORDER BY net_rank"
            )
            predecessor_cursor = iter(connection.execute(
                "WITH ranked AS ("
                " SELECT seq,net_id,row_number() OVER (ORDER BY net_id,seq)-1 "
                " AS net_rank FROM routes) "
                "SELECT child.net_rank,rp.seq,parent.net_rank "
                "FROM route_predecessors rp "
                "JOIN ranked child ON child.seq=rp.route_seq "
                "LEFT JOIN ranked parent ON parent.net_id=rp.predecessor_net "
                "ORDER BY child.net_rank,rp.seq"
            ))
            predecessor_row = next(predecessor_cursor, None)
            ordered = self.ordered("net")
            observed = 0
            for net_rank, (row, route) in enumerate(zip(route_cursor, ordered)):
                observed += 1
                if int(row["net_rank"]) != net_rank:
                    raise ValidationError("system-route net rank is not contiguous")
                predecessor_ranks: list[int] = []
                while (
                    predecessor_row is not None
                    and int(predecessor_row[0]) == net_rank
                ):
                    if predecessor_row[2] is None:
                        raise ValidationError(
                            "system route has an unknown predecessor net"
                        )
                    predecessor_ranks.append(int(predecessor_row[2]))
                    predecessor_row = next(predecessor_cursor, None)
                yield route, int(row["demand_rank"]), predecessor_ranks
            if observed != self._count or predecessor_row is not None:
                raise ValidationError("system-route predecessor coverage disagrees")
        finally:
            connection.close()

    def iter_exact_dependency_ranks(self) -> Iterator[int]:
        """Yield net ranks in the reverse level order used by Phase 5.

        The cursor keeps the graph ordering in SQLite.  Consumers can update
        compact packed arrays without sorting or retaining one Python route
        object per cut.
        """

        connection = _connect(self.store_path)
        try:
            observed = 0
            for row in connection.execute(
                "WITH ranked AS ("
                " SELECT seq,net_id,dependency_level,"
                " row_number() OVER (ORDER BY net_id,seq)-1 AS net_rank"
                " FROM routes) SELECT net_rank FROM ranked "
                "ORDER BY dependency_level DESC,net_id DESC"
            ):
                observed += 1
                yield int(row[0])
            if observed != self._count:
                raise ValidationError(
                    "system-route dependency-rank coverage disagrees"
                )
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                "system-route dependency ranks are unreadable"
            ) from error
        finally:
            connection.close()

    def iter_demands_by_net_rank_order(
        self, net_ranks: Iterator[int]
    ) -> Iterator[str]:
        """Yield demand identities for an externally computed net-rank order."""

        connection = _connect(self.store_path)
        try:
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute(
                "CREATE TEMP TABLE validation_route_order("
                "position INTEGER PRIMARY KEY,net_rank INTEGER NOT NULL UNIQUE)"
            )
            connection.executemany(
                "INSERT INTO validation_route_order(position,net_rank) "
                "VALUES(?,?)",
                enumerate(net_ranks),
            )
            rows = connection.execute(
                "WITH ranked AS ("
                " SELECT demand_id,row_number() OVER "
                " (ORDER BY net_id,seq)-1 AS net_rank FROM routes) "
                "SELECT r.demand_id FROM validation_route_order o "
                "JOIN ranked r USING(net_rank) ORDER BY o.position"
            )
            observed = 0
            for row in rows:
                observed += 1
                yield str(row[0])
            if observed != self._count:
                raise ValidationError(
                    "system-route validation order coverage disagrees"
                )
        except (sqlite3.DatabaseError, sqlite3.IntegrityError) as error:
            raise ValidationError(
                "system-route validation order is malformed"
            ) from error
        finally:
            connection.close()

    def iter_routes_by_net_rank_order(
        self, net_ranks: Iterator[int]
    ) -> Iterator[tuple[int, dict[str, Any], tuple[int, ...]]]:
        """Yield exact routes and predecessor ranks in a packed order.

        ``net_ranks`` is normally the compact topological order calculated by
        the independent Phase 5 validator.  The temporary order relation lets
        SQLite merge all route children in one pass, avoiding random OFFSET
        reads and an R-sized Python route dictionary/list.
        """

        connection = _connect(self.store_path)
        try:
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute(
                "CREATE TEMP TABLE validation_route_ranks("
                "route_seq INTEGER PRIMARY KEY,net_rank INTEGER NOT NULL UNIQUE)"
            )
            connection.execute(
                "INSERT INTO validation_route_ranks(route_seq,net_rank) "
                "SELECT seq,row_number() OVER (ORDER BY net_id,seq)-1 "
                "FROM routes"
            )
            connection.execute(
                "CREATE TEMP TABLE validation_route_order("
                "position INTEGER PRIMARY KEY,net_rank INTEGER NOT NULL UNIQUE)"
            )
            connection.executemany(
                "INSERT INTO validation_route_order(position,net_rank) "
                "VALUES(?,?)",
                enumerate(net_ranks),
            )
            route_cursor = connection.execute(
                "SELECT o.position,o.net_rank,r.* "
                "FROM validation_route_order o "
                "JOIN validation_route_ranks k USING(net_rank) "
                "JOIN routes r ON r.seq=k.route_seq ORDER BY o.position"
            )
            sink_cursor = iter(connection.execute(
                "SELECT o.position,r.seq,s.seq,s.sink_fpga "
                "FROM validation_route_order o "
                "JOIN validation_route_ranks k USING(net_rank) "
                "JOIN routes r ON r.seq=k.route_seq "
                "JOIN route_sinks s ON s.route_seq=r.seq "
                "ORDER BY o.position,s.seq"
            ))
            edge_cursor = iter(connection.execute(
                "SELECT o.position,r.seq,e.seq,e.link_id,e.source_fpga,"
                "e.sink_fpga FROM validation_route_order o "
                "JOIN validation_route_ranks k USING(net_rank) "
                "JOIN routes r ON r.seq=k.route_seq "
                "JOIN route_edges e ON e.route_seq=r.seq "
                "ORDER BY o.position,e.seq"
            ))
            predecessor_cursor = iter(connection.execute(
                "SELECT o.position,r.seq,p.seq,p.predecessor_net,pk.net_rank "
                "FROM validation_route_order o "
                "JOIN validation_route_ranks k USING(net_rank) "
                "JOIN routes r ON r.seq=k.route_seq "
                "JOIN route_predecessors p ON p.route_seq=r.seq "
                "LEFT JOIN routes pr ON pr.net_id=p.predecessor_net "
                "LEFT JOIN validation_route_ranks pk ON pk.route_seq=pr.seq "
                "ORDER BY o.position,p.seq"
            ))
            sink_row = next(sink_cursor, None)
            edge_row = next(edge_cursor, None)
            predecessor_row = next(predecessor_cursor, None)
            observed = 0
            for row in route_cursor:
                position = int(row["position"])
                net_rank = int(row["net_rank"])
                route_seq = int(row["seq"])
                route = _route_scalars_from_row(row, self._user_version)
                route.update(
                    {
                        "id": str(row["demand_id"]),
                        "net": str(row["net_id"]),
                        "source": str(row["source_fpga"]),
                    }
                )
                sinks: list[str] = []
                while sink_row is not None and int(sink_row[0]) == position:
                    if int(sink_row[1]) != route_seq:
                        raise ValidationError(
                            "system-route sink order disagrees"
                        )
                    sinks.append(str(sink_row[3]))
                    sink_row = next(sink_cursor, None)
                edges: list[dict[str, str]] = []
                while edge_row is not None and int(edge_row[0]) == position:
                    if int(edge_row[1]) != route_seq:
                        raise ValidationError(
                            "system-route edge order disagrees"
                        )
                    edges.append(
                        {
                            "link": str(edge_row[3]),
                            "from": str(edge_row[4]),
                            "to": str(edge_row[5]),
                        }
                    )
                    edge_row = next(edge_cursor, None)
                predecessors: list[str] = []
                predecessor_ranks: list[int] = []
                while (
                    predecessor_row is not None
                    and int(predecessor_row[0]) == position
                ):
                    if (
                        int(predecessor_row[1]) != route_seq
                        or predecessor_row[4] is None
                    ):
                        raise ValidationError(
                            "system route has an unknown predecessor net"
                        )
                    predecessors.append(str(predecessor_row[3]))
                    predecessor_ranks.append(int(predecessor_row[4]))
                    predecessor_row = next(predecessor_cursor, None)
                route["sinks"] = sinks
                route["tree_edges"] = edges
                if (
                    row["dependency_level"] is None
                    or row["combinational_dependency_depth"] is None
                ):
                    raise ValidationError(
                        "indexed exact route dependency metadata is invalid"
                    )
                route["predecessor_cut_nets"] = predecessors
                route["dependency_level"] = int(row["dependency_level"])
                route["combinational_dependency_depth"] = int(
                    row["combinational_dependency_depth"]
                )
                observed += 1
                yield net_rank, route, tuple(predecessor_ranks)
            if (
                observed != self._count
                or sink_row is not None
                or edge_row is not None
                or predecessor_row is not None
            ):
                raise ValidationError(
                    "system-route validation order coverage disagrees"
                )
        except (sqlite3.DatabaseError, sqlite3.IntegrityError) as error:
            raise ValidationError(
                "system-route validation order is malformed"
            ) from error
        finally:
            connection.close()

    def iter_cross_layer_route_metadata(
        self,
    ) -> Iterator[tuple[str, str, str, tuple[str, ...]]]:
        """Yield the compact net/demand/source/sink columns used by Phase 5."""

        connection = _connect(self.store_path)
        try:
            route_cursor = connection.execute(
                "SELECT seq,net_id,demand_id,source_fpga FROM routes "
                "ORDER BY net_id,seq"
            )
            sink_cursor = iter(connection.execute(
                "SELECT r.seq,s.sink_fpga FROM routes r JOIN route_sinks s "
                "ON s.route_seq=r.seq ORDER BY r.net_id,r.seq,s.seq"
            ))
            sink_row = next(sink_cursor, None)
            observed = 0
            for row in route_cursor:
                route_seq = int(row[0])
                sinks: list[str] = []
                while sink_row is not None and int(sink_row[0]) == route_seq:
                    sinks.append(str(sink_row[1]))
                    sink_row = next(sink_cursor, None)
                observed += 1
                yield str(row[1]), str(row[2]), str(row[3]), tuple(sinks)
            if observed != self._count or sink_row is not None:
                raise ValidationError("system-route metadata coverage disagrees")
        finally:
            connection.close()

    def iter_cross_layer_hop_keys(
        self,
    ) -> Iterator[tuple[str, str, str, str]]:
        """Yield canonical unique demand/link/source/sink route-hop keys."""

        connection = _connect(self.store_path)
        try:
            observed = 0
            for row in connection.execute(
                "SELECT r.demand_id,e.link_id,e.source_fpga,e.sink_fpga "
                "FROM routes r JOIN route_edges e ON e.route_seq=r.seq "
                "ORDER BY r.demand_id,e.link_id,e.source_fpga,e.sink_fpga"
            ):
                observed += 1
                yield tuple(str(value) for value in row)  # type: ignore[return-value]
            expected = connection.execute(
                "SELECT count(*) FROM route_edges"
            ).fetchone()[0]
            if observed != expected:
                raise ValidationError("system-route hop coverage disagrees")
        finally:
            connection.close()

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self)[index]
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        order_sql = {
            "seq": "seq",
            "net": "net_id,seq",
            "demand": "demand_id,seq",
        }[self.order]
        connection = _connect(self.store_path)
        try:
            row = connection.execute(
                f"SELECT * FROM routes ORDER BY {order_sql} LIMIT 1 OFFSET ?",
                (index,),
            ).fetchone()
            if row is None:
                raise ValidationError("system route sequence is not contiguous")
            return _route_from_row(connection, row, self._user_version)
        finally:
            connection.close()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Sequence) and not isinstance(other, (str, bytes)):
            return len(self) == len(other) and all(
                left == right for left, right in zip(self, other)
            )
        return False

    def __ne__(self, other: object) -> bool:
        return not self == other


class SystemRoutesView(dict):
    """Shallow logical document whose graph-sized arrays remain lazy."""

    def __init__(self, manifest_path: Path):
        self.manifest_path = Path(manifest_path).resolve()
        with self.manifest_path.open("r", encoding="utf-8") as stream:
            manifest = json.load(stream)
        if not isinstance(manifest, dict):
            raise ValidationError("system-route selector is not an object")
        header = manifest.get("header")
        sections = manifest.get("sections")
        user_version = manifest.get("user_version")
        if (
            manifest.get("schema") != SYSTEM_ROUTE_STORAGE_SCHEMA
            or manifest.get("logical_schema") != SYSTEM_ROUTES_SCHEMA
            or manifest.get("application_id") != SYSTEM_ROUTE_APPLICATION_ID
            or user_version not in _SUPPORTED_SYSTEM_ROUTE_USER_VERSIONS
            or not isinstance(header, dict)
            or header.get("schema") != SYSTEM_ROUTES_SCHEMA
            or any(key in header for key in ("demands", "routes"))
            or not isinstance(sections, dict)
        ):
            raise ValidationError("system-route storage manifest is malformed")
        expected_sections = {"demands", "routes", "timing_paths"}
        if user_version >= 2:
            expected_sections.update(
                {"master_selections", "refinement_batch_paths"}
            )
        if set(sections) != expected_sections:
            raise ValidationError(
                "system-route storage section coverage is malformed"
            )
        for section in ("demands", "routes", "timing_paths"):
            value = sections.get(section)
            if (
                not isinstance(value, dict)
                or isinstance(value.get("records"), bool)
                or not isinstance(value.get("records"), int)
                or value["records"] < 0
                or not _valid_digest(value.get("sha256"))
            ):
                raise ValidationError(
                    f"system-route {section} seal is malformed"
                )
        if user_version >= 2:
            selection_section = sections["master_selections"]
            batch_section = sections["refinement_batch_paths"]
            if (
                not isinstance(selection_section, dict)
                or set(selection_section) != {"records", "sha256"}
                or isinstance(selection_section.get("records"), bool)
                or not isinstance(selection_section.get("records"), int)
                or selection_section["records"] < 0
                or not _valid_digest(selection_section.get("sha256"))
                or not isinstance(batch_section, dict)
                or set(batch_section) != {"records", "batches", "sha256"}
                or any(
                    isinstance(batch_section.get(key), bool)
                    or not isinstance(batch_section.get(key), int)
                    or batch_section[key] < 0
                    for key in ("records", "batches")
                )
                or not _valid_digest(batch_section.get("sha256"))
            ):
                raise ValidationError(
                    "system-route optimization relation seal is malformed"
                )
        artifact = manifest.get("artifact")
        store_sha256 = manifest.get("store_sha256")
        if (
            not isinstance(artifact, str)
            or Path(artifact).name != artifact
            or not artifact.startswith(SYSTEM_ROUTE_STORE_PREFIX)
            or not artifact.endswith(SYSTEM_ROUTE_STORE_SUFFIX)
        ):
            raise ValidationError("system-route store path is invalid")
        if not _valid_digest(store_sha256):
            raise ValidationError("system-route store seal is malformed")
        if artifact != _content_addressed_store_name(store_sha256):
            raise ValidationError(
                "system-route content-addressed artifact identity disagrees"
            )
        store_path = self.manifest_path.with_name(artifact)
        try:
            store_size = store_path.stat().st_size
        except OSError as error:
            raise ValidationError("system-route store is missing") from error
        if (
            manifest.get("store_bytes") != store_size
            or file_sha256(store_path) != store_sha256
            or not _valid_digest(manifest.get("logical_sha256"))
        ):
            raise ValidationError("system-route store seal disagrees")
        connection = _connect(store_path)
        try:
            if (
                connection.execute("PRAGMA application_id").fetchone()[0]
                != SYSTEM_ROUTE_APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0]
                != user_version
            ):
                raise ValidationError("unsupported system-route SQLite artifact")
            route_columns = tuple(
                str(row[1])
                for row in connection.execute("PRAGMA table_info(routes)")
            )
            expected_route_columns = (
                _TYPED_ROUTE_COLUMNS
                if user_version >= 3
                else _LEGACY_ROUTE_COLUMNS
            )
            if route_columns != expected_route_columns:
                raise ValidationError(
                    "unsupported system-route routes relation schema"
                )
            metadata = {
                str(row[0]): json.loads(row[1])
                for row in connection.execute("SELECT key,value FROM metadata")
            }
            for table in ("demands", "routes", "timing_paths"):
                count, minimum, maximum = connection.execute(
                    f"SELECT count(*),min(seq),max(seq) FROM {table}"
                ).fetchone()
                expected = sections[table]["records"]
                if (
                    count != expected
                    or minimum != (0 if expected else None)
                    or maximum != (expected - 1 if expected else None)
                ):
                    raise ValidationError(
                        f"system-route {table} sequence coverage disagrees"
                    )
            if user_version >= 2:
                for table, section in (
                    ("route_master_selections", "master_selections"),
                    ("refinement_batch_paths", "refinement_batch_paths"),
                ):
                    count, minimum, maximum = connection.execute(
                        f"SELECT count(*),min(seq),max(seq) FROM {table}"
                    ).fetchone()
                    expected = sections[section]["records"]
                    if (
                        count != expected
                        or minimum != (0 if expected else None)
                        or maximum != (expected - 1 if expected else None)
                    ):
                        raise ValidationError(
                            f"system-route {table} sequence coverage disagrees"
                        )
            if metadata.get("header") != header or metadata.get(
                "sections"
            ) != sections or metadata.get("logical_sha256") != manifest.get(
                "logical_sha256"
            ):
                raise ValidationError("system-route metadata seal disagrees")
            orphan = connection.execute(
                "SELECT route_seq FROM demands LEFT JOIN routes "
                "ON routes.seq=demands.route_seq WHERE routes.seq IS NULL "
                "OR routes.demand_id!=demands.demand_id "
                "OR demands.seq!=demands.route_seq "
                "UNION ALL SELECT route_seq FROM route_sinks LEFT JOIN routes "
                "ON routes.seq=route_sinks.route_seq WHERE routes.seq IS NULL "
                "UNION ALL SELECT route_seq FROM route_edges LEFT JOIN routes "
                "ON routes.seq=route_edges.route_seq WHERE routes.seq IS NULL "
                "UNION ALL SELECT route_seq FROM route_predecessors LEFT JOIN routes "
                "ON routes.seq=route_predecessors.route_seq WHERE routes.seq IS NULL "
                "LIMIT 1"
            ).fetchone()
            noncontiguous = None
            for table in (
                "route_sinks",
                "route_edges",
                "route_predecessors",
            ):
                noncontiguous = connection.execute(
                    f"SELECT route_seq FROM {table} GROUP BY route_seq "
                    "HAVING min(seq)!=0 OR max(seq)!=count(*)-1 LIMIT 1"
                ).fetchone()
                if noncontiguous is not None:
                    break
            malformed_timing = connection.execute(
                "SELECT seq FROM timing_paths WHERE path_id IS NOT "
                "coalesce(json_extract(record_json,'$.path'),"
                "json_extract(record_json,'$.id')) LIMIT 1"
            ).fetchone()
            malformed_optimization = None
            if user_version >= 2:
                malformed_optimization = connection.execute(
                    "SELECT s.seq FROM route_master_selections s "
                    "LEFT JOIN routes r ON r.demand_id=s.demand_id "
                    "WHERE r.seq IS NULL OR r.seq!=s.seq "
                    "UNION ALL SELECT b.batch_index "
                    "FROM refinement_batch_paths b "
                    "WHERE b.batch_index<0 OR b.batch_index>=? "
                    "OR b.path_index<0 OR b.path_index>=? "
                    "UNION ALL SELECT batch_index "
                    "FROM refinement_batch_paths GROUP BY batch_index "
                    "HAVING min(position)!=0 OR max(position)!=count(*)-1 "
                    "LIMIT 1",
                    (
                        sections["refinement_batch_paths"]["batches"],
                        sections["timing_paths"]["records"],
                    ),
                ).fetchone()
            if (
                orphan is not None
                or noncontiguous is not None
                or malformed_timing is not None
                or malformed_optimization is not None
            ):
                raise ValidationError("system-route typed relation is malformed")
        except (json.JSONDecodeError, sqlite3.DatabaseError) as error:
            raise ValidationError("system-route SQLite artifact is corrupt") from error
        finally:
            connection.close()
        value = dict(header)
        value["demands"] = SystemDemandSequence(
            store_path, sections["demands"]["records"], int(user_version)
        )
        value["routes"] = SystemRouteSequence(
            store_path, sections["routes"]["records"], int(user_version)
        )
        timing_header = value.pop("timing", None)
        if timing_header is not None:
            if not isinstance(timing_header, dict) or "paths" in timing_header:
                raise ValidationError("system-route timing header is malformed")
            value["timing"] = {
                **timing_header,
                "paths": _JsonRecordSequence(
                    store_path,
                    "timing_paths",
                    sections["timing_paths"]["records"],
                ),
            }
        elif sections["timing_paths"]["records"]:
            raise ValidationError("system-route timing paths lack a header")
        if user_version >= 2:
            joint = value.get("joint_optimization")
            has_optimization_relations = bool(
                sections["master_selections"]["records"]
                or sections["refinement_batch_paths"]["records"]
                or sections["refinement_batch_paths"]["batches"]
            )
            if not isinstance(joint, dict):
                if has_optimization_relations:
                    raise ValidationError(
                        "system-route optimization relations lack a header"
                    )
            elif has_optimization_relations:
                joint = dict(joint)
                candidate_generation_value = joint.get(
                    "candidate_generation"
                )
                refinement_value = joint.get("refinement_batches")
                candidate_generation = (
                    dict(candidate_generation_value)
                    if isinstance(candidate_generation_value, dict)
                    else candidate_generation_value
                )
                refinement = (
                    dict(refinement_value)
                    if isinstance(refinement_value, dict)
                    else refinement_value
                )
                if not isinstance(candidate_generation, dict) or not isinstance(
                    refinement, dict
                ):
                    raise ValidationError(
                        "system-route optimization header is malformed"
                    )
                if (
                    "master_selection" in candidate_generation
                    or "batches" in refinement
                ):
                    raise ValidationError(
                        "system-route optimization header duplicates typed records"
                    )
                candidate_generation["master_selection"] = (
                    _MasterSelectionSequence(
                        store_path,
                        sections["master_selections"]["records"],
                    )
                )
                refinement["batches"] = _RefinementBatchSequence(
                    store_path,
                    sections["refinement_batch_paths"]["batches"],
                    sections["refinement_batch_paths"]["records"],
                )
                joint["candidate_generation"] = candidate_generation
                joint["refinement_batches"] = refinement
                value["joint_optimization"] = joint
        super().__init__(value)
        self.store_path = store_path
        self.storage_manifest = manifest
        self.storage_user_version = int(user_version)


def pretty_system_routes_sha256(routes: Mapping[str, Any]) -> str:
    if isinstance(routes, SystemRoutesView):
        return str(routes.storage_manifest["logical_sha256"])
    return _pretty_sha256(routes)


def read_system_routes(path: Path) -> Mapping[str, Any]:
    if Path(path).suffix == ".gz":
        from .io import read_json

        return read_json(path)
    with Path(path).open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValidationError("system routes must be an object")
    if value.get("schema") == SYSTEM_ROUTE_STORAGE_SCHEMA:
        return SystemRoutesView(path)
    if value.get("schema") == "emuflow.managed-json-storage/v1":
        from .managed_json_storage import expand_managed_json

        return expand_managed_json(value, path)
    return value


def system_route_store_path(path: Path) -> Path | None:
    """Resolve a typed route selector's sealed sidecar, or legacy ``None``."""

    with Path(path).open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValidationError("system routes must be an object")
    if value.get("schema") != SYSTEM_ROUTE_STORAGE_SCHEMA:
        return None
    return SystemRoutesView(path).store_path


def expand_system_route_storage(
    manifest: Mapping[str, Any], path: Path
) -> dict[str, Any]:
    del manifest
    view = SystemRoutesView(path)
    value = dict(view)
    value["demands"] = list(view["demands"])
    value["routes"] = list(view["routes"])
    if "timing" in view:
        value["timing"] = {
            **view["timing"],
            "paths": list(view["timing"]["paths"]),
        }
    if view.storage_user_version >= 2 and "joint_optimization" in view:
        joint = dict(view["joint_optimization"])
        candidate_value = joint.get("candidate_generation")
        refinement_value = joint.get("refinement_batches")
        if (
            isinstance(candidate_value, Mapping)
            and "master_selection" in candidate_value
            and isinstance(refinement_value, Mapping)
            and "batches" in refinement_value
        ):
            candidate_generation = dict(candidate_value)
            candidate_generation["master_selection"] = list(
                candidate_generation["master_selection"]
            )
            refinement = dict(refinement_value)
            refinement["batches"] = list(refinement["batches"])
            joint["candidate_generation"] = candidate_generation
            joint["refinement_batches"] = refinement
            value["joint_optimization"] = joint
    if _pretty_sha256(value) != view.storage_manifest["logical_sha256"]:
        raise ValidationError("system-route logical reconstruction disagrees")
    return value


def write_system_route_storage(
    manifest_path: Path, routes: Mapping[str, Any]
) -> dict[str, Any]:
    if routes.get("schema") != SYSTEM_ROUTES_SCHEMA:
        raise ValidationError("system-route storage requires system routes")
    raw_demands = routes.get("demands")
    raw_routes = routes.get("routes")
    if not isinstance(raw_demands, Sequence) or isinstance(raw_demands, (str, bytes)):
        raise ValidationError("system routes demands must be an array")
    if not isinstance(raw_routes, Sequence) or isinstance(raw_routes, (str, bytes)):
        raise ValidationError("system routes routes must be an array")
    header = dict(routes)
    del header["demands"]
    del header["routes"]
    raw_master_selections: Sequence[Mapping[str, Any]] = []
    raw_refinement_batches: Sequence[Sequence[int]] = []
    joint = header.get("joint_optimization")
    if isinstance(joint, Mapping):
        joint_header = dict(joint)
        candidate_generation = joint_header.get("candidate_generation")
        if isinstance(candidate_generation, Mapping):
            candidate_header = dict(candidate_generation)
            raw_selection_value = candidate_header.pop(
                "master_selection", []
            )
            if not isinstance(raw_selection_value, Sequence) or isinstance(
                raw_selection_value, (str, bytes)
            ):
                raise ValidationError(
                    "system-route master selection must be an array"
                )
            raw_master_selections = raw_selection_value
            joint_header["candidate_generation"] = candidate_header
        refinement = joint_header.get("refinement_batches")
        if isinstance(refinement, Mapping):
            refinement_header = dict(refinement)
            raw_batch_value = refinement_header.pop("batches", [])
            if not isinstance(raw_batch_value, Sequence) or isinstance(
                raw_batch_value, (str, bytes)
            ):
                raise ValidationError(
                    "system-route refinement batches must be an array"
                )
            raw_refinement_batches = raw_batch_value
            joint_header["refinement_batches"] = refinement_header
        header["joint_optimization"] = joint_header
    timing = header.get("timing")
    raw_timing_paths: Sequence[Mapping[str, Any]] = []
    if timing is not None:
        if not isinstance(timing, Mapping):
            raise ValidationError("system routes timing must be an object")
        paths = timing.get("paths")
        if not isinstance(paths, Sequence) or isinstance(paths, (str, bytes)):
            raise ValidationError("system routes timing paths must be an array")
        raw_timing_paths = paths
        header["timing"] = {
            key: value for key, value in timing.items() if key != "paths"
        }

    # Normalize macOS /var -> /private/var aliases before any generation
    # comparison.  Mixing resolved previous paths with unresolved new paths can
    # otherwise make a same-content rewrite delete its only sidecar.
    manifest_path = Path(manifest_path).resolve()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    previous_artifact = _previous_route_artifact(manifest_path)
    temporary = manifest_path.with_name(
        f".{manifest_path.name}.{uuid.uuid4().hex}.sqlite.tmp"
    )
    relation_digests = {
        name: hashlib.sha256()
        for name in (
            "demands",
            "routes",
            "timing_paths",
            "master_selections",
            "refinement_batch_paths",
        )
    }
    connection = sqlite3.connect(temporary)
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=FILE")
        connection.executescript(_DDL)
        connection.execute(f"PRAGMA application_id={SYSTEM_ROUTE_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={SYSTEM_ROUTE_USER_VERSION}")
        if len(raw_demands) != len(raw_routes):
            raise ValidationError("system-route demand/route coverage disagrees")
        for seq, route in enumerate(raw_routes):
            if not isinstance(route, Mapping):
                raise ValidationError("system route is not an object")
            route_id = route.get("id")
            net = route.get("net")
            source = route.get("source")
            sinks = route.get("sinks")
            edges = route.get("tree_edges")
            predecessors = route.get("predecessor_cut_nets", [])
            if (
                not isinstance(route_id, str)
                or not route_id
                or not isinstance(net, str)
                or not net
                or not isinstance(source, str)
                or not source
                or not isinstance(sinks, list)
                or not all(isinstance(sink, str) and sink for sink in sinks)
                or not isinstance(edges, list)
                or not isinstance(predecessors, list)
                or not all(
                    isinstance(predecessor, str) and predecessor
                    for predecessor in predecessors
                )
            ):
                raise ValidationError("system route typed fields are malformed")
            payload = _canonical(route)
            relation_digests["routes"].update(payload.encode("utf-8"))
            relation_digests["routes"].update(b"\n")
            scalar_values = _typed_route_scalar_values(route)
            connection.execute(
                "INSERT INTO routes VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    seq,
                    route_id,
                    net,
                    source,
                    *scalar_values,
                ),
            )
            demand = raw_demands[seq]
            if not isinstance(demand, Mapping) or demand != _demand_from_route(route):
                raise ValidationError(
                    "system-route demand does not match its routed record"
                )
            demand_payload = _canonical(demand)
            relation_digests["demands"].update(demand_payload.encode("utf-8"))
            relation_digests["demands"].update(b"\n")
            connection.execute(
                "INSERT INTO demands VALUES(?,?,?)",
                (seq, demand["id"], seq),
            )
            connection.executemany(
                "INSERT INTO route_sinks VALUES(?,?,?)",
                ((seq, index, sink) for index, sink in enumerate(sinks)),
            )
            connection.executemany(
                "INSERT INTO route_edges VALUES(?,?,?,?,?)",
                (
                    (seq, index, edge["link"], edge["from"], edge["to"])
                    for index, edge in enumerate(edges)
                ),
            )
            connection.executemany(
                "INSERT INTO route_predecessors VALUES(?,?,?)",
                (
                    (seq, index, predecessor)
                    for index, predecessor in enumerate(predecessors)
                ),
            )
        for seq, timing_path in enumerate(raw_timing_paths):
            if not isinstance(timing_path, Mapping):
                raise ValidationError("system-route timing path is malformed")
            identity = timing_path.get("path", timing_path.get("id"))
            if not isinstance(identity, str):
                raise ValidationError("system-route timing path lacks identity")
            payload = _canonical(timing_path)
            relation_digests["timing_paths"].update(payload.encode("utf-8"))
            relation_digests["timing_paths"].update(b"\n")
            connection.execute(
                "INSERT INTO timing_paths VALUES(?,?,?)",
                (seq, identity, payload),
            )
        for seq, selection in enumerate(raw_master_selections):
            if (
                not isinstance(selection, Mapping)
                or set(selection) != {"demand", "generator"}
                or not isinstance(selection.get("demand"), str)
                or not isinstance(selection.get("generator"), str)
                or seq >= len(raw_routes)
                or raw_routes[seq].get("id") != selection["demand"]
            ):
                raise ValidationError(
                    "system-route master selection is malformed or unordered"
                )
            payload = _canonical(selection)
            relation_digests["master_selections"].update(
                payload.encode("utf-8")
            )
            relation_digests["master_selections"].update(b"\n")
            connection.execute(
                "INSERT INTO route_master_selections VALUES(?,?,?)",
                (seq, selection["demand"], selection["generator"]),
            )
        batch_record_count = 0
        seen_path_indices = bytearray(len(raw_timing_paths))
        seen_path_count = 0
        for batch_index, batch in enumerate(raw_refinement_batches):
            if not isinstance(batch, Sequence) or isinstance(
                batch, (str, bytes)
            ):
                raise ValidationError(
                    "system-route refinement batch is not an array"
                )
            for position, path_index in enumerate(batch):
                if (
                    isinstance(path_index, bool)
                    or not isinstance(path_index, int)
                    or path_index < 0
                    or path_index >= len(raw_timing_paths)
                    or seen_path_indices[path_index]
                ):
                    raise ValidationError(
                        "system-route refinement path coverage is malformed"
                    )
                seen_path_indices[path_index] = 1
                seen_path_count += 1
                record = {
                    "batch": batch_index,
                    "position": position,
                    "path": path_index,
                }
                payload = _canonical(record)
                relation_digests["refinement_batch_paths"].update(
                    payload.encode("utf-8")
                )
                relation_digests["refinement_batch_paths"].update(b"\n")
                connection.execute(
                    "INSERT INTO refinement_batch_paths VALUES(?,?,?,?)",
                    (
                        batch_record_count,
                        batch_index,
                        position,
                        path_index,
                    ),
                )
                batch_record_count += 1
        if raw_refinement_batches:
            refinement_header = header["joint_optimization"][
                "refinement_batches"
            ]
            if (
                refinement_header.get("batch_count")
                != len(raw_refinement_batches)
                or refinement_header.get("maximum_parallel_batch")
                != max(map(len, raw_refinement_batches), default=0)
                or seen_path_count != len(raw_timing_paths)
            ):
                raise ValidationError(
                    "system-route refinement batch summary disagrees"
                )
        sections = {
            "demands": {
                "records": len(raw_demands),
                "sha256": relation_digests["demands"].hexdigest(),
            },
            "routes": {
                "records": len(raw_routes),
                "sha256": relation_digests["routes"].hexdigest(),
            },
            "timing_paths": {
                "records": len(raw_timing_paths),
                "sha256": relation_digests["timing_paths"].hexdigest(),
            },
            "master_selections": {
                "records": len(raw_master_selections),
                "sha256": relation_digests[
                    "master_selections"
                ].hexdigest(),
            },
            "refinement_batch_paths": {
                "records": batch_record_count,
                "batches": len(raw_refinement_batches),
                "sha256": relation_digests[
                    "refinement_batch_paths"
                ].hexdigest(),
            },
        }
        logical_sha256 = _pretty_sha256(routes)
        connection.executemany(
            "INSERT INTO metadata VALUES(?,?)",
            (
                ("header", _canonical(header)),
                ("sections", _canonical(sections)),
                ("logical_sha256", _canonical(logical_sha256)),
            ),
        )
        connection.commit()
    except sqlite3.IntegrityError as error:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise ValidationError(
            "system-route storage contains duplicate or invalid identities"
        ) from error
    except BaseException:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise
    connection.close()
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    store_sha256 = file_sha256(temporary)
    store_name = _content_addressed_store_name(store_sha256)
    store_path = manifest_path.with_name(store_name)
    created_artifact = not store_path.exists()
    if not created_artifact:
        if file_sha256(store_path) != store_sha256:
            temporary.unlink(missing_ok=True)
            raise ValidationError("existing system-route store seal disagrees")
        temporary.unlink()
    else:
        os.replace(temporary, store_path)
    manifest = {
        "schema": SYSTEM_ROUTE_STORAGE_SCHEMA,
        "logical_schema": SYSTEM_ROUTES_SCHEMA,
        "application_id": SYSTEM_ROUTE_APPLICATION_ID,
        "user_version": SYSTEM_ROUTE_USER_VERSION,
        "header": header,
        "sections": sections,
        "logical_sha256": logical_sha256,
        "artifact": store_name,
        "store_bytes": store_path.stat().st_size,
        "store_sha256": store_sha256,
    }
    selector_backup: Path | None = None
    if manifest_path.exists():
        selector_backup = manifest_path.with_name(
            f".{manifest_path.name}.{uuid.uuid4().hex}.previous"
        )
        try:
            os.link(manifest_path, selector_backup)
        except OSError:
            try:
                shutil.copyfile(manifest_path, selector_backup)
                with selector_backup.open("rb") as stream:
                    os.fsync(stream.fileno())
            except BaseException:
                selector_backup.unlink(missing_ok=True)
                if created_artifact and store_path != previous_artifact:
                    store_path.unlink(missing_ok=True)
                raise
    try:
        write_json(manifest_path, manifest, compact=True)
        # The selector is the commit point, but old data remains recoverable
        # until a fresh reader has checked every selector, store and relation
        # seal.  This also catches failures injected after atomic replacement.
        SystemRoutesView(manifest_path)
    except BaseException:
        if selector_backup is not None:
            os.replace(selector_backup, manifest_path)
        else:
            manifest_path.unlink(missing_ok=True)
        if created_artifact and store_path != previous_artifact:
            store_path.unlink(missing_ok=True)
        raise
    if selector_backup is not None:
        selector_backup.unlink(missing_ok=True)
    if previous_artifact is not None and previous_artifact != store_path:
        previous_artifact.unlink(missing_ok=True)
    return manifest
