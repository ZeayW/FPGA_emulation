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
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri
from emuflow.common.errors import ValidationError
from emuflow.storage.store_publication import bind_store_source, require_store_sources_unchanged
from emuflow.common.io import file_sha256
from emuflow.routing.routing import SYSTEM_ROUTES_SCHEMA


SYSTEM_ROUTE_STORAGE_SCHEMA = "emuflow.system-route-storage/v1"
SYSTEM_ROUTE_APPLICATION_ID = 0x454D5552  # ASCII "EMUR"
SYSTEM_ROUTE_USER_VERSION = 3
_SUPPORTED_SYSTEM_ROUTE_USER_VERSIONS = {1, 2, SYSTEM_ROUTE_USER_VERSION}
SYSTEM_ROUTE_STORE_PREFIX = "routes."
SYSTEM_ROUTE_STORE_SUFFIX = ".sqlite"


_SELECTOR_SCHEMA_PATTERN = re.compile(
    rb'"schema"\s*:\s*"emuflow\.system-route-storage/v1"'
)


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


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        immutable_sqlite_uri(path), uri=True
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
        self._source_bindings = None
        self._logical_identity = None
        if order not in {"seq", "net", "demand"}:
            raise ValueError(f"unsupported route order {order!r}")
        self.order = order

    def __len__(self) -> int:
        return self._count

    def ordered(self, order: str) -> "SystemRouteSequence":
        result = SystemRouteSequence(
            self.store_path, self._count, self._user_version, order
        )
        result._source_bindings = self._source_bindings
        result._logical_identity = self._logical_identity
        return result

    def bound_store_sources(self):
        if self._source_bindings is None:
            raise ValidationError("route cursor has no original source owner")
        require_store_sources_unchanged(self._source_bindings)
        return self._source_bindings

    def native_logical_identity(self):
        self.bound_store_sources()
        if self._user_version != 3 or self._logical_identity is None:
            raise ValidationError(
                "native route projection requires a current logical owner"
            )
        return self._logical_identity

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

    def iter_native_exact_metadata(self) -> Iterator[tuple[dict[str, Any], int, list[int]]]:
        """Compatibility metadata only; native ingress owns all tree edges."""
        connection = _connect(self.store_path)
        try:
            rows = connection.execute(
                "SELECT seq,demand_id,net_id,source_fpga,dependency_level,"
                "combinational_dependency_depth,row_number() OVER "
                "(ORDER BY demand_id,seq)-1 AS demand_rank FROM routes ORDER BY net_id,seq"
            )
            sinks = iter(connection.execute(
                "SELECT r.seq,s.sink_fpga FROM route_sinks s JOIN routes r ON r.seq=s.route_seq "
                "ORDER BY r.net_id,r.seq,s.seq"
            ))
            predecessors = iter(connection.execute(
                "WITH ranked AS (SELECT seq,net_id,row_number() OVER (ORDER BY net_id,seq)-1 AS rank FROM routes) "
                "SELECT r.seq,p.predecessor_net,parent.rank FROM route_predecessors p "
                "JOIN routes r ON r.seq=p.route_seq LEFT JOIN ranked parent ON parent.net_id=p.predecessor_net "
                "ORDER BY r.net_id,r.seq,p.seq"
            ))
            sink, predecessor = next(sinks,None), next(predecessors,None)
            observed = 0
            for row in rows:
                route_sinks, names, ranks = [], [], []
                while sink is not None and sink[0] == row[0]:
                    route_sinks.append(str(sink[1])); sink = next(sinks,None)
                while predecessor is not None and predecessor[0] == row[0]:
                    if predecessor[2] is None:
                        raise ValidationError("system route has an unknown predecessor net")
                    names.append(str(predecessor[1])); ranks.append(int(predecessor[2]))
                    predecessor = next(predecessors,None)
                observed += 1
                yield ({"id":str(row[1]),"net":str(row[2]),"source":str(row[3]),"sinks":route_sinks,
                        "predecessor_cut_nets":names,"dependency_level":int(row[4]),
                        "combinational_dependency_depth":int(row[5])},int(row[6]),ranks)
            if observed != self._count or sink is not None or predecessor is not None:
                raise ValidationError("system-route metadata coverage disagrees")
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
        selector_source = bind_store_source(
            self.manifest_path, "system-route-selector-v1"
        )
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
            store_source = bind_store_source(
                store_path, f"system-route-store-v{user_version}"
            )
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
        self.storage_manifest = manifest
        self.storage_user_version = int(user_version)
        self.store_path = store_path.resolve()
        self._source_bindings = (selector_source, store_source)
        self["routes"]._source_bindings = self._source_bindings
        self["routes"]._logical_identity = manifest["logical_sha256"]
        require_store_sources_unchanged(self._source_bindings)

    def bound_store_sources(self):
        if self.storage_manifest["user_version"] != SYSTEM_ROUTE_USER_VERSION:
            raise ValidationError(
                "typed publication requires the current RouteStore owner"
            )
        require_store_sources_unchanged(self._source_bindings)
        return self._source_bindings


def pretty_system_routes_sha256(routes: Mapping[str, Any]) -> str:
    if isinstance(routes, SystemRoutesView):
        return str(routes.storage_manifest["logical_sha256"])
    return _pretty_sha256(routes)


def read_system_routes(path: Path) -> Mapping[str, Any]:
    if Path(path).suffix == ".gz":
        from emuflow.common.io import read_json

        return read_json(path)
    with Path(path).open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValidationError("system routes must be an object")
    if value.get("schema") == SYSTEM_ROUTE_STORAGE_SCHEMA:
        return SystemRoutesView(path)
    if value.get("schema") == "emuflow.managed-json-storage/v1":
        from emuflow.storage.managed_json_storage import expand_managed_json

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
