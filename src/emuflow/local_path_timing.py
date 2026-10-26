"""Source-bound post-route timing for original paths local to one FPGA."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tempfile
from collections import OrderedDict
from collections.abc import Iterator, Sequence, Set
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path
from typing import Any, Dict, Mapping

from .errors import ValidationError
from .io import read_json, write_json
from .ir import EmuIR
from .emuir_compact import is_compact_emuir, selected_net_records
from .emuir_storage import is_emuir_sqlite
from .indexed_physical_ir import IndexedPhysicalIR
from .indexed_timing import IndexedTimingView
from .logic_segment_timing import (
    LOGIC_SEGMENT_TIMING_HEADER,
    _instance_pin_inventory,
    _xilinx_object,
)
from .local_path_timing_storage import (
    LOCAL_PATH_TIMING_STORAGE_SCHEMA,
    read_local_path_timing_storage,
    write_local_path_timing_storage,
)
from .timing_disposition import OPTIMIZED_AWAY, is_optimized_away, validate_optimization_record
from .partition import PARTITION_ASSIGNMENT_SCHEMA
from .phase3_storage import read_phase3_assignment_view
from .routing import system_route_timing_paths
from .system_route_storage import read_system_routes
from .sta import (
    STA_PATH_DATABASE_SCHEMA,
    STA_PATH_DATABASE_INDEX_SCHEMA, STA_PATH_DATABASE_CLOCK_SCHEMA,
    STA_PATH_DATABASE_STREAM_SCHEMA,
    iter_sta_path_database_paths,
    sta_object_index,
    sta_path_endpoints,
)


LOCAL_PATH_IDENTITY_SCHEMA = "emuflow.local-path-identity/v1"
LOCAL_PATH_IDENTITY_SCHEMA_V2 = "emuflow.local-path-identity/v2"
LOCAL_PATH_TIMING_SCHEMA = "emuflow.local-path-timing/v1"
LOCAL_PATH_QUERY_HEADER_V2 = (
    "endpoint\tkind\tstart_pin\tend_pin\tpath_pins"
)
LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION = (
    "typed-local-path-query-identity-owner-v1"
)


def local_path_timing_header(
    identity: Mapping[str, Any], *, provider: str, qualification: str
) -> Dict[str, Any]:
    """Return the bounded header shared by local-path timing stores."""

    return {
        "schema": LOCAL_PATH_TIMING_SCHEMA,
        "status": "pass",
        "design": identity["design"],
        "fpga": identity["fpga"],
        "provider": provider,
        "qualification": qualification,
        "source": identity["source"],
        "identity_schema": identity["schema"],
        "coverage": identity["coverage"],
    }


class _WithoutDelayPath(Mapping[str, Any]):
    """Expose one timing record as its identity without copying its payload."""

    def __init__(self, source: Mapping[str, Any]):
        self.source = source

    def __getitem__(self, key: str) -> Any:
        if key in {"delay_ns", "optimization_source"}:
            raise KeyError(key)
        if key == "measurement" and is_optimized_away(self.source):
            return "endpoint-longest-path-fallback"
        return self.source[key]

    def __iter__(self) -> Iterator[str]:
        return (key for key in self.source if key not in {"delay_ns", "optimization_source"})

    def __len__(self) -> int:
        return len(self.source) - sum(key in self.source for key in ("delay_ns", "optimization_source"))


class _WithoutDelayPaths(Sequence[Mapping[str, Any]]):
    """Repeatable identity projection over list- or store-backed timing rows."""

    def __init__(self, source: Sequence[Mapping[str, Any]]):
        self.source = source

    def __len__(self) -> int:
        return len(self.source)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(len(self)))]
        item = self.source[index]
        if not isinstance(item, Mapping):
            raise ValidationError("local path timing record is invalid")
        return _WithoutDelayPath(item)

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        for item in self.source:
            if not isinstance(item, Mapping):
                raise ValidationError("local path timing record is invalid")
            yield _WithoutDelayPath(item)


class _StoredLocalPathIdentity(Mapping[str, Any]):
    """Logical identity projection over a leased typed timing owner."""

    _KEYS = (
        "schema", "status", "design", "fpga", "provider", "source",
        "coverage", "paths",
    )

    def __init__(self, timing: Any) -> None:
        self.timing = timing
        self._paths = _WithoutDelayPaths(timing["paths"])

    @property
    def store_path(self):
        return self.timing.store_path

    def bound_store_sources(self):
        return self.timing.bound_store_sources()

    def validate_identity(self) -> Dict[str, Any]:
        validation = self.timing.validate_timing()
        if (
            self.timing["qualification"]
            != LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION
            or self.timing["identity_schema"]
            != LOCAL_PATH_IDENTITY_SCHEMA_V2
            or validation["maximum_delay_ns"] != 0.0
        ):
            raise ValidationError(
                "typed local path identity owner is invalid"
            )
        return {
            "status": "pass",
            "fpga": self.timing["fpga"],
            "local_paths": validation["local_paths"],
        }

    def close(self) -> None:
        self.timing.close()

    def __enter__(self) -> "_StoredLocalPathIdentity":
        self.validate_identity()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def __getitem__(self, key: str) -> Any:
        if key == "schema":
            return self.timing["identity_schema"]
        if key == "paths":
            return self._paths
        if key in self._KEYS:
            return self.timing[key]
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._KEYS)

    def __len__(self) -> int:
        return len(self._KEYS)


def read_local_path_identity(path: Path) -> Mapping[str, Any]:
    """Open a typed production identity or an explicit legacy JSON value."""

    raw = read_json(path)
    if not isinstance(raw, Mapping):
        raise ValidationError("local path identity root is invalid")
    if raw.get("schema") != LOCAL_PATH_TIMING_STORAGE_SCHEMA:
        return raw
    timing = read_local_path_timing_storage(path)
    identity = _StoredLocalPathIdentity(timing)
    try:
        identity.validate_identity()
    except BaseException:
        identity.close()
        raise
    return identity


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def path_id_set_sha256(path_ids: list[str] | set[str]) -> str:
    """Return an unambiguous digest of a sorted set of original path IDs."""
    encoded = json.dumps(
        sorted(path_ids), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _open_prepared_store(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


class _PreparedStoreLifetime:
    """Keep one private preparation directory alive for all worker views."""

    def __init__(self) -> None:
        self.directory = tempfile.TemporaryDirectory(
            prefix="emuflow-local-path-prepared-"
        )
        self.path = Path(self.directory.name) / "paths.sqlite"


class _PreparedLocalPathSequence(Sequence[Mapping[str, Any]]):
    def __init__(
        self,
        lifetime: _PreparedStoreLifetime,
        fpga: str,
        count: int,
    ) -> None:
        self._lifetime = lifetime
        self.fpga = fpga
        self.count = count

    @staticmethod
    def _record(row: sqlite3.Row) -> Dict[str, Any]:
        record = {
            "id": str(row[0]),
            "clock_domain": str(row[1]),
            "clock_period_ns": float(row[2]),
            "slack_ns": float(row[3]),
            "fixed_delay_ns": float(row[4]),
            "startpoint": {
                "object": row[5],
                "instance": row[6],
                "port": row[7],
                "bit": int(row[8]),
            },
            "endpoint": {
                "object": row[9],
                "instance": row[10],
                "port": row[11],
                "bit": int(row[12]),
            },
        }
        return record

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        connection = _open_prepared_store(self._lifetime.path)
        try:
            observed = 0
            for row in connection.execute(
                """SELECT path_id,clock_domain,clock_period_ns,slack_ns,
                fixed_delay_ns,start_object,start_instance,start_port,start_bit,
                end_object,end_instance,end_port,end_bit
                FROM local_paths WHERE fpga=? ORDER BY path_id""",
                (self.fpga,),
            ):
                observed += 1
                yield self._record(row)
            if observed != self.count:
                raise ValidationError(
                    "prepared local path row count disagrees"
                )
        finally:
            connection.close()

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self)[index]
        if index < 0:
            index += self.count
        if index < 0 or index >= self.count:
            raise IndexError(index)
        connection = _open_prepared_store(self._lifetime.path)
        try:
            row = connection.execute(
                """SELECT path_id,clock_domain,clock_period_ns,slack_ns,
                fixed_delay_ns,start_object,start_instance,start_port,start_bit,
                end_object,end_instance,end_port,end_bit
                FROM local_paths WHERE fpga=? ORDER BY path_id LIMIT 1 OFFSET ?""",
                (self.fpga, index),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValidationError("prepared local path sequence is incomplete")
        return self._record(row)


class _PreparedLocalPathMapping(
    Mapping[str, Sequence[Mapping[str, Any]]]
):
    def __init__(
        self,
        lifetime: _PreparedStoreLifetime,
        counts: Mapping[str, int],
    ) -> None:
        self._lifetime = lifetime
        self._counts = dict(counts)

    def __len__(self) -> int:
        return len(self._counts)

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._counts))

    def __getitem__(self, fpga: str) -> Sequence[Mapping[str, Any]]:
        if fpga not in self._counts:
            raise KeyError(fpga)
        return _PreparedLocalPathSequence(
            self._lifetime, fpga, self._counts[fpga]
        )


class _PreparedIdentitySet(Set[str]):
    def __init__(
        self,
        lifetime: _PreparedStoreLifetime,
        table: str,
        count: int,
    ) -> None:
        self._lifetime = lifetime
        self._table = table
        self._count = count

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[str]:
        connection = _open_prepared_store(self._lifetime.path)
        try:
            for row in connection.execute(
                f"SELECT path_id FROM {self._table} ORDER BY path_id"
            ):
                yield str(row[0])
        finally:
            connection.close()

    def __contains__(self, identity: object) -> bool:
        if not isinstance(identity, str):
            return False
        connection = _open_prepared_store(self._lifetime.path)
        try:
            return connection.execute(
                f"SELECT 1 FROM {self._table} WHERE path_id=?",
                (identity,),
            ).fetchone() is not None
        finally:
            connection.close()


class _LocalPathPreparationWriter:
    """Build the shared path partition as typed, bounded SQLite rows."""

    def __init__(self, lifetime: _PreparedStoreLifetime) -> None:
        self.lifetime = lifetime
        self.connection = sqlite3.connect(lifetime.path)
        self.connection.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            CREATE TABLE cross_paths(
              path_id TEXT PRIMARY KEY
            ) WITHOUT ROWID;
            CREATE TABLE all_paths(
              path_id TEXT PRIMARY KEY
            ) WITHOUT ROWID;
            CREATE TABLE local_paths(
              fpga TEXT NOT NULL,
              path_id TEXT NOT NULL,
              clock_domain TEXT NOT NULL,
              clock_period_ns REAL NOT NULL,
              slack_ns REAL NOT NULL,
              fixed_delay_ns REAL NOT NULL,
              start_object TEXT,
              start_instance TEXT,
              start_port TEXT,
              start_bit INTEGER NOT NULL,
              end_object TEXT,
              end_instance TEXT,
              end_port TEXT,
              end_bit INTEGER NOT NULL,
              PRIMARY KEY(fpga,path_id)
            ) WITHOUT ROWID;
            """
        )
        self._local_batch: list[tuple[Any, ...]] = []

    def add_cross_paths(self, identities: Sequence[str]) -> None:
        self.connection.executemany(
            "INSERT OR IGNORE INTO cross_paths VALUES (?)",
            ((identity,) for identity in identities),
        )

    def add_path_ids(self, identities: Sequence[str]) -> None:
        try:
            self.connection.executemany(
                "INSERT INTO all_paths VALUES (?)",
                ((identity,) for identity in identities),
            )
        except sqlite3.IntegrityError as error:
            raise ValidationError(
                "local path database contains duplicate path identities"
            ) from error

    def cross_paths(self, identities: Sequence[str]) -> set[str]:
        result: set[str] = set()
        for start in range(0, len(identities), 900):
            chunk = identities[start:start + 900]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            result.update(
                str(row[0])
                for row in self.connection.execute(
                    f"SELECT path_id FROM cross_paths "
                    f"WHERE path_id IN ({placeholders})",
                    tuple(chunk),
                )
            )
        return result

    def add_local_path(
        self, fpga: str, record: Mapping[str, Any]
    ) -> None:
        start = record["startpoint"]
        end = record["endpoint"]
        self._local_batch.append(
            (
                fpga,
                record["id"],
                record["clock_domain"],
                float(record["clock_period_ns"]),
                float(record["slack_ns"]),
                float(record["fixed_delay_ns"]),
                start.get("object"),
                start.get("instance"),
                start.get("port"),
                int(start["bit"]),
                end.get("object"),
                end.get("instance"),
                end.get("port"),
                int(end["bit"]),
            )
        )
        if len(self._local_batch) >= 1024:
            self._flush_local()


    def _flush_local(self) -> None:
        if not self._local_batch:
            return
        try:
            self.connection.executemany(
                "INSERT INTO local_paths VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                self._local_batch,
            )
        except sqlite3.IntegrityError as error:
            raise ValidationError(
                "local path partition contains duplicate identities"
            ) from error
        self._local_batch.clear()

    def contains_cross_path(self, identity: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM cross_paths WHERE path_id=?", (identity,)
        ).fetchone() is not None

    def finish(
        self,
    ) -> tuple[
        _PreparedLocalPathMapping,
        _PreparedIdentitySet,
        tuple[int, str],
    ]:
        self._flush_local()
        self.connection.commit()
        counts = {
            str(row[0]): int(row[1])
            for row in self.connection.execute(
                "SELECT fpga,count(*) FROM local_paths GROUP BY fpga"
            )
        }
        path_count = int(self.connection.execute(
            "SELECT count(*) FROM all_paths"
        ).fetchone()[0])
        cross_count = int(self.connection.execute(
            "SELECT count(*) FROM cross_paths"
        ).fetchone()[0])
        digest = hashlib.sha256()
        digest.update(b"[")
        for index, row in enumerate(self.connection.execute(
            "SELECT path_id FROM all_paths ORDER BY path_id"
        )):
            if index:
                digest.update(b",")
            digest.update(json.dumps(
                str(row[0]), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8"))
        digest.update(b"]")
        self.connection.close()
        return (
            _PreparedLocalPathMapping(self.lifetime, counts),
            _PreparedIdentitySet(
                self.lifetime, "cross_paths", cross_count
            ),
            (path_count, digest.hexdigest()),
        )


def _source_manifest(
    database: Mapping[str, Any],
    path: Path,
    original_ir_path: Path,
    assignment_path: Path,
    routes_path: Path,
    *,
    path_ids: list[str] | None = None,
    path_identity: tuple[int, str] | None = None,
) -> Dict[str, Any]:
    if path_identity is None:
        ids = (
            [item["id"] for item in database["paths"]]
            if path_ids is None
            else path_ids
        )
        original_paths = len(ids)
        original_path_ids_sha256 = path_id_set_sha256(ids)
    else:
        original_paths, original_path_ids_sha256 = path_identity
    return {
        "path_database_sha256": _sha256(path),
        "original_ir_sha256": _sha256(original_ir_path),
        "assignment_sha256": _sha256(assignment_path),
        "routes_sha256": _sha256(routes_path),
        "original_paths": original_paths,
        "original_path_ids_sha256": original_path_ids_sha256,
    }


def validate_local_path_identity(database: Mapping[str, Any]) -> Dict[str, Any]:
    indexed_validator = getattr(database, "validate_identity", None)
    if callable(indexed_validator):
        return indexed_validator()
    schema = database.get("schema")
    if schema not in {
        LOCAL_PATH_IDENTITY_SCHEMA,
        LOCAL_PATH_IDENTITY_SCHEMA_V2,
    } or database.get("status") != "pass":
        raise ValidationError("local path identity is invalid")
    fpga = database.get("fpga")
    if not isinstance(fpga, str) or not fpga:
        raise ValidationError("local path identity FPGA is invalid")
    source = database.get("source")
    if not isinstance(source, Mapping) or set(source) != {
        "path_database_sha256",
        "original_ir_sha256",
        "assignment_sha256",
        "routes_sha256",
        "original_paths",
        "original_path_ids_sha256",
    }:
        raise ValidationError("local path identity source seal is invalid")
    if (
        any(
            not isinstance(source[field], str) or len(source[field]) != 64
            for field in (
                "path_database_sha256",
                "original_ir_sha256",
                "assignment_sha256",
                "routes_sha256",
                "original_path_ids_sha256",
            )
        )
        or isinstance(source["original_paths"], bool)
        or not isinstance(source["original_paths"], int)
        or source["original_paths"] <= 0
    ):
        raise ValidationError("local path identity source seal is invalid")
    paths = database.get("paths")
    if not isinstance(paths, Sequence) or isinstance(
        paths, (str, bytes, bytearray)
    ):
        raise ValidationError("local path identity paths are invalid")
    ids = set()
    for index, path in enumerate(paths):
        context = f"local path identity[{index}]"
        expected_fields = {
            "id",
            "kind",
            "fpga",
            "clock_domain",
            "clock_period_ns",
            "start_pin",
            "end_pin",
        }
        if schema == LOCAL_PATH_IDENTITY_SCHEMA_V2:
            expected_fields.update(
                {"required_time_ns", "measurement", "path_pins"}
            )
        if not isinstance(path, Mapping) or set(path) != expected_fields:
            raise ValidationError(f"{context} is invalid")
        path_id = path["id"]
        if (
            not isinstance(path_id, str)
            or not path_id
            or path_id in ids
            or path["kind"] != "local"
            or path["fpga"] != fpga
            or not isinstance(path["clock_domain"], str)
            or not path["clock_domain"]
            or isinstance(path["clock_period_ns"], bool)
            or not isinstance(path["clock_period_ns"], (int, float))
            or not math.isfinite(float(path["clock_period_ns"]))
            or float(path["clock_period_ns"]) <= 0.0
            or not isinstance(path["start_pin"], str)
            or not path["start_pin"]
            or not isinstance(path["end_pin"], str)
            or not path["end_pin"]
        ):
            raise ValidationError(f"{context} is invalid")
        if schema == LOCAL_PATH_IDENTITY_SCHEMA_V2:
            required_time = path["required_time_ns"]
            if (
                isinstance(required_time, bool)
                or not isinstance(required_time, (int, float))
                or not math.isfinite(float(required_time))
                or float(required_time) <= 0.0
            ):
                raise ValidationError(f"{context} required time is invalid")
            measurement = path["measurement"]
            pins = path["path_pins"]
            if measurement not in {
                "explicit-routed-path-chain",
                "endpoint-longest-path-fallback",
            } or not isinstance(pins, Sequence) or isinstance(
                pins, (str, bytes, bytearray)
            ):
                raise ValidationError(f"{context} measurement is invalid")
            if measurement == "explicit-routed-path-chain":
                if (
                    len(pins) < 2
                    or pins[0] != path["start_pin"]
                    or pins[-1] != path["end_pin"]
                    or not all(
                        isinstance(pin, str) and pin and "," not in pin
                        for pin in pins
                    )
                ):
                    raise ValidationError(f"{context} path chain is invalid")
            elif pins:
                raise ValidationError(f"{context} fallback chain is invalid")
        ids.add(path_id)
    coverage = database.get("coverage")
    if not isinstance(coverage, Mapping) or coverage != {
        "local_paths": len(paths)
    }:
        raise ValidationError("local path identity coverage is invalid")
    return {"status": "pass", "fpga": fpga, "local_paths": len(paths)}


def _same_endpoint(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(
        left.get(key) == right.get(key)
        for key in ("instance", "port", "bit")
    )


def _resolve_local_path_fpga(
    path: Mapping[str, Any],
    start: Mapping[str, Any],
    end: Mapping[str, Any],
    original_nets: Mapping[str, Mapping[str, Any]],
    instance_assignment: Mapping[str, Any],
) -> str | None:
    """Resolve a path owner without pretending top-level ports are instances.

    OpenSTA legitimately reports primary inputs and outputs as path endpoints,
    whose provider-neutral ``instance`` is ``None``.  The selected
    ``path_nets`` still certify the exact path: their unique drivers, together
    with any cell endpoints, identify the FPGA that owns the local logic.  Use
    only those drivers rather than every fanout sink, because unrelated sinks
    of the same net may live on other FPGAs.

    Return ``None`` when the evidence contains no assigned instance or an
    instance is absent from the assignment.  Multiple evidenced FPGAs mean the
    path is cross-partition and must have appeared in Phase 4 instead.
    """

    path_id = path.get("id")
    instances: set[str] = set()
    for endpoint in (start, end):
        instance = endpoint.get("instance")
        if instance is None:
            continue
        if not isinstance(instance, str) or not instance:
            raise ValidationError(
                f"original path {path_id!r} has an invalid endpoint instance"
            )
        instances.add(instance)

    path_nets = path.get("path_nets")
    if not isinstance(path_nets, list) or not path_nets:
        raise ValidationError(
            f"original path {path_id!r} has invalid selected nets"
        )
    for net_id in path_nets:
        net = original_nets.get(net_id)
        if not isinstance(net, Mapping):
            raise ValidationError(
                f"original path {path_id!r} names an unknown selected net"
            )
        drivers = net.get("drivers")
        if (
            not isinstance(drivers, list)
            or len(drivers) != 1
            or not isinstance(drivers[0], Mapping)
        ):
            raise ValidationError(
                f"original path {path_id!r} lacks one unique selected-net "
                "driver"
            )
        instance = drivers[0].get("instance")
        if instance is None:
            continue
        if not isinstance(instance, str) or not instance:
            raise ValidationError(
                f"original path {path_id!r} has an invalid selected-net driver"
            )
        instances.add(instance)

    if not instances:
        return None
    assigned_fpgas = set()
    for instance in instances:
        assigned = instance_assignment.get(instance)
        if not isinstance(assigned, str) or not assigned:
            return None
        assigned_fpgas.add(assigned)
    if len(assigned_fpgas) != 1:
        raise ValidationError(
            f"original path {path_id!r} is cross-partition but absent "
            "from the Phase 4 timing population"
        )
    return next(iter(assigned_fpgas))


@dataclass(frozen=True)
class LocalPathQueryInputs:
    """Immutable original-design timing state shared by FPGA workers."""

    original_ir: EmuIR | None
    assignment: Mapping[str, Any]
    database: Mapping[str, Any]
    routes: Mapping[str, Any]
    original_ir_path: Path
    local_paths_by_fpga: Mapping[str, Sequence[Mapping[str, Any]]]
    cross_path_ids: Set[str]
    source_manifest: Mapping[str, Any]

    @property
    def path_endpoints(
        self,
    ) -> Mapping[str, tuple[Mapping[str, Any], Mapping[str, Any]]]:
        """Compatibility view for small callers; production never builds it."""
        return {
            path["id"]: (path["startpoint"], path["endpoint"])
            for paths in self.local_paths_by_fpga.values()
            for path in paths
        }


def prepare_local_path_query_inputs(
    original_ir_path: Path,
    assignment_path: Path,
    path_database_path: Path,
    routes_path: Path,
    *,
    original_ir: EmuIR | None = None,
    assignment: Mapping[str, Any] | None = None,
    database: Mapping[str, Any] | None = None,
    routes: Mapping[str, Any] | None = None,
) -> LocalPathQueryInputs:
    """Partition TimingPathDB into bounded per-FPGA local-path records.

    The old implementation converted the complete indexed TimingPathDB into a
    Python list, then retained a second endpoint map and every selected EmuIR
    net.  Large designs expanded a 1.2 GiB SQLite payload to several GiB of
    Python objects before physical work began.  This implementation streams
    paths in bounded batches, resolves ownership once, and retains only the
    lightweight records that each FPGA worker will consume.
    """
    if assignment is None:
        assignment = read_phase3_assignment_view(
            assignment_path,
            include_instance_assignment=True,
        )
    database = database or read_json(path_database_path)
    if routes is None:
        routes = read_system_routes(routes_path)
    if database.get("schema") not in {
        STA_PATH_DATABASE_SCHEMA,
        STA_PATH_DATABASE_STREAM_SCHEMA,
        STA_PATH_DATABASE_INDEX_SCHEMA, STA_PATH_DATABASE_CLOCK_SCHEMA,
    }:
        raise ValidationError("local path STA database schema is invalid")
    if assignment.get("schema") != PARTITION_ASSIGNMENT_SCHEMA:
        raise ValidationError("local path assignment schema is invalid")
    if assignment.get("design") != database.get("design"):
        raise ValidationError("local path assignment design disagrees")
    instance_assignment = assignment.get("instance_assignment")
    if not isinstance(instance_assignment, Mapping):
        raise ValidationError("local path instance assignment is invalid")
    raw_route_timing = system_route_timing_paths(
        routes,
        context="local path system routes",
    )
    lifetime = _PreparedStoreLifetime()
    writer = _LocalPathPreparationWriter(lifetime)
    cross_batch: list[str] = []
    for item in raw_route_timing:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValidationError("local path system route timing is invalid")
        members = item.get("compressed_path_ids", [item["path"]])
        if (
            not isinstance(members, list)
            or not all(isinstance(member, str) and member for member in members)
        ):
            raise ValidationError("local path route member coverage is invalid")
        for member in members:
            cross_batch.append(member)
            if len(cross_batch) >= 4096:
                writer.add_cross_paths(cross_batch)
                cross_batch.clear()
    writer.add_cross_paths(cross_batch)
    writer.connection.commit()

    indexed = (
        original_ir is None
        and is_emuir_sqlite(original_ir_path)
        and is_compact_emuir(original_ir_path)
    )
    unresolved_count = 0
    unresolved_examples: list[dict[str, str]] = []

    def unresolved(path_id: str, reason: str) -> None:
        nonlocal unresolved_count
        unresolved_count += 1
        if len(unresolved_examples) < 10:
            unresolved_examples.append({"path": path_id, "reason": reason})

    if indexed:
        view = IndexedTimingView(original_ir_path)
        try:
            object_index = view.object_index
            iterator = iter_sta_path_database_paths(
                path_database_path,
                database,
                verify_payload=False,
            )
            while True:
                batch = []
                try:
                    for _ in range(1024):
                        batch.append(next(iterator))
                except StopIteration:
                    pass
                if not batch:
                    break
                batch_ids = [path["id"] for path in batch]
                writer.add_path_ids(batch_ids)
                cross_ids = writer.cross_paths(batch_ids)
                candidates = [
                    path for path in batch
                    if path["id"] not in cross_ids
                ]
                selected_ids = {
                    net_id
                    for path in candidates
                    for net_id in path.get("path_nets", [])
                }
                original_nets = selected_net_records(
                    view.connection, selected_ids
                )
                for path in candidates:
                    try:
                        start, end = sta_path_endpoints(path, object_index)
                        path_fpga = _resolve_local_path_fpga(
                            path,
                            start,
                            end,
                            original_nets,
                            instance_assignment,
                        )
                    except (KeyError, ValidationError) as error:
                        unresolved(path["id"], str(error))
                        continue
                    if path_fpga is None:
                        unresolved(
                            path["id"], "path-partition-unresolved"
                        )
                        continue
                    record = {
                        "id": path["id"],
                        "clock_domain": path["clock_domain"],
                        "clock_period_ns": path["clock_period_ns"],
                        "slack_ns": path["slack_ns"],
                        "fixed_delay_ns": path["fixed_delay_ns"],
                        "startpoint": start,
                        "endpoint": end,
                    }
                    writer.add_local_path(path_fpga, record)
                if len(batch) < 1024:
                    break
        finally:
            view.close()
        original_ir = None
    else:
        original_ir = original_ir or EmuIR.load(original_ir_path)
        object_index = sta_object_index(original_ir)
        original_nets = {
            item["id"]: item for item in original_ir.value["nets"]
        }
        for path in iter_sta_path_database_paths(
            path_database_path,
            database,
            verify_payload=False,
        ):
            writer.add_path_ids([path["id"]])
            if writer.contains_cross_path(path["id"]):
                continue
            try:
                start, end = sta_path_endpoints(path, object_index)
                path_fpga = _resolve_local_path_fpga(
                    path,
                    start,
                    end,
                    original_nets,
                    instance_assignment,
                )
            except (KeyError, ValidationError) as error:
                unresolved(path["id"], str(error))
                continue
            if path_fpga is None:
                unresolved(path["id"], "path-partition-unresolved")
                continue
            record = {
                **path,
                "startpoint": start,
                "endpoint": end,
            }
            record.pop("path_nets", None)
            writer.add_local_path(path_fpga, record)
    if unresolved_count:
        writer.connection.close()
        lifetime.directory.cleanup()
        raise ValidationError(
            "complete local path timing has unresolved original endpoints: "
            f"{unresolved_examples}"
        )
    local_paths_by_fpga, cross_path_ids, path_identity = writer.finish()
    return LocalPathQueryInputs(
        original_ir=original_ir,
        assignment=assignment,
        database=database,
        routes=routes,
        original_ir_path=original_ir_path,
        local_paths_by_fpga=local_paths_by_fpga,
        cross_path_ids=cross_path_ids,
        source_manifest=_source_manifest(
            database,
            path_database_path,
            original_ir_path,
            assignment_path,
            routes_path,
            path_identity=path_identity,
        ),
    )


def _write_local_path_query(
    original_ir_path: Path,
    assignment_path: Path,
    path_database_path: Path,
    routes_path: Path,
    merged_ir_path: Path,
    fpga: str,
    query_path: Path,
    identity_path: Path,
    *,
    prepared_inputs: LocalPathQueryInputs | None = None,
) -> Dict[str, Any]:
    """Materialize every original same-partition path for one backend."""
    prepared = prepared_inputs or prepare_local_path_query_inputs(
        original_ir_path,
        assignment_path,
        path_database_path,
        routes_path,
    )
    indexed_merged = (
        is_emuir_sqlite(merged_ir_path) and is_compact_emuir(merged_ir_path)
    )
    merged_ir = (
        IndexedPhysicalIR(merged_ir_path)
        if indexed_merged
        else EmuIR.load(merged_ir_path)
    )
    assignment = prepared.assignment
    if merged_ir.value["design"]["name"] != f"{assignment['design']}__{fpga}":
        raise ValidationError("local path merged IR target is invalid")
    merged_instances = (
        merged_ir.instances
        if indexed_merged
        else {
            item["id"]: item for item in merged_ir.value["instances"]
        }
    )
    merged_pins = _instance_pin_inventory(merged_ir)
    local_paths = prepared.local_paths_by_fpga.get(fpga, ())
    def endpoint_pin(endpoint: Mapping[str, Any]) -> str:
        return _xilinx_object(
            merged_ir, endpoint, merged_pins, merged_instances
        )[1]

    identity_provider = "original-timing-pathdb-to-xilinx-endpoint-bound-v1"
    local_path_count = len(local_paths)
    identity_header = {
        "schema": LOCAL_PATH_IDENTITY_SCHEMA_V2,
        "status": "pass",
        "design": assignment["design"],
        "fpga": fpga,
        "provider": identity_provider,
        "source": dict(prepared.source_manifest),
        "coverage": {"local_paths": local_path_count},
    }
    storage_header = local_path_timing_header(
        identity_header,
        provider=identity_provider,
        qualification=LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION,
    )
    measurement_counts = {
        "explicit-routed-path-chain": 0,
        "endpoint-longest-path-fallback": 0,
    }
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    query_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, query_temporary_name = tempfile.mkstemp(
        prefix=f".{query_path.name}.", suffix=".tmp", dir=query_path.parent
    )
    query_temporary = Path(query_temporary_name)
    query_stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="")

    def identity_records() -> Iterator[Mapping[str, Any]]:
        paths = (
            local_paths
            if isinstance(local_paths, _PreparedLocalPathSequence)
            else sorted(local_paths, key=lambda item: item["id"])
        )
        for path in paths:
            start = path["startpoint"]
            end = path["endpoint"]
            record = {
                "id": path["id"],
                "kind": "local",
                "fpga": fpga,
                "clock_domain": path["clock_domain"],
                "clock_period_ns": float(path["clock_period_ns"]),
                "required_time_ns": float(path["fixed_delay_ns"])
                + float(path["slack_ns"]),
                "start_pin": endpoint_pin(start),
                "end_pin": endpoint_pin(end),
            }
            path_pins = []
            measurement = "endpoint-longest-path-fallback"
            record.update({"measurement": measurement, "path_pins": path_pins})
            measurement_counts[measurement] += 1
            query_stream.write(
                "\t".join(
                    (
                        record["id"],
                        "local",
                        record["start_pin"],
                        record["end_pin"],
                        ",".join(path_pins) if path_pins else "-",
                    )
                )
                + "\n"
            )
            yield {**record, "delay_ns": 0.0}

    try:
        query_stream.write(LOCAL_PATH_QUERY_HEADER_V2 + "\n")
        with write_local_path_timing_storage(
            identity_path, storage_header, identity_records()
        ) as identity_store:
            validation = identity_store.validate_timing()
            if (
                validation["local_paths"] != local_path_count
                or validation["maximum_delay_ns"] != 0.0
            ):
                raise ValidationError(
                    "typed local path identity publication disagrees"
                )
        query_stream.flush()
        os.fsync(query_stream.fileno())
        query_stream.close()
        os.replace(query_temporary, query_path)
        directory = os.open(query_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if not query_stream.closed:
            query_stream.close()
        query_temporary.unlink(missing_ok=True)
        if indexed_merged:
            merged_ir.close()
    return {
        "status": "pass",
        "fpga": fpga,
        "local_paths": local_path_count,
        "measurement_counts": measurement_counts,
        "query": str(query_path),
        "identity": str(identity_path),
    }


def write_xilinx_local_path_query(
    original_ir_path: Path,
    assignment_path: Path,
    path_database_path: Path,
    routes_path: Path,
    merged_ir_path: Path,
    fpga: str,
    query_path: Path,
    identity_path: Path,
    *,
    prepared_inputs: LocalPathQueryInputs | None = None,
) -> Dict[str, Any]:
    """Bind every original same-partition path to Xilinx logical pins."""
    return _write_local_path_query(
        original_ir_path,
        assignment_path,
        path_database_path,
        routes_path,
        merged_ir_path,
        fpga,
        query_path,
        identity_path,
        prepared_inputs=prepared_inputs,
    )


def validate_local_path_timing(database: Mapping[str, Any]) -> Dict[str, Any]:
    indexed_validator = getattr(database, "validate_timing", None)
    if callable(indexed_validator):
        return indexed_validator()
    if database.get("schema") != LOCAL_PATH_TIMING_SCHEMA:
        raise ValidationError("local path timing schema is invalid")
    paths = database.get("paths")
    if not isinstance(paths, Sequence) or isinstance(
        paths, (str, bytes, bytearray)
    ):
        raise ValidationError("local path timing paths are invalid")
    identity = {
        key: database[key]
        for key in (
            "status", "design", "fpga", "source", "coverage"
        )
    }
    identity.update(
        {
            "schema": database.get(
                "identity_schema", LOCAL_PATH_IDENTITY_SCHEMA
            ),
            "provider": "local-path-timing-validation",
            "paths": _WithoutDelayPaths(paths),
        }
    )
    checked = validate_local_path_identity(identity)
    maximum = 0.0
    measurement_counts = {
        "explicit-routed-path-chain": 0,
        "endpoint-longest-path-fallback": 0,
        OPTIMIZED_AWAY: 0,
    }
    for item in paths:
        if not isinstance(item, Mapping):
            raise ValidationError("local path timing record is invalid")
        if validate_optimization_record(item):
            measurement_counts[OPTIMIZED_AWAY] += 1
            continue
        delay = item.get("delay_ns")
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(float(delay))
            or float(delay) < 0.0
        ):
            raise ValidationError("local path timing delay is invalid")
        maximum = max(maximum, float(delay))
        if database.get("identity_schema") == LOCAL_PATH_IDENTITY_SCHEMA_V2:
            measurement = item.get("measurement")
            if measurement in measurement_counts:
                measurement_counts[measurement] += 1
    if database.get("identity_schema") != LOCAL_PATH_IDENTITY_SCHEMA_V2:
        measurement_counts = {}
    return {
        **checked,
        "maximum_delay_ns": maximum,
        "measurement_counts": measurement_counts,
        "optimized_away_paths": measurement_counts.get(OPTIMIZED_AWAY, 0),
        "implemented_paths": len(paths)-measurement_counts.get(OPTIMIZED_AWAY, 0),
    }
