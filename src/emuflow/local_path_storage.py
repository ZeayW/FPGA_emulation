"""Normalized, streaming owner for source-bound physical local timing rows.

The JSON file is a bounded header, not a second copy of the path population.
Connections are iterator-local so parallel FPGA consumers share immutable
bytes without sharing cursors or retaining a complete Python circuit.
"""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
import sqlite3
import uuid
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Iterator, Mapping

from .errors import ValidationError

STORAGE_SCHEMA = "emuflow.local-path-storage/v1"
IDENTITY_V1 = "emuflow.local-path-identity/v1"
IDENTITY_V2 = "emuflow.local-path-identity/v2"
TIMING = "emuflow.local-path-timing/v1"


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA cache_size=-8192")
    return connection


class LocalPathRows(Sequence):
    """Re-iterable typed rows; no per-path JSON records or full-list cache."""

    def __init__(self, path: Path, header: Mapping[str, Any]):
        self.path = path
        self.header = dict(header)
        self.metadata = dict(header["metadata"])
        self.count = int(header["storage"]["records"])
        self.timing = self.metadata["schema"] == TIMING
        self.v2 = self.metadata.get("identity_schema", self.metadata["schema"]) == IDENTITY_V2
        self._validation = None

    def __len__(self) -> int:
        return self.count

    def _record(self, row: tuple, connection: sqlite3.Connection) -> dict:
        id_, clock, period, required, start, end, measurement, delay = row
        item = {"id": id_, "kind": "local", "fpga": self.metadata["fpga"],
                "clock_domain": clock, "clock_period_ns": period,
                "start_pin": start, "end_pin": end}
        if self.v2:
            item.update(required_time_ns=required, measurement=measurement,
                        path_pins=[p[0] for p in connection.execute(
                            "SELECT pin FROM path_pins WHERE path=? ORDER BY ordinal", (id_,))]
                        if measurement == "explicit-routed-path-chain" else [])
        if self.timing:
            item["delay_ns"] = delay
        return item

    def __iter__(self) -> Iterator[dict]:
        connection = _connect(self.path)
        try:
            for row in connection.execute("SELECT * FROM paths ORDER BY id"):
                yield self._record(row, connection)
        finally:
            connection.close()

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        connection = _connect(self.path)
        try:
            row = connection.execute("SELECT * FROM paths ORDER BY id LIMIT 1 OFFSET ?", (index,)).fetchone()
            return self._record(row, connection)
        finally:
            connection.close()

    def __eq__(self, other):
        return isinstance(other, Sequence) and len(self) == len(other) and all(a == b for a, b in zip(self, other))

    def envelope(self, manifest_path: Path) -> dict:
        value = {**self.header, "metadata": dict(self.metadata), "storage": dict(self.header["storage"])}
        value["storage"]["path"] = os.path.relpath(self.path, manifest_path.parent)
        return value

    def validation(self) -> dict:
        if self._validation is not None:
            return dict(self._validation)
        connection = _connect(self.path)
        try:
            invalid = connection.execute("""
                SELECT id FROM paths WHERE
                    typeof(id)!='text' OR length(id)=0 OR typeof(clock)!='text' OR length(clock)=0
                    OR typeof(start)!='text' OR length(start)=0 OR typeof(end)!='text' OR length(end)=0
                    OR typeof(period) NOT IN ('real','integer') OR period<=0 OR abs(period)>1.7976931348623157e308
                    OR (? AND (typeof(required) NOT IN ('real','integer') OR required<=0 OR abs(required)>1.7976931348623157e308
                        OR measurement NOT IN ('explicit-routed-path-chain','endpoint-longest-path-fallback') OR measurement IS NULL))
                    OR (? AND (typeof(delay) NOT IN ('real','integer') OR delay<0 OR abs(delay)>1.7976931348623157e308))
                    OR (NOT ? AND delay IS NOT NULL) LIMIT 1
            """, (self.v2, self.timing, self.timing)).fetchone()
            if invalid is not None:
                raise ValidationError(f"invalid indexed local path {invalid[0]!r}")
            invalid = connection.execute("""
                SELECT p.id FROM paths p LEFT JOIN path_pins q ON q.path=p.id
                GROUP BY p.id HAVING
                    (p.measurement='endpoint-longest-path-fallback' AND count(q.pin)>0)
                    OR (p.measurement='explicit-routed-path-chain' AND
                        (count(q.pin)<2 OR min(q.ordinal)!=0 OR max(q.ordinal)!=count(q.pin)-1
                         OR sum(length(q.pin)=0 OR instr(q.pin,',')>0)>0
                         OR (SELECT pin FROM path_pins WHERE path=p.id ORDER BY ordinal LIMIT 1)!=p.start
                         OR (SELECT pin FROM path_pins WHERE path=p.id ORDER BY ordinal DESC LIMIT 1)!=p.end))
                LIMIT 1
            """).fetchone()
            if invalid is not None or connection.execute(
                "SELECT 1 FROM path_pins q LEFT JOIN paths p ON p.id=q.path WHERE p.id IS NULL LIMIT 1"
            ).fetchone() is not None:
                raise ValidationError("indexed local path chain is invalid")
            count, maximum = connection.execute("SELECT count(*),max(delay) FROM paths").fetchone()
            checked = {"status": "pass", "fpga": self.metadata["fpga"], "local_paths": count}
            if self.timing:
                counts = dict(connection.execute("SELECT measurement,count(*) FROM paths GROUP BY measurement"))
                checked.update(maximum_delay_ns=float(maximum or 0.0), measurement_counts={
                    name: counts.get(name, 0) for name in ("explicit-routed-path-chain", "endpoint-longest-path-fallback")
                } if self.v2 else {})
            if checked != self.header["validation"]:
                raise ValidationError("indexed local path validation summary disagrees")
            self._validation = checked
            return dict(checked)
        finally:
            connection.close()


