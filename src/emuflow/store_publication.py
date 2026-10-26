"""Bounded source-bound publication of closed derived artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import shutil
from pathlib import Path
import tempfile

from .errors import ValidationError
from .io import file_sha256, json_writes_are_durable, write_json
from .native_protocol import CheckedRequestWriter
from .native_tools import native_file_generation, resolve_native_executable
from .tool_runtime import NativeToolProcess


@dataclass(frozen=True)
class StoreSource:
    path: Path
    schema: str
    generation: tuple[int, ...]


class VerifiedStoreFile:
    """Invocation-scoped file seal, reusable by views of the same generation."""

    def __init__(self, path: Path, schema: str):
        self.source = bind_store_source(path, schema)
        self.sha256 = file_sha256(self.source.path)
        self.require_unchanged()

    @property
    def bytes(self):
        return self.source.generation[2]

    def require_unchanged(self):
        require_store_sources_unchanged((self.source,))

    def require(self, path: Path, schema: str, byte_count: int, sha256: str):
        if (
            Path(path).resolve() != self.source.path
            or schema != self.source.schema
            or byte_count != self.bytes
            or sha256 != self.sha256
        ):
            raise ValidationError("SQLite store seal belongs to another artifact")
        self.require_unchanged()


def bind_store_source(path: Path, schema: str) -> StoreSource:
    path = Path(path).resolve()
    return StoreSource(path, schema, native_file_generation(path))


def bind_view_sources(*views):
    """Bind indexed inputs before production; in-memory reference inputs have no file."""
    result = []
    for value, schema in views:
        if getattr(value, "store_path", None) is None:
            continue
        bind = getattr(value, "bound_store_sources", None)
        if not callable(bind):
            raise ValidationError("indexed input lacks its original source bindings")
        sources = bind()
        if not any(
            source.schema == schema and source.path == Path(value.store_path).resolve()
            for source in sources
        ):
            raise ValidationError("indexed source binding schema disagrees")
        result.extend(sources)
    return tuple(result)


def require_store_sources_unchanged(sources):
    for source in sources:
        try:
            if native_file_generation(source.path) != source.generation:
                raise ValidationError("SQLite publication source generation changed")
        except OSError as error:
            raise ValidationError(
                "SQLite publication source generation changed"
            ) from error


def validate_store_destination(destination: Path, sources):
    """Reject changed inputs and output aliases before acquiring a writer lease."""
    sources = tuple(sources)
    require_store_sources_unchanged(sources)
    destination = Path(destination).resolve()
    for source in sources:
        if destination == source.path or (
            destination.exists() and os.path.samefile(destination, source.path)
        ):
            raise ValidationError("SQLite publication output aliases a source")
    return sources


def publish_sqlite_store(
    temporary: Path, destination: Path, schema: str, *, sources=()
):
    """Move one verified private file; no row transfer, file copy or rehash."""
    temporary, destination = Path(temporary).absolute(), Path(destination).absolute()
    sources = tuple(sources)
    require_store_sources_unchanged(sources)
    if len(sources) > 64:
        raise ValidationError("SQLite publication source count exceeds bound")
    executable = resolve_native_executable("emuflow-native")
    with tempfile.TemporaryDirectory(
        prefix=".store-publication-", dir=destination.parent
    ) as scratch:
        request, report = Path(scratch) / "request.bin", Path(scratch) / "result.json"
        writer = CheckedRequestWriter(request)
        try:
            writer.put("QI", 0x31505351464D5545, 1)
            writer.text(schema)
            writer.text(str(temporary))
            writer.put("3Q4q", *native_file_generation(temporary))
            writer.put("I", len(sources))
            for source in sources:
                writer.text(str(source.path))
                writer.text(source.schema)
                writer.put("3Q4q", *source.generation)
            writer.finish()
        finally:
            writer.close()
        command = [
            executable,
            "--report",
            str(report),
            "publish-store",
            str(request),
            str(destination),
        ]
        with NativeToolProcess(
            executable,
            command,
            environment=dict(os.environ),
            timeout_ms=21600000,
            scratch_parent=Path(scratch),
        ) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError(
                "native SQLite publication failed: " + completed.stdout
            )
        if report.stat().st_size > 256 * 1024:
            raise ValidationError("native SQLite publication result exceeds bound")
        result = json.loads(report.read_text(encoding="utf-8"))
        if (
            result.get("schema") != "emuflow.native-stage-result/v1"
            or result.get("stage") != "native.publish-store"
            or result.get("status") != "pass"
            or len(result.get("outputs", [])) != 1
            or result["outputs"][0].get("schema") != schema
            or Path(result["outputs"][0].get("path", "")).resolve()
            != destination.resolve()
        ):
            raise ValidationError("native SQLite publication result disagrees")
        return result


def publish_derived_outputs(artifacts, *, sources=()):
    """Publish a bounded group of closed derived files with failure rollback.

    Entries are (staging, destination, SQLite schema or None). Native producers
    have already synchronized plain-file staging. Hard-link recovery names do
    not copy large payloads. This is an invocation boundary, not a persistent
    checkpoint or a multi-file snapshot for concurrent readers; consumers bind
    the final source-sealed report only after the producer succeeds.
    """
    artifacts, sources = tuple(artifacts), tuple(sources)
    if not 1 <= len(artifacts) <= 16:
        raise ValidationError("derived publication output count exceeds bound")
    paths = []
    for temporary, destination, _schema in artifacts:
        temporary, destination = Path(temporary), Path(destination)
        validate_store_destination(temporary, sources)
        validate_store_destination(destination, sources)
        if (temporary.is_symlink() or not temporary.is_file()
                or destination.is_symlink() or destination.is_dir()
                or temporary.parent.resolve() != destination.parent.resolve()):
            raise ValidationError("derived publication requires closed sibling staging")
        paths.extend((temporary, destination))
    for i, path in enumerate(paths):
        for other in paths[:i]:
            if path.resolve() == other.resolve() or (
                path.exists() and other.exists() and os.path.samefile(path, other)
            ):
                raise ValidationError("derived publication paths alias")
    backups, changed = {}, []
    retain_recovery = False
    parents = {Path(destination).parent for _, destination, _ in artifacts}

    def sync():
        for parent in parents:
            descriptor = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    try:
        for _, destination, _ in artifacts:
            destination = Path(destination)
            if destination.exists():
                descriptor, name = tempfile.mkstemp(prefix=".derived-recovery.", dir=destination.parent)
                os.close(descriptor)
                backup = Path(name)
                backups[destination] = backup
                backup.unlink()
                os.link(destination, backup, follow_symlinks=False)
        require_store_sources_unchanged(sources)
        for temporary, destination, schema in artifacts:
            temporary, destination = Path(temporary), Path(destination)
            changed.append(destination)
            if schema is None:
                os.replace(temporary, destination)
            else:
                publish_sqlite_store(temporary, destination, schema, sources=sources)
        require_store_sources_unchanged(sources)
        sync()
    except BaseException:
        try:
            for destination in reversed(changed):
                if destination in backups:
                    os.replace(backups[destination], destination)
                else:
                    destination.unlink(missing_ok=True)
            sync()
        except BaseException:
            retain_recovery = True
            raise
        raise
    finally:
        if not retain_recovery:
            for backup in backups.values():
                backup.unlink(missing_ok=True)


def commit_store_selector(
    path, artifact, sources, payload, created, *, durable=None, validate=None
):
    """Commit a bounded selector; a rejected commit preserves its prior payload.

    If rollback itself fails, retain both generations and the recovery selector
    for diagnosis. An optional independent validator returns the published view;
    its close method, if any, runs before a rejected generation is retired.
    An error never reports a passing complete artifact set.
    """
    sources = tuple(sources)
    if path.resolve() == payload.resolve() or (
        path.exists() and payload.exists() and os.path.samefile(path, payload)
    ):
        raise ValidationError("selector output aliases its payload")
    backup = None
    backup_ready = False
    retain_recovery = False
    checked = None
    had_previous = path.exists()
    try:
        validate_store_destination(path, sources)
        if had_previous:
            descriptor, name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".previous", dir=path.parent
            )
            os.close(descriptor)
            backup = Path(name)
            shutil.copyfile(path, backup)
            with backup.open("rb") as stream:
                os.fsync(stream.fileno())
            backup_ready = True
        require_store_sources_unchanged(sources)
        write_json(path, artifact, compact=True, durable=durable)
        if validate is not None:
            checked = validate()
        require_store_sources_unchanged(sources)
        effective_durable = json_writes_are_durable() if durable is None else durable
        if effective_durable:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return checked
    except BaseException:
        close = getattr(checked, "close", None)
        if callable(close):
            try:
                close()
            except BaseException:
                retain_recovery = True
                raise
        if backup_ready:
            try:
                os.replace(backup, path)
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except BaseException:
                retain_recovery = True
                raise
        elif not had_previous:
            path.unlink(missing_ok=True)
        if created:
            payload.unlink(missing_ok=True)
        raise
    finally:
        if backup is not None and not retain_recovery:
            backup.unlink(missing_ok=True)
