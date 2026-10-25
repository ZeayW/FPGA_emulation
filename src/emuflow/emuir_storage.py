"""Indexed on-disk storage for large EmuIR graphs.

The logical EmuIR schema remains ``emuflow.emuir/v1``.  This module changes
only its physical representation: repeated instance/net/endpoint records are
stored as normalized SQLite rows instead of one monolithic JSON document.
Small, irregular dictionaries (cell parameters and attributes) remain
canonical JSON *row values*; no consumer has to parse the complete graph to
reach one record or one summary.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Mapping


EMUIR_STORAGE_SCHEMA = "emuflow.emuir-sqlite/v1"
EMUIR_COMPACT_STORAGE_SCHEMA = "emuflow.emuir-sqlite/v2"
EMUIR_STORAGE_SUFFIX = ".emuir.sqlite"
EMUIR_SQLITE_APPLICATION_ID = 0x454D5549  # ASCII "EMUI"
EMUIR_SQLITE_USER_VERSION = 1


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode(value: str, context: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"{context}: invalid canonical JSON row") from error


def _extras(record: Mapping[str, Any], owned: Iterable[str]) -> str:
    excluded = set(owned)
    return _json({key: value for key, value in record.items() if key not in excluded})


def open_emuir_sqlite_read_only(path: Path) -> sqlite3.Connection:
    """Open and validate a sealed indexed EmuIR artifact without journals."""

    if not is_emuir_sqlite(path):
        raise ValueError(f"{path}: not an EmuIR SQLite artifact")
    # ``immutable=1`` prevents journal creation beside a sealed artifact and
    # is safe because EmuFlow never mutates a published EmuIR store in place.
    connection = sqlite3.connect(
        f"file:{path.resolve()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if (
            application_id != EMUIR_SQLITE_APPLICATION_ID
            or version not in {EMUIR_SQLITE_USER_VERSION, 2}
        ):
            raise ValueError(f"{path}: unsupported EmuIR SQLite header")
        storage_schema = connection.execute(
            "SELECT value FROM metadata WHERE key = 'storage_schema'"
        ).fetchone()
        if storage_schema is None or storage_schema[0] not in {EMUIR_STORAGE_SCHEMA, EMUIR_COMPACT_STORAGE_SCHEMA}:
            raise ValueError(f"{path}: unsupported EmuIR storage schema")
        if (version == 2) != (storage_schema[0] == EMUIR_COMPACT_STORAGE_SCHEMA):
            raise ValueError(f"{path}: inconsistent EmuIR storage version")
        return connection
    except BaseException:
        connection.close()
        raise


def is_emuir_sqlite(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open("rb") as stream:
            return stream.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def write_emuir_sqlite(
    path: Path, value: Mapping[str, Any], *, source_bindings=(), _finalize=None
) -> None:
    """Write the current compact store; preserve the logical v1 graph schema."""
    from .emuir_compact import write_compact_emuir
    write_compact_emuir(
        path, value, source_bindings=source_bindings, _finalize=_finalize
    )


def _write_emuir_sqlite_v1(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically write one logical EmuIR document as normalized rows."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temporary)
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        # Large graphs must not turn SQLite sorting/index scratch into another
        # multi-GiB RSS spike.  The artifact itself remains one transaction;
        # temporary B-trees use disk-backed scratch.
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute("PRAGMA page_size=4096")
        connection.execute(f"PRAGMA application_id={EMUIR_SQLITE_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={EMUIR_SQLITE_USER_VERSION}")
        connection.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE ports (
                seq INTEGER PRIMARY KEY,
                id TEXT NOT NULL UNIQUE,
                name TEXT,
                direction TEXT NOT NULL,
                width INTEGER NOT NULL,
                clock INTEGER,
                reset INTEGER,
                extras_json TEXT NOT NULL
            );
            CREATE TABLE port_constants (
                port_seq INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                bit INTEGER NOT NULL,
                value TEXT NOT NULL,
                extras_json TEXT NOT NULL,
                PRIMARY KEY (port_seq, ordinal)
            ) WITHOUT ROWID;
            CREATE TABLE instances (
                seq INTEGER PRIMARY KEY,
                id TEXT NOT NULL UNIQUE,
                name TEXT,
                type TEXT NOT NULL,
                parameters_json TEXT,
                attributes_json TEXT,
                extras_json TEXT NOT NULL
            );
            CREATE TABLE instance_resources (
                instance_seq INTEGER NOT NULL,
                resource TEXT NOT NULL,
                amount INTEGER NOT NULL,
                PRIMARY KEY (instance_seq, resource)
            ) WITHOUT ROWID;
            CREATE TABLE instance_constants (
                instance_seq INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                port TEXT NOT NULL,
                bit INTEGER NOT NULL,
                value TEXT NOT NULL,
                extras_json TEXT NOT NULL,
                PRIMARY KEY (instance_seq, ordinal)
            ) WITHOUT ROWID;
            CREATE TABLE nets (
                seq INTEGER PRIMARY KEY,
                id TEXT NOT NULL UNIQUE,
                name TEXT,
                yosys_bit INTEGER,
                bus_index INTEGER,
                fanout INTEGER,
                cut_class TEXT NOT NULL,
                extras_json TEXT NOT NULL
            );
            CREATE TABLE net_aliases (
                net_seq INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                alias TEXT NOT NULL,
                PRIMARY KEY (net_seq, ordinal)
            ) WITHOUT ROWID;
            CREATE TABLE endpoints (
                net_seq INTEGER NOT NULL,
                role INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                instance_id TEXT,
                port TEXT NOT NULL,
                bit INTEGER NOT NULL,
                extras_json TEXT NOT NULL,
                PRIMARY KEY (net_seq, role, ordinal)
            ) WITHOUT ROWID;
            CREATE INDEX endpoints_instance ON endpoints(instance_id);
            CREATE TABLE clocks (
                seq INTEGER PRIMARY KEY,
                record_json TEXT NOT NULL
            );
            """
        )
        metadata = {
            "storage_schema": EMUIR_STORAGE_SCHEMA,
            "logical_schema": value.get("schema"),
            "design_json": _json(value.get("design")),
            "warnings_json": _json(value.get("warnings", [])),
        }
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            sorted(metadata.items()),
        )

        ports = value.get("ports", [])
        for seq, record in enumerate(ports):
            constants = record.get("constant_connections", [])
            connection.execute(
                "INSERT INTO ports VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    seq,
                    record["id"],
                    record.get("name"),
                    record["direction"],
                    record["width"],
                    int(record["clock"]) if "clock" in record else None,
                    int(record["reset"]) if "reset" in record else None,
                    _extras(
                        record,
                        {
                            "id", "name", "direction", "width", "clock",
                            "reset", "constant_connections",
                        },
                    ),
                ),
            )
            for ordinal, constant in enumerate(constants):
                connection.execute(
                    "INSERT INTO port_constants VALUES (?, ?, ?, ?, ?)",
                    (
                        seq,
                        ordinal,
                        constant["bit"],
                        constant["value"],
                        _extras(constant, {"bit", "value"}),
                    ),
                )

        instances = value.get("instances", [])
        for seq, record in enumerate(instances):
            constants = record.get("constant_connections", [])
            connection.execute(
                "INSERT INTO instances VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    seq,
                    record["id"],
                    record.get("name"),
                    record["type"],
                    _json(record["parameters"]) if "parameters" in record else None,
                    _json(record["attributes"]) if "attributes" in record else None,
                    _extras(
                        record,
                        {
                            "id", "name", "type", "resources", "parameters",
                            "attributes", "constant_connections",
                        },
                    ),
                ),
            )
            connection.executemany(
                "INSERT INTO instance_resources VALUES (?, ?, ?)",
                (
                    (seq, resource, amount)
                    for resource, amount in sorted(record["resources"].items())
                ),
            )
            for ordinal, constant in enumerate(constants):
                connection.execute(
                    "INSERT INTO instance_constants VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        seq,
                        ordinal,
                        constant["port"],
                        constant["bit"],
                        constant["value"],
                        _extras(constant, {"port", "bit", "value"}),
                    ),
                )

        nets = value.get("nets", [])
        for seq, record in enumerate(nets):
            aliases = record.get("aliases", [])
            connection.execute(
                "INSERT INTO nets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    seq,
                    record["id"],
                    record.get("name"),
                    record.get("yosys_bit"),
                    record.get("bus_index"),
                    record.get("fanout"),
                    record["cut_class"],
                    _extras(
                        record,
                        {
                            "id", "name", "yosys_bit", "bus_index", "aliases",
                            "drivers", "sinks", "fanout", "cut_class",
                        },
                    ),
                ),
            )
            connection.executemany(
                "INSERT INTO net_aliases VALUES (?, ?, ?)",
                ((seq, ordinal, alias) for ordinal, alias in enumerate(aliases)),
            )
            for role, collection in enumerate(("drivers", "sinks")):
                for ordinal, endpoint in enumerate(record[collection]):
                    connection.execute(
                        "INSERT INTO endpoints VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            seq,
                            role,
                            ordinal,
                            endpoint.get("instance"),
                            endpoint["port"],
                            endpoint["bit"],
                            _extras(endpoint, {"instance", "port", "bit"}),
                        ),
                    )

        connection.executemany(
            "INSERT INTO clocks VALUES (?, ?)",
            ((seq, _json(record)) for seq, record in enumerate(value.get("clocks", []))),
        )
        connection.commit()
        # SQLite enforces keys while inserting. Do not reread every table and
        # index here: storage-engine audits are explicit offline diagnostics,
        # not a prerequisite for handing a newly written graph to a consumer.
        connection.close()
        connection = None
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)
        raise


