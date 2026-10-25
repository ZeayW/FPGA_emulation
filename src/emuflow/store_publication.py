"""Bounded handoff of closed SQLite staging to the shared native publisher."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile

from .errors import ValidationError
from .io import file_sha256
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
