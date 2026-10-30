"""Compact EmuIR: integer endpoint owners and shared per-cell payloads.

The compatibility views are read interfaces, not duplicated stored tables.
The explicit v1 reader remains available for historical immutable artifacts.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from emuflow.ir.emuir_storage import EMUIR_SQLITE_APPLICATION_ID, EMUIR_COMPACT_STORAGE_SCHEMA, _decode, _RowsForOwner, open_emuir_sqlite_read_only


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


def selected_net_instance_endpoints(
    c: sqlite3.Connection,
    net_ids: Iterable[str],
) -> dict[str, dict[str, list[str]]]:
    """Read only instance owners for selected nets from compact EmuIR.

    Placement-aware planning and lookahead need the endpoints of scheduled
    nets, not a reconstructed copy of every instance, payload, and net in the
    design.  Keep the query bounded so callers can use it for thousands of
    schedule entries without hitting SQLite's parameter limit.
    """

    requested = sorted(set(net_ids))
    result: dict[str, dict[str, list[str]]] = {}
    for offset in range(0, len(requested), 500):
        batch = requested[offset : offset + 500]
        placeholders = ",".join("?" for _ in batch)
        rows = c.execute(
            f"""SELECT n.id,e.role,e.ordinal,cell.id AS instance_id
            FROM net_records n
            LEFT JOIN endpoint_records e ON e.net_seq=n.seq
            LEFT JOIN cell_records cell ON cell.seq=e.instance_seq
            WHERE n.id IN ({placeholders})
            ORDER BY n.id,e.role,e.ordinal""",
            batch,
        )
        for row in rows:
            record = result.setdefault(
                row["id"], {"drivers": [], "sinks": []}
            )
            if row["role"] is None or row["instance_id"] is None:
                continue
            if row["role"] not in (0, 1):
                raise ValueError("compact EmuIR endpoint has invalid role")
            record[("drivers", "sinks")[row["role"]]].append(
                row["instance_id"]
            )
    missing = sorted(set(requested) - set(result))
    if missing:
        raise ValueError(
            "compact EmuIR is missing requested nets: "
            + ", ".join(repr(item) for item in missing[:10])
        )
    return result


def selected_net_records(
    c: sqlite3.Connection,
    net_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Decode complete logical records only for an explicit net subset."""

    requested = sorted(set(net_ids))
    result: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(requested), 500):
        batch = requested[offset : offset + 500]
        placeholders = ",".join("?" for _ in batch)
        for row in c.execute(
            f"SELECT * FROM nets WHERE id IN ({placeholders}) ORDER BY seq",
            batch,
        ):
            record = {
                "id": row["id"],
                "cut_class": row["cut_class"],
                "drivers": [],
                "sinks": [],
                **_decode(row["extras_json"], "net extras"),
            }
            for field in ("name", "yosys_bit", "bus_index", "fanout"):
                if row[field] is not None:
                    record[field] = row[field]
            result[row["id"]] = record
        for row in c.execute(
            f"""SELECT n.id,e.role,e.ordinal,cell.id AS instance_id,
            pin.name AS port,e.bit,payload.value AS extras_json
            FROM net_records n JOIN endpoint_records e ON e.net_seq=n.seq
            LEFT JOIN cell_records cell ON cell.seq=e.instance_seq
            JOIN pin_names pin ON pin.seq=e.port_seq
            JOIN payloads payload ON payload.seq=e.extras_seq
            WHERE n.id IN ({placeholders})
            ORDER BY n.id,e.role,e.ordinal""",
            batch,
        ):
            if row["role"] not in (0, 1):
                raise ValueError("compact EmuIR endpoint has invalid role")
            result[row["id"]][("drivers", "sinks")[row["role"]]].append(
                {
                    "instance": row["instance_id"],
                    "port": row["port"],
                    "bit": row["bit"],
                    **_decode(row["extras_json"], "endpoint extras"),
                }
            )
    missing = sorted(set(requested) - set(result))
    if missing:
        raise ValueError(
            "compact EmuIR is missing requested nets: "
            + ", ".join(repr(item) for item in missing[:10])
        )
    return result


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