class _RowsForOwner:
    """Merge ordered child rows with their owner using one-row lookahead."""

    def __init__(self, rows: Iterable[sqlite3.Row], owner_column: str):
        self._rows = iter(rows)
        self._owner_column = owner_column
        self._next = next(self._rows, None)

    def take(self, owner: int) -> Iterator[sqlite3.Row]:
        if self._next is not None and self._next[self._owner_column] < owner:
            raise ValueError(f"EmuIR SQLite orphaned {self._owner_column} row")
        while self._next is not None and self._next[self._owner_column] == owner:
            row = self._next
            self._next = next(self._rows, None)
            yield row

    def finish(self) -> None:
        if self._next is not None:
            raise ValueError(f"EmuIR SQLite orphaned {self._owner_column} row")


def read_emuir_sqlite(path: Path) -> Dict[str, Any]:
    """Read an indexed EmuIR store into the existing logical Python schema."""

    from .emuir_compact import read_compact_emuir, is_compact_emuir
    if is_compact_emuir(path):
        return read_compact_emuir(path)

    connection = open_emuir_sqlite_read_only(path)
    try:
        metadata = {
            row["key"]: row["value"]
            for row in connection.execute("SELECT key, value FROM metadata")
        }

        port_constants = _RowsForOwner(connection.execute(
            "SELECT * FROM port_constants ORDER BY port_seq, ordinal"
        ), "port_seq")
        ports = []
        for row in connection.execute("SELECT * FROM ports ORDER BY seq"):
            record = {
                "id": row["id"],
                "direction": row["direction"],
                "width": row["width"],
                "constant_connections": [
                    {
                        "bit": child["bit"], "value": child["value"],
                        **_decode(child["extras_json"], "port constant extras"),
                    }
                    for child in port_constants.take(row["seq"])
                ],
                **_decode(row["extras_json"], "port extras"),
            }
            if row["name"] is not None:
                record["name"] = row["name"]
            if row["clock"] is not None:
                record["clock"] = bool(row["clock"])
            if row["reset"] is not None:
                record["reset"] = bool(row["reset"])
            ports.append(record)
        port_constants.finish()

        resources = _RowsForOwner(connection.execute(
            "SELECT * FROM instance_resources ORDER BY instance_seq, resource"
        ), "instance_seq")
        constants = _RowsForOwner(connection.execute(
            "SELECT * FROM instance_constants ORDER BY instance_seq, ordinal"
        ), "instance_seq")
        instances = []
        for row in connection.execute("SELECT * FROM instances ORDER BY seq"):
            record = {
                "id": row["id"],
                "type": row["type"],
                "resources": {
                    child["resource"]: child["amount"]
                    for child in resources.take(row["seq"])
                },
                "constant_connections": [
                    {
                        "port": child["port"], "bit": child["bit"],
                        "value": child["value"],
                        **_decode(child["extras_json"], "instance constant extras"),
                    }
                    for child in constants.take(row["seq"])
                ],
                **_decode(row["extras_json"], "instance extras"),
            }
            if row["name"] is not None:
                record["name"] = row["name"]
            if row["parameters_json"] is not None:
                record["parameters"] = _decode(
                    row["parameters_json"], "instance parameters"
                )
            if row["attributes_json"] is not None:
                record["attributes"] = _decode(
                    row["attributes_json"], "instance attributes"
                )
            instances.append(record)
        resources.finish()
        constants.finish()

        aliases = _RowsForOwner(connection.execute(
            "SELECT * FROM net_aliases ORDER BY net_seq, ordinal"
        ), "net_seq")
        endpoints = _RowsForOwner(connection.execute(
            "SELECT * FROM endpoints ORDER BY net_seq, role, ordinal"
        ), "net_seq")
        nets = []
        for row in connection.execute("SELECT * FROM nets ORDER BY seq"):
            drivers: list[Dict[str, Any]] = []
            sinks: list[Dict[str, Any]] = []
            for child in endpoints.take(row["seq"]):
                if child["role"] not in (0, 1):
                    raise ValueError("EmuIR SQLite invalid endpoint role")
                target = drivers if child["role"] == 0 else sinks
                target.append({
                    "instance": child["instance_id"],
                    "port": child["port"], "bit": child["bit"],
                    **_decode(child["extras_json"], "endpoint extras"),
                })
            record = {
                "id": row["id"],
                "cut_class": row["cut_class"],
                "aliases": [child["alias"] for child in aliases.take(row["seq"])],
                "drivers": drivers,
                "sinks": sinks,
                **_decode(row["extras_json"], "net extras"),
            }
            for field in ("name", "yosys_bit", "bus_index", "fanout"):
                if row[field] is not None:
                    record[field] = row[field]
            nets.append(record)
        aliases.finish()
        endpoints.finish()

        clocks = [
            _decode(row["record_json"], "clock record")
            for row in connection.execute("SELECT record_json FROM clocks ORDER BY seq")
        ]
        return {
            "schema": metadata.get("logical_schema"),
            "design": _decode(metadata["design_json"], "design metadata"),
            "ports": ports,
            "instances": instances,
            "nets": nets,
            "clocks": clocks,
            "warnings": _decode(metadata["warnings_json"], "warnings metadata"),
        }
    finally:
        connection.close()
