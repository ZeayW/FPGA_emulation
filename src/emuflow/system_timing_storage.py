"""Indexed owner of original-path OpenSTA results behind a bounded QoR selector."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import weakref

from .errors import ValidationError
from .io import file_sha256, json_writes_are_durable, write_json
from .timing_disposition import OPTIMIZED_AWAY, PATH_POPULATION_POLICY, is_optimized_away, validate_optimization_record

SCHEMA = "emuflow.system-qor-storage/v1"
APPLICATION_ID = 0x454D5551  # EMUQ
SCALARS = ("system_delay_bound_ns", "target_required_time_ns", "runtime_required_time_ns",
           "target_clock_slack_bound_ns", "runtime_clock_slack_bound_ns")
_BASE = {"path", "representative_path", "path_scope", "clock_domain", "target_period_ns",
         "physical_logic_segments_cone_bound", *SCALARS}
_PROOF = {"measurement", "optimization_source", "optimization_fpga"}
_DDL = """
CREATE TABLE metadata(key TEXT PRIMARY KEY NOT NULL,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE symbols(symbol INTEGER PRIMARY KEY,value TEXT NOT NULL UNIQUE CHECK(length(value)>0));
CREATE TABLE paths(
 seq INTEGER PRIMARY KEY CHECK(seq>=0), path TEXT NOT NULL UNIQUE CHECK(length(path)>0),
 scope INTEGER NOT NULL CHECK(scope IN (0,1)), period REAL NOT NULL CHECK(period>0),
 cone INTEGER NOT NULL CHECK(cone IN (0,1)),
 representative INTEGER NOT NULL REFERENCES symbols(symbol), clock INTEGER REFERENCES symbols(symbol),
 model INTEGER REFERENCES symbols(symbol), optimization_source INTEGER REFERENCES symbols(symbol),
 optimization_fpga INTEGER REFERENCES symbols(symbol),
 delay REAL, target_required REAL, runtime_required REAL, target_slack REAL, runtime_slack REAL,
 CHECK((optimization_source IS NULL AND optimization_fpga IS NULL AND delay IS NOT NULL
   AND target_required IS NOT NULL AND runtime_required IS NOT NULL AND target_slack IS NOT NULL AND runtime_slack IS NOT NULL)
 OR (optimization_source IS NOT NULL AND optimization_fpga IS NOT NULL AND delay IS NULL
   AND target_required IS NULL AND runtime_required IS NULL AND target_slack IS NULL AND runtime_slack IS NULL))
);
"""
_SELECT = """SELECT p.seq,p.path,p.scope,p.period,p.cone,r.value,c.value,m.value,s.value,f.value,
 p.delay,p.target_required,p.runtime_required,p.target_slack,p.runtime_slack FROM paths p
 LEFT JOIN symbols r ON r.symbol=p.representative LEFT JOIN symbols c ON c.symbol=p.clock
 LEFT JOIN symbols m ON m.symbol=p.model LEFT JOIN symbols s ON s.symbol=p.optimization_source
 LEFT JOIN symbols f ON f.symbol=p.optimization_fpga """


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValidationError("system timing value is not canonical") from error


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**32-1


def _text(value, optional=False):
    return optional and value is None or isinstance(value, str) and bool(value)


def _validate_row(row):
    if not isinstance(row, Mapping):
        raise ValidationError("system timing path record is malformed")
    eliminated = is_optimized_away(row)
    required = _BASE | (_PROOF if eliminated else set())
    if (not isinstance(row, Mapping) or not required <= set(row)
            or set(row) - required - {"interconnect_delay_model"}
            or any(not _text(row[k]) for k in ("path", "representative_path"))
            or not _text(row["clock_domain"], optional=True)
            or row["path_scope"] not in {"cross-fpga", "same-fpga-local"}
            or not isinstance(row["physical_logic_segments_cone_bound"], bool)
            or isinstance(row["target_period_ns"], bool)
            or not isinstance(row["target_period_ns"], (int, float))
            or not math.isfinite(row["target_period_ns"]) or row["target_period_ns"] <= 0
            or ("interconnect_delay_model" in row and not _text(row["interconnect_delay_model"]))):
        raise ValidationError("system timing path record is malformed")
    if eliminated:
        validate_optimization_record(row)
        if not _text(row["optimization_fpga"]) or any(row[k] is not None for k in SCALARS):
            raise ValidationError("eliminated system timing path carries engine scalars")
    elif any(isinstance(row[k], bool) or not isinstance(row[k], (int, float)) or not math.isfinite(row[k]) for k in SCALARS):
        raise ValidationError("implemented system timing path lacks finite engine scalars")


def _validate_header(header, counts):
    if (not isinstance(header, dict) or header.get("schema") != "emuflow.qor-report/v4"
            or not isinstance(header.get("timing"), dict)
            or header["timing"].get("schema") != "emuflow.system-timing/v2"
            or header["timing"].get("qualification") != "opensta-fixed-event-physical-bounds"
            or header["timing"].get("path_population_policy") != PATH_POPULATION_POLICY
            or "paths" in header["timing"] or not isinstance(counts, dict)
            or set(counts) != {"paths", "implemented", "optimized_away"}
            or any(not _count(v) for v in counts.values()) or counts["paths"] == 0
            or counts["implemented"] + counts["optimized_away"] != counts["paths"]):
        raise ValidationError("system timing selector header or population is invalid")
    summary = header["timing"].get("summary", {})
    if not isinstance(summary, dict):
        raise ValidationError("system timing selector summary is invalid")
    if any(summary.get(field) != counts[key] for field, key in (
            ("timing_paths", "paths"), ("implemented_paths", "implemented"), ("optimized_away_paths", "optimized_away"))):
        raise ValidationError("system timing selector counts disagree with the report")
    source = header["timing"].get("source_binding")
    if source is not None and (not isinstance(source, dict) or not _count(source.get("original_paths"))
            or source["original_paths"] != counts["paths"] or not _digest(source.get("original_path_ids_sha256"))):
        raise ValidationError("system timing selector source population is invalid")
    gate = header["timing"].get("global_opensta", {})
    if not isinstance(gate, dict) or not isinstance(gate.get("metrics"), dict) or not _count(gate.get("checks")):
        raise ValidationError("system timing selector OpenSTA summary is invalid")
    if any(gate.get(field) != counts[key] for field,key in (
            ("original_paths","paths"),("implemented_paths","implemented"),("optimized_away_paths","optimized_away"))):
        raise ValidationError("system timing OpenSTA population disagrees with the report")
    if counts["implemented"] == 0 and (gate.get("authority") != "no-numerical-checks"
            or gate.get("execution") != "not-applicable-no-implemented-original-paths" or gate.get("checks") != 0):
        raise ValidationError("eliminated system timing population claims numerical execution")


def _validate_metrics(db, header):
    timing = header["timing"]
    for role, column in (("target", "target_slack"), ("runtime", "runtime_slack")):
        wns, negative = db.execute(f"SELECT min({column}),count(CASE WHEN {column}<0 THEN 1 END) FROM paths WHERE optimization_source IS NULL").fetchone()
        tns = math.fsum(row[0] for row in db.execute(f"SELECT {column} FROM paths WHERE {column}<0 ORDER BY seq"))
        worst = db.execute(f"SELECT path FROM paths WHERE optimization_source IS NULL ORDER BY {column},path LIMIT 1").fetchone()
        expected = {"worst_path": worst[0] if worst else None, "worst_slack_bound_ns": wns,
                    "tns_bound_ns": tns, "total_negative_slack_bound_ns": tns, "negative_slack_paths": negative}
        clock = timing.get(role+"_clock")
        if not isinstance(clock, dict) or any(clock.get(key) != value for key, value in expected.items()):
            raise ValidationError("system timing clock metrics disagree with original-path scalars")
        metrics = timing.get("global_opensta", {}).get("metrics", {}).get(role)
        if metrics != {"wns_ns": wns, "original_path_tns_ns": tns, "negative_paths": negative}:
            raise ValidationError("system timing OpenSTA metrics disagree with original-path scalars")
    maximum = db.execute("SELECT max(delay) FROM paths WHERE optimization_source IS NULL").fetchone()[0]
    if timing["summary"].get("maximum_system_delay_bound_ns") != maximum:
        raise ValidationError("system timing maximum disagrees with original-path scalars")


@lru_cache(maxsize=1)
def _expected_objects():
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(_DDL)
        return _objects(db)
    finally:
        db.close()


def _objects(db):
    return tuple((kind, name, table, " ".join(str(sql).split())) for kind, name, table, sql in db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"))


def _hash_start(header):
    return hashlib.sha256(("emuflow-system-qor-logical-v1\n" + _canonical(header) + "\n").encode())


def write_system_qor(path: Path, qor: Mapping):
    """Stream logical paths directly into their one canonical typed owner."""
    rows = qor["timing"]["paths"]
    header = {**qor, "timing": {key: value for key, value in qor["timing"].items() if key != "paths"}}
    summary = header["timing"]["summary"]
    counts = {key: summary[field] for key, field in (("paths", "timing_paths"),
              ("implemented", "implemented_paths"), ("optimized_away", "optimized_away_paths"))}
    _validate_header(header, counts)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".system-timing-", suffix=".sqlite", dir=path.parent)
    os.close(fd)
    temporary = Path(name)
    db = None
    payload, created, published = None, False, False
    try:
        db = sqlite3.connect(temporary)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=OFF")
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.execute("PRAGMA user_version=1")
        db.executescript(_DDL)
        db.execute("INSERT INTO metadata VALUES ('header',?)", (_canonical(header),))
        logical = _hash_start(header)
        ids = hashlib.sha256(b"[")
        previous, total, eliminated = None, 0, 0

        @lru_cache(maxsize=4096)
        def intern(value):
            if value is None:
                return None
            db.execute("INSERT OR IGNORE INTO symbols(value) VALUES (?)", (value,))
            return db.execute("SELECT symbol FROM symbols WHERE value=?", (value,)).fetchone()[0]

        for row in rows:
            _validate_row(row)
            if previous is not None and row["path"] <= previous:
                raise ValidationError("system timing original paths must be sorted and unique")
            ids.update(("," if total else "").encode() + _canonical(row["path"]).encode())
            row = {**row, "target_period_ns": float(row["target_period_ns"]),
                   **{key: None if row[key] is None else float(row[key]) for key in SCALARS}}
            logical.update((_canonical(row) + "\n").encode())
            previous = row["path"]
            symbols = [intern(row.get(k)) for k in ("representative_path", "clock_domain", "interconnect_delay_model",
                                                       "optimization_source", "optimization_fpga")]
            db.execute("INSERT INTO paths VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                total, row["path"], int(row["path_scope"] == "same-fpga-local"), row["target_period_ns"],
                int(row["physical_logic_segments_cone_bound"]), *symbols, *(row[k] for k in SCALARS)))
            total += 1
            eliminated += is_optimized_away(row)
        ids.update(b"]")
        if {"paths": total, "implemented": total-eliminated, "optimized_away": eliminated} != counts:
            raise ValidationError("system timing path population does not match the report")
        source = header["timing"].get("source_binding")
        if source is not None and (source.get("original_paths") != total or source.get("original_path_ids_sha256") != ids.hexdigest()):
            raise ValidationError("system timing original identity coverage disagrees")
        _validate_metrics(db, header)
        logical_sha = logical.hexdigest()
        db.execute("INSERT INTO metadata VALUES ('logical_sha256',?)", (logical_sha,))
        db.commit()
        db.close()
        db = None
        store_sha = file_sha256(temporary)
        payload = path.with_name(path.name + ".paths." + logical_sha + ".sqlite")
        if payload.exists():
            if file_sha256(payload) != store_sha:
                raise ValidationError("existing system timing payload differs")
            temporary.unlink()
        else:
            if json_writes_are_durable():
                with temporary.open("rb") as stream:
                    os.fsync(stream.fileno())
            os.replace(temporary, payload)
            created = True
        selector = {"schema": SCHEMA, "logical_schema": header["schema"], "header": header,
                    "counts": counts, "logical_sha256": logical_sha, "artifact": payload.name,
                    "store_bytes": payload.stat().st_size, "store_sha256": store_sha}
        write_json(path, selector, compact=True)
        published = True
        return selector
    except sqlite3.DatabaseError as error:
        raise ValidationError("system timing storage publication failed") from error
    finally:
        if db is not None:
            db.close()
        temporary.unlink(missing_ok=True)
        if created and not published:
            payload.unlink(missing_ok=True)


class IndexedSystemTimingPaths(Sequence):
    """Lazy row projection; summary reads never open or hash the path store."""

    def __init__(self, path: Path, selector):
        self.path, self.selector = path, selector
        self._db = None
        self._finalizer = None

    def __len__(self):
        return self.selector["counts"]["paths"]

    def _open(self):
        if self._db is not None:
            return self._db
        try:
            if (self.path.stat().st_size != self.selector["store_bytes"]
                    or file_sha256(self.path) != self.selector["store_sha256"]):
                raise ValidationError("system timing payload seal differs")
            db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
            try:
                if (db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                        or db.execute("PRAGMA user_version").fetchone()[0] != 1 or _objects(db) != _expected_objects()):
                    raise ValidationError("system timing SQLite envelope is invalid")
                if dict(db.execute("SELECT key,value FROM metadata")) != {
                        "header": _canonical(self.selector["header"]), "logical_sha256": self.selector["logical_sha256"]}:
                    raise ValidationError("system timing payload belongs to a different report")
                population = db.execute("SELECT count(*),min(seq),max(seq),count(optimization_source) FROM paths").fetchone()
                if population != (len(self), 0, len(self)-1, self.selector["counts"]["optimized_away"]):
                    raise ValidationError("system timing stored population is incomplete")
            except BaseException:
                db.close()
                raise
            self._db = db
            self._finalizer = weakref.finalize(self, db.close)
            return db
        except (OSError, sqlite3.DatabaseError) as error:
            raise ValidationError("cannot open system timing payload") from error

    @staticmethod
    def _project(values):
        seq, path, scope, period, cone, representative, clock, model, source, fpga, *scalars = values
        if scope not in (0, 1) or cone not in (0, 1):
            raise ValidationError("system timing stored flags are invalid")
        row = {"path": path, "representative_path": representative, "path_scope": "same-fpga-local" if scope else "cross-fpga",
               "clock_domain": clock, "target_period_ns": period, "physical_logic_segments_cone_bound": bool(cone),
               **dict(zip(SCALARS, scalars))}
        if model is not None:
            row["interconnect_delay_model"] = model
        if source is not None:
            row.update(measurement=OPTIMIZED_AWAY, optimization_source=source, optimization_fpga=fpga)
        elif fpga is not None:
            raise ValidationError("system timing stored proof identity is incomplete")
        _validate_row(row)
        return row

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        try:
            values = self._open().execute(_SELECT + " WHERE p.seq=?", (index,)).fetchone()
            if values is None:
                raise ValidationError("system timing stored path is missing")
            return self._project(values)
        except sqlite3.DatabaseError as error:
            raise ValidationError("system timing stored row is invalid") from error

    def by_path(self, path_id):
        if not isinstance(path_id,str):
            raise KeyError(path_id)
        try:
            values = self._open().execute(_SELECT+" WHERE p.path=?",(path_id,)).fetchone()
            if values is None:
                raise KeyError(path_id)
            return self._project(values)
        except sqlite3.DatabaseError as error:
            raise ValidationError("system timing stored row is invalid") from error

    def __iter__(self):
        try:
            for values in self._open().execute(_SELECT + " ORDER BY p.seq"):
                yield self._project(values)
        except sqlite3.DatabaseError as error:
            raise ValidationError("system timing stored rows are invalid") from error

    def __eq__(self, other):
        if not isinstance(other, Sequence) or len(self) != len(other):
            return False
        return all(left == right for left, right in zip(self, other))

    def close(self):
        if self._finalizer is not None:
            self._finalizer()
        self._db = None
        self._finalizer = None


def expand_system_qor(selector, source_path: Path):
    if (not isinstance(selector, dict) or set(selector) != {"schema", "logical_schema", "header", "counts", "logical_sha256", "artifact", "store_bytes", "store_sha256"}
            or selector["schema"] != SCHEMA or not _digest(selector["logical_sha256"]) or not _digest(selector["store_sha256"])
            or not isinstance(selector["artifact"], str) or Path(selector["artifact"]).name != selector["artifact"]
            or selector["artifact"] in {"", ".", ".."} or "\\" in selector["artifact"]
            or not isinstance(selector["store_bytes"], int) or isinstance(selector["store_bytes"], bool) or selector["store_bytes"] <= 0):
        raise ValidationError("system timing storage selector is invalid")
    _validate_header(selector["header"], selector["counts"])
    if selector["logical_schema"] != selector["header"]["schema"]:
        raise ValidationError("system timing logical schema differs")
    payload = source_path.parent / selector["artifact"]
    if payload.is_symlink():
        raise ValidationError("system timing payload must be a sibling regular file")
    return {**selector["header"], "timing": {**selector["header"]["timing"], "paths": IndexedSystemTimingPaths(payload, selector)}}


def validate_system_qor(qor):
    """Explicit terminal check of every typed row, logical seal and original ID."""
    rows = qor["timing"]["paths"]
    if not isinstance(rows, IndexedSystemTimingPaths):
        raise ValidationError("explicit system timing store validation needs an indexed owner")
    selector = rows.selector
    logical, ids = _hash_start(selector["header"]), hashlib.sha256(b"[")
    previous, total, eliminated = None, 0, 0
    for row in rows:
        if previous is not None and row["path"] <= previous:
            raise ValidationError("system timing stored original paths are not sorted and unique")
        logical.update((_canonical(row)+"\n").encode())
        ids.update(("," if total else "").encode()+_canonical(row["path"]).encode())
        previous = row["path"]
        total += 1
        eliminated += is_optimized_away(row)
    ids.update(b"]")
    if (logical.hexdigest() != selector["logical_sha256"] or total != len(rows)
            or eliminated != selector["counts"]["optimized_away"]):
        raise ValidationError("system timing logical row seal differs")
    source = selector["header"]["timing"].get("source_binding")
    if source is not None and (source.get("original_paths") != total or source.get("original_path_ids_sha256") != ids.hexdigest()):
        raise ValidationError("system timing stored original identity coverage disagrees")
    _validate_metrics(rows._open(), selector["header"])
    return {"status": "pass", **selector["counts"], "logical_sha256": logical.hexdigest()}


def system_timing_payload_reference(path: Path):
    """Read a producer's bounded selector without rehashing its sealed payload."""
    with path.open() as stream:
        selector = json.load(stream)
    view = expand_system_qor(selector,path)["timing"]["paths"]
    return {"artifact": view.path.name, "sha256": selector["store_sha256"]}
