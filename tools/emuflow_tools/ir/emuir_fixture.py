"""Independent EmuIR fixtures for storage and handoff qualification.

The complete flow writes EmuIR through the native frontend. These writers build
controlled current and legacy test inputs; they are not installed in its runtime.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable, Mapping

from emuflow.common.paths import DATA_ROOT
from emuflow.ir.emuir_storage import (
    EMUIR_SQLITE_APPLICATION_ID, EMUIR_SQLITE_USER_VERSION,
    EMUIR_STORAGE_SCHEMA, EMUIR_COMPACT_STORAGE_SCHEMA, _json,
)
from emuflow.storage.store_publication import publish_sqlite_store, validate_store_destination

DDL = (DATA_ROOT / "schema/emuir_compact.sql").read_text(encoding="utf-8")


def _extras(record: Mapping[str, Any], owned: Iterable[str]) -> str:
    excluded = set(owned)
    return _json({key: value for key, value in record.items() if key not in excluded})



class _Intern:
    """Bounded hot dictionary; SQLite owns the full vocabulary."""
    def __init__(self, connection, table, column):
        self.connection, self.table, self.column = connection, table, column
        self.cache = OrderedDict()
        self.cache_bytes = 0

    def __call__(self, value):
        if value in self.cache:
            self.cache.move_to_end(value)
            return self.cache[value]
        row = self.connection.execute(
            f"SELECT seq FROM {self.table} WHERE {self.column}=?", (value,)
        ).fetchone()
        if row is None:
            row_id = self.connection.execute(
                f"INSERT INTO {self.table}({self.column}) VALUES (?)", (value,)
            ).lastrowid
        else:
            row_id = row[0]
        self.cache[value] = row_id
        self.cache_bytes += sys.getsizeof(value) + 128
        while len(self.cache) > 8192 or self.cache_bytes > 32 * 1024 * 1024:
            removed, _ = self.cache.popitem(last=False)
            self.cache_bytes -= sys.getsizeof(removed) + 128
        return row_id



def write_compact_emuir(
    path: Path, value: Mapping[str, Any], *, source_bindings=(), _finalize=None,
) -> None:
    source_bindings = validate_store_destination(path, source_bindings)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    c = sqlite3.connect(temporary)
    try:
        c.executescript(f"""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE;
        PRAGMA cache_size=-65536; PRAGMA application_id={EMUIR_SQLITE_APPLICATION_ID};
        PRAGMA user_version=2;
        """ + DDL)
        payload = _Intern(c, "payloads", "value")
        pin = _Intern(c, "pin_names", "name")
        metadata = {
            "storage_schema": EMUIR_COMPACT_STORAGE_SCHEMA,
            "logical_schema": value.get("schema"),
            "design_json": _json(value.get("design")),
            "warnings_json": _json(value.get("warnings", [])),
        }
        c.executemany("INSERT INTO metadata VALUES (?,?)", sorted(metadata.items()))
        counts = {"cell_records": 0, "net_records": 0, "endpoint_records": 0}
        for seq, record in enumerate(value.get("ports", [])):
            c.execute("INSERT INTO port_records VALUES (?,?)", (seq, _json({
                **record, "constant_connections": record.get("constant_connections", [])
            })))
        for seq, record in enumerate(value.get("instances", [])):
            counts["cell_records"] += 1
            same_name = record.get("name") == record["id"]
            # Pool complete per-cell facts, including explicit zero resource
            # keys and absent-vs-empty fields. Never infer them from cell type.
            c.execute("INSERT INTO cell_records VALUES (?,?,?,?,?,?,?,?,?,?)", (
                seq, record["id"], None if same_name else record.get("name"),
                int(same_name), record["type"], payload(_json(record["resources"])),
                payload(_json(record["parameters"])) if "parameters" in record else None,
                payload(_json(record["attributes"])) if "attributes" in record else None,
                payload(_json(record.get("constant_connections", []))),
                payload(_extras(record, {"id", "name", "type", "resources", "parameters", "attributes", "constant_connections"})),
            ))
        owners = OrderedDict()
        for seq, record in enumerate(value.get("nets", [])):
            counts["net_records"] += 1
            same_name = record.get("name") == record["id"]
            c.execute("INSERT INTO net_records VALUES (?,?,?,?,?,?,?,?,?)", (
                seq, record["id"], None if same_name else record.get("name"), int(same_name),
                record.get("yosys_bit"), record.get("bus_index"), record.get("fanout"),
                record["cut_class"], payload(_extras(record, {
                    "id", "name", "yosys_bit", "bus_index", "fanout", "cut_class",
                    "aliases", "drivers", "sinks",
                })),
            ))
            c.executemany("INSERT INTO alias_records VALUES (?,?,?)", (
                (seq, ordinal, name) for ordinal, name in enumerate(record.get("aliases", []))
            ))
            for role, collection in enumerate(("drivers", "sinks")):
                for ordinal, endpoint in enumerate(record[collection]):
                    counts["endpoint_records"] += 1
                    identity = endpoint.get("instance")
                    owner = None
                    if identity is not None:
                        owner = owners.get(identity)
                        if owner is None:
                            row = c.execute("SELECT seq FROM cell_records WHERE id=?", (identity,)).fetchone()
                            if row is None:
                                raise ValueError(f"EmuIR endpoint refers to absent instance {identity!r}")
                            owner = row[0]
                            owners[identity] = owner
                            if len(owners) > 8192:
                                owners.popitem(last=False)
                    c.execute("INSERT INTO endpoint_records VALUES (?,?,?,?,?,?,?)", (
                        seq, role, ordinal, owner, pin(endpoint["port"]), endpoint["bit"],
                        payload(_extras(endpoint, {"instance", "port", "bit"})),
                    ))
        c.executemany("INSERT INTO clocks VALUES (?,?)", (
            (seq, _json(record)) for seq, record in enumerate(value.get("clocks", []))
        ))
        c.execute("INSERT INTO metadata VALUES (?,?)", ("record_counts_json", _json(counts)))
        # Normalization and transport buffering belong to the private generation.
        # The shared writer owns commit, connection lifetime and publication.
        if _finalize is not None:
            _finalize(c)
        # Build the required reverse adjacency once from compact integer rows,
        # rather than randomly updating its B-tree on every pin insertion.
        c.execute("CREATE INDEX endpoint_owner ON endpoint_records(instance_seq,port_seq,bit)")
        c.commit()
        c.close()
        publish_sqlite_store(
            temporary, path, "emuir-compact-v2", sources=source_bindings
        )
    except BaseException:
        c.close()
        temporary.unlink(missing_ok=True)
        raise



def write_emuir_fixture(path: Path, value: Mapping[str, Any]) -> None:
    """Write a logical test fixture using the indexed artifact filename contract."""
    if not path.name.endswith(".emuir.sqlite"):
        raise ValueError("new EmuIR artifacts must use the indexed .emuir.sqlite format")
    write_compact_emuir(path, value)



def write_legacy_emuir_fixture(path: Path, value: Mapping[str, Any]) -> None:
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