def open_local_paths(manifest_path: Path, header: Mapping[str, Any], *, verify_digest: bool = False) -> dict:
    from .io import file_sha256
    try:
        if header["schema"] != STORAGE_SCHEMA:
            raise ValueError("schema")
        storage = header["storage"]
        if any(isinstance(storage[key], bool) or not isinstance(storage[key], int) or storage[key] < 0 for key in ("records", "bytes")):
            raise ValueError("payload counts")
        relative = Path(storage["path"])
        if relative.is_absolute():
            raise ValueError("absolute payload")
        path = manifest_path.parent / relative
        if path.stat().st_size != storage["bytes"] or not isinstance(storage["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", storage["sha256"]) is None:
            raise ValueError("payload identity")
        if verify_digest and file_sha256(path) != storage["sha256"]:
            raise ValueError("payload digest")
        connection = _connect(path)
        try:
            sealed = dict(connection.execute("SELECT key,value FROM metadata"))
            if (json.loads(sealed["document"]) != header["metadata"]
                    or json.loads(sealed["validation"]) != header["validation"]
                    or connection.execute("SELECT count(*) FROM paths").fetchone()[0] != storage["records"]
                    or header["metadata"]["coverage"]["local_paths"] != storage["records"]):
                raise ValueError("payload/header binding")
        finally:
            connection.close()
        return {**header["metadata"], "paths": LocalPathRows(path, header)}
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as error:
        raise ValidationError(f"invalid local path storage: {error}") from error


def local_paths_for_json(value: Mapping[str, Any], destination: Path) -> dict:
    """Rebase references only; serialization must never iterate lazy rows."""
    if isinstance(value.get("paths"), LocalPathRows):
        if {key: item for key, item in value.items() if key != "paths"} != value["paths"].metadata:
            raise ValidationError("indexed local path metadata changed without a new producer")
        return value["paths"].envelope(destination)
    if value.get("schema") == "emuflow.phase7b-physical-summary/v1":
        local = value.get("local_path_timing", {})
        return {**value, "local_path_timing": {
            fpga: local_paths_for_json(database, destination)
            for fpga, database in local.items()
        }} if local else dict(value)
    return dict(value)


def write_local_paths(path: Path, metadata: Mapping[str, Any], records) -> dict:
    """Validate and insert each row once, then publish a bounded header."""
    from .io import file_sha256, write_json
    from .local_path_timing import validate_local_path_identity
    meta = {key: value for key, value in metadata.items() if key not in {"paths", "coverage"}}
    timing = meta["schema"] == TIMING
    identity_schema = meta.get("identity_schema", IDENTITY_V1 if timing else meta["schema"])
    v2 = identity_schema == IDENTITY_V2
    validate_local_path_identity({**meta, "schema": identity_schema, "coverage": {"local_paths": 0}, "paths": []})
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = path.with_name(path.name + ".paths." + uuid.uuid4().hex + ".sqlite")
    temporary = payload.with_name("." + payload.name + "." + uuid.uuid4().hex + ".tmp")
    connection = sqlite3.connect(temporary)
    published = False
    count, maximum = 0, 0.0
    measurements: Counter = Counter()
    expected = {"id", "kind", "fpga", "clock_domain", "clock_period_ns", "start_pin", "end_pin"}
    if v2:
        expected.update({"required_time_ns", "measurement", "path_pins"})
    if timing:
        expected.add("delay_ns")
    try:
        connection.executescript("""
            PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE;
            PRAGMA cache_size=-8192;
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE paths(id TEXT PRIMARY KEY,clock TEXT NOT NULL,period REAL NOT NULL,
                required REAL,start TEXT NOT NULL,end TEXT NOT NULL,measurement TEXT,delay REAL) WITHOUT ROWID;
            CREATE TABLE path_pins(path TEXT,ordinal INTEGER,pin TEXT NOT NULL,PRIMARY KEY(path,ordinal)) WITHOUT ROWID;
        """)
        for item in records:
            if (not isinstance(item, dict) or set(item) != expected
                    or item["kind"] != "local" or item["fpga"] != meta["fpga"]
                    or any(not isinstance(item[key], str) or not item[key] for key in ("id", "clock_domain", "start_pin", "end_pin"))):
                raise ValidationError("local path storage record is invalid")
            for key in ("clock_period_ns", *( ["required_time_ns"] if v2 else []), *( ["delay_ns"] if timing else [])):
                value = item[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or (value < 0 if key == "delay_ns" else value <= 0):
                    raise ValidationError(f"local path storage {key} is invalid")
            if v2:
                pins, measurement = item["path_pins"], item["measurement"]
                if not isinstance(pins, list) or measurement not in {"explicit-routed-path-chain", "endpoint-longest-path-fallback"}:
                    raise ValidationError("local path storage measurement is invalid")
                if measurement == "explicit-routed-path-chain":
                    if len(pins) < 2 or pins[0] != item["start_pin"] or pins[-1] != item["end_pin"] or any(not isinstance(p, str) or not p or "," in p for p in pins):
                        raise ValidationError("local path storage chain is invalid")
                elif pins:
                    raise ValidationError("local path storage fallback chain is invalid")
                measurements[measurement] += 1
            connection.execute("INSERT INTO paths VALUES (?,?,?,?,?,?,?,?)", (
                item["id"], item["clock_domain"], item["clock_period_ns"], item.get("required_time_ns"),
                item["start_pin"], item["end_pin"], item.get("measurement"), item.get("delay_ns")))
            if v2 and item["path_pins"]:
                connection.executemany("INSERT INTO path_pins VALUES (?,?,?)", ((item["id"], i, pin) for i, pin in enumerate(item["path_pins"])))
            count += 1
            maximum = max(maximum, item.get("delay_ns", 0.0))
        if metadata.get("coverage", {"local_paths": count}) != {"local_paths": count}:
            raise ValidationError("local path storage coverage is invalid")
        meta["coverage"] = {"local_paths": count}
        validation = {"status": "pass", "fpga": meta["fpga"], "local_paths": count}
        if timing:
            validation.update(maximum_delay_ns=maximum, measurement_counts={
                name: measurements[name] for name in ("explicit-routed-path-chain", "endpoint-longest-path-fallback")
            } if v2 else {})
        connection.executemany("INSERT INTO metadata VALUES (?,?)", ((key, json.dumps(value, sort_keys=True)) for key, value in (("document", meta), ("validation", validation))))
        connection.commit()
        connection.close()
        os.replace(temporary, payload)
        with payload.open("rb") as stream:
            os.fsync(stream.fileno())
        header = {"schema": STORAGE_SCHEMA, "metadata": meta, "validation": validation,
                  "storage": {"path": payload.name, "records": count, "bytes": payload.stat().st_size, "sha256": file_sha256(payload)}}
        write_json(path, header, compact=True)
        published = True
        return {**validation, "output": str(path)}
    except sqlite3.IntegrityError as error:
        raise ValidationError(f"duplicate/invalid local path storage row: {error}") from error
    finally:
        connection.close()
        temporary.unlink(missing_ok=True)
        if not published:
            payload.unlink(missing_ok=True)


class PreparedPathRows(Sequence):
    """Only the endpoint facts needed by one FPGA's binding worker."""

    def __init__(self, path: Path, fpga: str, count: int):
        self.path, self.fpga, self.count = path, fpga, count

    def __len__(self):
        return self.count

    @staticmethod
    def _record(row):
        return {"id": row[0], "clock_domain": row[1], "clock_period_ns": row[2],
                "slack_ns": row[3], "fixed_delay_ns": row[4],
                "startpoint": dict(zip(("object", "instance", "port", "bit"), row[5:9])),
                "endpoint": dict(zip(("object", "instance", "port", "bit"), row[9:13]))}

    def __iter__(self):
        connection = _connect(self.path)
        try:
            for row in connection.execute("SELECT id,clock,period,slack,fixed,so,si,sp,sb,eo,ei,ep,eb FROM local WHERE fpga=? ORDER BY id", (self.fpga,)):
                yield self._record(row)
        finally:
            connection.close()

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        connection = _connect(self.path)
        try:
            row = connection.execute("SELECT id,clock,period,slack,fixed,so,si,sp,sb,eo,ei,ep,eb FROM local WHERE fpga=? ORDER BY id LIMIT 1 OFFSET ?", (self.fpga, index)).fetchone()
            return self._record(row)
        finally:
            connection.close()

    def __eq__(self, other):
        return isinstance(other, Sequence) and len(self) == len(other) and all(a == b for a, b in zip(self, other))


class PreparedPathStore:
    """One ephemeral typed SQL spool shared by physical workers, not a cache."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / (".local-path-inputs-" + uuid.uuid4().hex + ".sqlite")
        self.connection = sqlite3.connect(self.path)
        self.connection.executescript("""
            PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE; PRAGMA cache_size=-8192;
            CREATE TABLE ids(id TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE local(id TEXT PRIMARY KEY,fpga TEXT,clock TEXT,period REAL,slack REAL,fixed REAL,
                so TEXT,si TEXT,sp TEXT,sb INTEGER,eo TEXT,ei TEXT,ep TEXT,eb INTEGER) WITHOUT ROWID;
        """)
        self.count = 0

    def add_ids(self, ids):
        for id_ in ids:
            self.connection.execute("INSERT INTO ids VALUES (?)", (id_,))
            self.count += 1

    def add(self, fpga, record):
        endpoints = tuple(record[side].get(key) for side in ("startpoint", "endpoint") for key in ("object", "instance", "port", "bit"))
        self.connection.execute("INSERT INTO local VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            record["id"], fpga, record["clock_domain"], record["clock_period_ns"],
            record["slack_ns"], record["fixed_delay_ns"], *endpoints))

    def __len__(self):
        return self.count

    def sorted_id_digest(self):
        digest = hashlib.sha256(b"[")
        for i, (id_,) in enumerate(self.connection.execute("SELECT id FROM ids ORDER BY id")):
            if i:
                digest.update(b",")
            digest.update(json.dumps(id_, ensure_ascii=False).encode("utf-8"))
        digest.update(b"]")
        return digest.hexdigest()

    def finish(self):
        self.connection.execute("CREATE INDEX local_by_fpga ON local(fpga,id)")
        counts = list(self.connection.execute("SELECT fpga,count(*) FROM local GROUP BY fpga"))
        self.connection.commit()
        self.connection.close()
        return {fpga: PreparedPathRows(self.path, fpga, count) for fpga, count in counts}

    def abort(self):
        self.connection.close()
        self.path.unlink(missing_ok=True)
