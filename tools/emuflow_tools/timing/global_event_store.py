"""Read native Global STA event facts and retain their source-bound lifetime."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import struct
import shutil
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import resolve_native_executable
from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri
from emuflow.storage.store_publication import bind_store_source, require_store_sources_unchanged
from emuflow.common.tool_runtime import NativeToolProcess

_ROLES = ("target", "runtime", "tx", "commit")


def _native(arguments, root):
    executable = resolve_native_executable("emuflow-native")
    with NativeToolProcess(executable, [executable, "global-sta", *map(str, arguments)],
                           environment=dict(os.environ), scratch_parent=root) as process:
        result = process.finish()
    if result.returncode:
        raise ValidationError("native global STA failed: " + result.stdout)


class _Owner:
    def __init__(self):
        # Retire only after the last borrowed cursor closes, including cyclic GC.
        self.closed = False
        self._borrows = 0
        self.connection = None
        self._dependents = []
        self.root = Path(tempfile.mkdtemp(prefix="emuflow-global-events-"))
        self.primary = self.root / "events.sqlite"
        self.index = self.root / "index.sqlite"
        self.mode = 3
        self.paths = self.events = self.checks = 0
        self.finished = False
        self.sources = ()
        self.header = None

    def close(self):
        if self.closed:
            return
        self.closed = True
        if not self._borrows:
            self._dispose()

    def _dispose(self):
        for dependent in self._dependents:
            dependent.close()
        self._dependents.clear()
        connection, self.connection = self.connection, None
        if connection is not None:
            connection.close()
        shutil.rmtree(self.root)

    def __del__(self):
        try:
            self.close()
        except BaseException:
            pass

    def writable(self):
        if self.finished or self.closed:
            raise ValidationError("global STA event builder is sealed")

    def require(self):
        if self.closed:
            raise ValidationError("global STA event owner is closed")
        if not self._borrows:
            require_store_sources_unchanged(self.sources)

    @contextmanager
    def borrow(self):
        self.require()
        self._borrows += 1
        try:
            yield
        except BaseException:
            self._borrows -= 1
            if self.closed and not self._borrows:
                self._dispose()
            raise
        else:
            self._borrows -= 1
            if self.closed and not self._borrows:
                self._dispose()
            self.require()


    def _open_index(self, header):
        original = bind_store_source(self.primary, "global-event-primary-v1")
        _native(["index-events", self.primary, self.index], self.root)
        require_store_sources_unchanged((original,))
        derived = bind_store_source(self.index, "global-event-index-v1")
        self.connection = sqlite3.connect(immutable_sqlite_uri(self.primary), uri=True)
        self.connection.execute("ATTACH DATABASE ? AS derived", (immutable_sqlite_uri(self.index),))
        self.connection.executescript("PRAGMA cache_size=-8192; PRAGMA derived.cache_size=-8192; PRAGMA temp_store=FILE;")
        value = self.connection.execute("SELECT value FROM derived.metadata WHERE key='header'").fetchone()
        if value is None or len(value[0]) > 8192:
            raise ValidationError("native global STA index header is missing/oversized")
        self.header = json.loads(value[0])
        if (self.header.get("schema") != "emuflow.global-event-index/v1"
                or self.header.get("implementation_id") != "native-global-event-index-v1"
                or any(self.header.get(key) != header[key] for key in ("mode", "paths", "checks", "events"))
                or self.header.get("source_path") != str(original.path)
                or self.header.get("source_generation") != [str(n) for n in original.generation]):
            raise ValidationError("native global STA index ownership disagrees")
        self.sources = (original, derived)
        self.require()

    def accept_native(self):
        self.writable()
        try:
            with closing(sqlite3.connect(immutable_sqlite_uri(self.primary), uri=True)) as connection:
                values = dict(connection.execute("SELECT key,value FROM metadata"))
            if set(values) != {"header", "binding"} or any(len(value) > 65536 for value in values.values()):
                raise ValidationError("native global STA binding metadata is invalid")
            header, binding = json.loads(values["header"]), json.loads(values["binding"])
            if (set(header) != {"schema", "mode", "paths", "checks", "events"}
                    or header["schema"] != "emuflow.global-event-checks/v1" or header["mode"] != 3
                    or any(type(header[key]) is not int or header[key] < 0 for key in ("paths", "checks", "events"))
                    or set(binding) != {"implementation_id", "compressed_representative_paths", "source_binding"}
                    or binding["implementation_id"] != "native-physical-event-binding-v1"
                    or type(binding["compressed_representative_paths"]) is not int
                    or binding["compressed_representative_paths"] < 0):
                raise ValidationError("native global STA binding coverage is invalid")
            self.paths, self.checks, self.events = (header[key] for key in ("paths", "checks", "events"))
            self.finished = True
            self._open_index(header)
            return binding
        except BaseException:
            self.close()
            raise

    def cursor(self, sql, parameters=()):
        self.require()
        return self.connection.execute(sql, parameters)


class _Strings(Sequence):
    def __init__(self, owner, kind):
        self.owner, self.kind = owner, kind

    def __len__(self):
        return self.owner.paths if self.kind in {"paths", "sorted"} else self.owner.events

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[n] for n in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        if self.kind == "sorted":
            query = "SELECT p.path FROM derived.path_order o JOIN paths p ON p.seq=o.path_seq WHERE o.position=?"
        elif self.kind == "paths":
            query = "SELECT path FROM paths WHERE seq=?"
        else:
            query = "SELECT event FROM events WHERE seq=?"
        cursor = self.owner.cursor(query, (index,))
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
        if row is None:
            raise ValidationError("global STA string identity is missing")
        return row[0]

    def __iter__(self):
        query = ("SELECT p.path FROM derived.path_order o JOIN paths p ON p.seq=o.path_seq ORDER BY o.position"
                 if self.kind == "sorted" else "SELECT path FROM paths ORDER BY seq"
                 if self.kind == "paths" else "SELECT event FROM events ORDER BY seq")
        with self.owner.borrow():
            cursor = self.owner.cursor(query)
            try:
                for row in cursor:
                    self.owner.require()
                    yield row[0]
            finally:
                cursor.close()

    def __eq__(self, other):
        return isinstance(other, Sequence) and len(self) == len(other) and all(a == b for a, b in zip(self, other))


class EventChecks(Sequence):
    def __init__(self, owner, representatives=False):
        self._owner, self.representatives = owner, representatives
        self._paths, self._events = _Strings(owner, "paths"), _Strings(owner, "events")

    def __len__(self):
        return self._owner.header["representative_checks"] if self.representatives else self._owner.checks

    def _record(self, row):
        from emuflow_tools.timing.global_sta import EventCheck
        if row is None or not 0 <= row[1] < len(_ROLES) or len(row[3]) < 16 or len(row[3]) % 8:
            raise ValidationError("global STA check identity/numbers are invalid")
        numbers = struct.unpack("<" + "d" * (len(row[3]) // 8), row[3])
        return EventCheck(row[0], _ROLES[row[1]], row[2], numbers[0], numbers[2:], numbers[1])

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[n] for n in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        if self.representatives:
            query = "SELECT representative_first_check+(?-engine_first_check) FROM derived.path_index WHERE first_check=representative_first_check AND check_count>0 AND engine_first_check<=? AND engine_first_check+check_count>? ORDER BY engine_first_check DESC LIMIT 1"
            cursor = self._owner.cursor(query, (index, index, index))
            try:
                row = cursor.fetchone()
            finally:
                cursor.close()
            if row is None:
                raise ValidationError("global STA representative identity is missing")
            index = row[0]
        cursor = self._owner.cursor("SELECT p.path,c.role,e.event,c.numbers FROM checks c JOIN paths p ON p.seq=c.path_seq JOIN events e ON e.seq=c.event_seq WHERE c.seq=?", (index,))
        try:
            return self._record(cursor.fetchone())
        finally:
            cursor.close()

    def __iter__(self):
        with self._owner.borrow():
            if self.representatives:
                query = "SELECT p.path,c.role,e.event,c.numbers FROM derived.path_index i JOIN checks c ON c.seq>=i.first_check AND c.seq<i.first_check+i.check_count JOIN paths p ON p.seq=c.path_seq JOIN events e ON e.seq=c.event_seq WHERE i.first_check=i.representative_first_check AND i.check_count>0 ORDER BY i.engine_first_check,c.seq"
            else:
                query = "SELECT p.path,c.role,e.event,c.numbers FROM checks c JOIN paths p ON p.seq=c.path_seq JOIN events e ON e.seq=c.event_seq ORDER BY c.seq"
            cursor = self._owner.cursor(query)
            try:
                for row in cursor:
                    self._owner.require()
                    yield self._record(row)
            finally:
                cursor.close()

    def __eq__(self, other):
        return isinstance(other, Sequence) and len(self) == len(other) and all(a == b for a, b in zip(self, other))

    def close(self):
        self._owner.close()

    def require_valid(self):
        self._owner.require()
        if not len(self):
            raise ValidationError("global STA has no timing checks")

    def compressed(self):
        self.require_valid()
        if self.representatives:
            raise ValidationError("global STA representative selection is already compressed")
        return EventChecks(self._owner, True), RepresentativeMapping(self._owner)


class RepresentativeMapping(Sequence):
    def __init__(self, owner):
        self.owner = owner

    def __len__(self):
        return self.owner.checks

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[n] for n in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        cursor = self.owner.cursor("SELECT i.engine_first_check+c.seq-i.first_check FROM checks c JOIN derived.path_index i ON i.path_seq=c.path_seq WHERE c.seq=?", (index,))
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
        if row is None:
            raise ValidationError("global STA exact compression lost a timing check")
        return row[0]

    def __iter__(self):
        with self.owner.borrow():
            cursor = self.owner.cursor("SELECT i.engine_first_check+c.seq-i.first_check FROM checks c JOIN derived.path_index i ON i.path_seq=c.path_seq ORDER BY c.seq")
            try:
                for row in cursor:
                    self.owner.require()
                    yield row[0]
            finally:
                cursor.close()


class PathMetadata(Mapping):
    def __init__(self, owner):
        self._owner = owner
        self._paths = _Strings(owner, "sorted")

    def __len__(self):
        return self._owner.paths

    def __iter__(self):
        return iter(self._paths)

    def __getitem__(self, path):
        if not isinstance(path, str):
            raise KeyError(path)
        cursor = self._owner.cursor("SELECT representative_path,path_scope,clock_domain,target_period_ns,cone_bound,interconnect_delay_model FROM paths WHERE path=?", (path,))
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
        if row is None:
            raise KeyError(path)
        return {"representative_path": row[0], "path_scope": "same-fpga-local" if row[1] else "cross-fpga",
                "clock_domain": row[2], "target_period_ns": row[3],
                "physical_logic_segments_cone_bound": bool(row[4]),
                **({"interconnect_delay_model": row[5]} if row[5] is not None else {})}

    def position(self, path):
        cursor = self._owner.cursor(
            "SELECT o.position FROM paths p JOIN derived.path_order o ON o.path_seq=p.seq WHERE p.path=?",
            (path,),
        )
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
        if row is None:
            raise ValidationError("global STA measurement path is unknown")
        return row[0]

    def close(self):
        self._owner.close()

    def path_id_set_sha256(self):
        self._owner.require()
        return self._owner.header["path_ids_sha256"]


def export_event_model(rows, directory):
    rows.require_valid()
    owner = rows._owner
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    request, summary = directory / ".event-export-request.bin", directory / "model.json"
    writer = CheckedRequestWriter(request)
    try:
        writer.put("QII", 0x31584547464D5545, 1, int(rows.representatives))
        for source in owner.sources:
            writer.put("3Q4q", *source.generation)
        writer.finish()
        _native(["export-events", owner.primary, owner.index, request, summary], owner.root)
        owner.require()
        if summary.stat().st_size > 8192:
            raise ValidationError("native global STA model manifest exceeds bound")
        value = json.loads(summary.read_text())
        if (value.get("schema") != "emuflow.global-event-export/v1" or value.get("status") != "pass"
                or value.get("checks") != len(rows) or value.get("logical_sha256") != owner.header["logical_sha256"]):
            raise ValidationError("native global STA model coverage/source disagrees")
        model, _ = event_model_sources(directory)
        return model
    finally:
        writer.close()
        request.unlink(missing_ok=True)


def event_model_sources(directory):
    directory = Path(directory)
    manifest = directory / "model.json"
    if manifest.stat().st_size > 8192:
        raise ValidationError("native global STA model manifest exceeds bound")
    value = json.loads(manifest.read_text())
    if (value.get("schema") != "emuflow.global-event-export/v1"
            or value.get("status") != "pass"
            or value.get("implementation_id") != "native-global-event-export-v1"
            or not isinstance(value.get("directory"), str)):
        raise ValidationError("native global STA model manifest is invalid")
    model = Path(value["directory"])
    if not model.is_dir() or model.is_symlink() or model.parent.resolve() != directory.resolve():
        raise ValidationError("native global STA model directory is invalid")
    files = value.get("files")
    names = {"global_timing.lib", "global_timing.v", "global_timing.sdc", "analyze.tcl"}
    if (not isinstance(files, list) or len(files) != len(names)
            or any(not isinstance(item, dict) or set(item) != {"name", "bytes", "sha256", "generation"}
                   or not isinstance(item["name"], str) for item in files)
            or {item["name"] for item in files} != names):
        raise ValidationError("native global STA model inventory is invalid")
    sources = []
    for item in files:
        path = model / item["name"]
        source = bind_store_source(path, "global-event-model-text-v1")
        if (path.is_symlink() or type(item["bytes"]) is not int
                or source.generation[2] != item["bytes"]
                or item["generation"] != [str(n) for n in source.generation]
                or not isinstance(item["sha256"], str) or len(item["sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in item["sha256"])):
            raise ValidationError("native global STA model source generation changed")
        sources.append(source)
    require_store_sources_unchanged(sources)
    return model, tuple(sources)
