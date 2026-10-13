"""Compact EmuIR: integer endpoint owners and shared per-cell payloads.

The compatibility views are read interfaces, not duplicated stored tables.
The explicit v1 reader remains available for historical immutable artifacts.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterator, Mapping

from .emuir_storage import (
    EMUIR_SQLITE_APPLICATION_ID, EMUIR_COMPACT_STORAGE_SCHEMA,
    _json, _extras, _decode, _RowsForOwner, open_emuir_sqlite_read_only,
)


DDL = """
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE payloads(seq INTEGER PRIMARY KEY, value TEXT NOT NULL UNIQUE);
CREATE TABLE cell_records(
 seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, name TEXT, flags INTEGER NOT NULL,
 type TEXT NOT NULL, resources_seq INTEGER NOT NULL, parameters_seq INTEGER,
 attributes_seq INTEGER, constants_seq INTEGER NOT NULL, extras_seq INTEGER NOT NULL);
CREATE TABLE net_records(
 seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, name TEXT, flags INTEGER NOT NULL,
 yosys_bit INTEGER, bus_index INTEGER, fanout INTEGER, cut_class TEXT NOT NULL,
 extras_seq INTEGER NOT NULL);
CREATE TABLE pin_names(seq INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE endpoint_records(
 net_seq INTEGER NOT NULL, role INTEGER NOT NULL, ordinal INTEGER NOT NULL,
 instance_seq INTEGER, port_seq INTEGER NOT NULL, bit INTEGER NOT NULL,
 extras_seq INTEGER NOT NULL, PRIMARY KEY(net_seq,role,ordinal)) WITHOUT ROWID;
CREATE INDEX endpoint_owner ON endpoint_records(instance_seq,port_seq,bit);
CREATE TABLE alias_records(net_seq INTEGER NOT NULL, ordinal INTEGER NOT NULL,
 alias TEXT NOT NULL, PRIMARY KEY(net_seq,ordinal)) WITHOUT ROWID;
CREATE TABLE port_records(seq INTEGER PRIMARY KEY, record_json TEXT NOT NULL);
CREATE TABLE clocks(seq INTEGER PRIMARY KEY, record_json TEXT NOT NULL);
CREATE VIEW instances AS
 SELECT c.seq,c.id,CASE WHEN flags & 1 THEN c.id ELSE c.name END AS name,c.type,
 p.value AS parameters_json,a.value AS attributes_json,x.value AS extras_json
 FROM cell_records c LEFT JOIN payloads p ON p.seq=c.parameters_seq
 LEFT JOIN payloads a ON a.seq=c.attributes_seq JOIN payloads x ON x.seq=c.extras_seq;
CREATE VIEW instance_resources AS
 SELECT c.seq AS instance_seq,r.key AS resource,r.value AS amount
 FROM cell_records c JOIN payloads p ON p.seq=c.resources_seq,
 json_each(p.value) r;
CREATE VIEW nets AS SELECT n.seq,n.id,CASE WHEN n.flags & 1 THEN n.id ELSE n.name END AS name,
 n.yosys_bit,n.bus_index,n.fanout,n.cut_class,p.value AS extras_json
 FROM net_records n LEFT JOIN payloads p ON p.seq=n.extras_seq;
CREATE VIEW endpoints AS
 SELECT e.net_seq,e.role,e.ordinal,c.id AS instance_id,n.name AS port,e.bit,e.instance_seq,
 p.value AS extras_json FROM endpoint_records e
 LEFT JOIN cell_records c ON c.seq=e.instance_seq
 LEFT JOIN pin_names n ON n.seq=e.port_seq LEFT JOIN payloads p ON p.seq=e.extras_seq;
CREATE VIEW net_aliases AS SELECT * FROM alias_records;
"""


class _Intern:
    """Bounded hot dictionary; SQLite owns the full vocabulary."""
    def __init__(self, connection, table, column):
        self.connection, self.table, self.column = connection, table, column
        self.cache = OrderedDict()

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
        if len(self.cache) > 8192:
            self.cache.popitem(last=False)
        return row_id


def write_compact_emuir(path: Path, value: Mapping[str, Any]) -> None:
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
        c.commit()
        c.close()
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        c.close()
        temporary.unlink(missing_ok=True)
        raise


def is_compact_emuir(path: Path) -> bool:
    c = open_emuir_sqlite_read_only(path)
    try:
        return c.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        c.close()


def compact_metadata(c) -> dict:
    metadata = dict(c.execute("SELECT key,value FROM metadata"))
    return {"schema": metadata["logical_schema"],
            "design": _decode(metadata["design_json"], "design metadata"),
            "warnings": _decode(metadata["warnings_json"], "warnings metadata"),
            "ports": [_decode(row[0], "port") for row in c.execute("SELECT record_json FROM port_records ORDER BY seq")],
            "clocks": [_decode(row[0], "clock") for row in c.execute("SELECT record_json FROM clocks ORDER BY seq")]}


def compact_instances(c) -> Iterator[dict]:
    for row in c.execute("""SELECT c.*,r.value AS resources_json,p.value AS parameters_json,
        a.value AS attributes_json,k.value AS constants_json,x.value AS extras_json
        FROM cell_records c LEFT JOIN payloads r ON r.seq=c.resources_seq
        LEFT JOIN payloads p ON p.seq=c.parameters_seq LEFT JOIN payloads a ON a.seq=c.attributes_seq
        LEFT JOIN payloads k ON k.seq=c.constants_seq LEFT JOIN payloads x ON x.seq=c.extras_seq ORDER BY c.seq"""):
        facts = {"resources": _decode(row["resources_json"], "resources"),
                 "constant_connections": _decode(row["constants_json"], "constants"),
                 **_decode(row["extras_json"], "instance extras")}
        for key in ("parameters", "attributes"):
            if row[key + "_json"] is not None:
                facts[key] = _decode(row[key + "_json"], key)
        record = {"id": row["id"], "type": row["type"], **facts}
        if row["flags"] & 1:
            record["name"] = row["id"]
        elif row["name"] is not None:
            record["name"] = row["name"]
        yield record


def compact_nets(c) -> Iterator[dict]:
    endpoints = _RowsForOwner(c.execute("SELECT * FROM endpoints ORDER BY net_seq,role,ordinal"), "net_seq")
    aliases = _RowsForOwner(c.execute("SELECT * FROM net_aliases ORDER BY net_seq,ordinal"), "net_seq")
    for row in c.execute("SELECT * FROM nets ORDER BY seq"):
        record = {"id": row["id"], "cut_class": row["cut_class"],
                  "drivers": [], "sinks": [],
                  "aliases": [r["alias"] for r in aliases.take(row["seq"])],
                  **_decode(row["extras_json"], "net extras")}
        for endpoint in endpoints.take(row["seq"]):
            if endpoint["role"] not in (0, 1):
                raise ValueError("EmuIR SQLite invalid endpoint role")
            if "instance_seq" in endpoint.keys() and endpoint["instance_seq"] is not None and endpoint["instance_id"] is None:
                raise ValueError("EmuIR SQLite orphaned endpoint instance")
            record[("drivers", "sinks")[endpoint["role"]]].append({
                "instance": endpoint["instance_id"], "port": endpoint["port"], "bit": endpoint["bit"],
                **_decode(endpoint["extras_json"], "endpoint extras"),
            })
        for field in ("name", "yosys_bit", "bus_index", "fanout"):
            if row[field] is not None:
                record[field] = row[field]
        yield record
    endpoints.finish()
    aliases.finish()


def read_compact_emuir(path: Path) -> dict:
    c = open_emuir_sqlite_read_only(path)
    try:
        return {**compact_metadata(c), "instances": list(compact_instances(c)), "nets": list(compact_nets(c))}
    finally:
        c.close()


def source_metadata(c) -> dict:
    if c.execute("PRAGMA user_version").fetchone()[0] == 2:
        return compact_metadata(c)
    metadata = dict(c.execute("SELECT key,value FROM metadata"))
    constants = _RowsForOwner(c.execute("SELECT * FROM port_constants ORDER BY port_seq,ordinal"), "port_seq")
    ports = []
    for row in c.execute("SELECT * FROM ports ORDER BY seq"):
        record = {"id": row["id"], "direction": row["direction"], "width": row["width"],
                  "constant_connections": [{"bit": r["bit"], "value": r["value"],
                      **_decode(r["extras_json"], "port constant")} for r in constants.take(row["seq"])],
                  **_decode(row["extras_json"], "port extras")}
        if row["name"] is not None:
            record["name"] = row["name"]
        for key in ("clock", "reset"):
            if row[key] is not None:
                record[key] = bool(row[key])
        ports.append(record)
    constants.finish()
    return {"schema": metadata["logical_schema"],
            "design": _decode(metadata["design_json"], "design metadata"),
            "warnings": _decode(metadata["warnings_json"], "warnings metadata"),
            "ports": ports,
            "clocks": [_decode(r[0], "clock") for r in c.execute("SELECT record_json FROM clocks ORDER BY seq")]}


def source_instances(c) -> Iterator[dict]:
    if c.execute("PRAGMA user_version").fetchone()[0] == 2:
        yield from compact_instances(c)
        return
    resources = _RowsForOwner(c.execute("SELECT * FROM instance_resources ORDER BY instance_seq,resource"), "instance_seq")
    constants = _RowsForOwner(c.execute("SELECT * FROM instance_constants ORDER BY instance_seq,ordinal"), "instance_seq")
    for row in c.execute("SELECT * FROM instances ORDER BY seq"):
        record = {"id": row["id"], "type": row["type"],
                  "resources": {r["resource"]: r["amount"] for r in resources.take(row["seq"])},
                  "constant_connections": [{"port": r["port"], "bit": r["bit"], "value": r["value"],
                      **_decode(r["extras_json"], "instance constant")} for r in constants.take(row["seq"])],
                  **_decode(row["extras_json"], "instance extras")}
        if row["name"] is not None:
            record["name"] = row["name"]
        for key in ("parameters", "attributes"):
            if row[key + "_json"] is not None:
                record[key] = _decode(row[key + "_json"], key)
        yield record
    resources.finish()
    constants.finish()


def repack_emuir(source: Path, output: Path) -> None:
    """Convert an existing mapped graph without synthesis or whole-graph load."""
    if source.resolve() == output.resolve():
        raise ValueError("repack output must differ from read-only source")
    c = open_emuir_sqlite_read_only(source)
    try:
        write_compact_emuir(output, {**source_metadata(c),
            "instances": source_instances(c), "nets": compact_nets(c)})
    finally:
        c.close()
