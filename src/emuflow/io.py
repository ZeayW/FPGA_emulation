from __future__ import annotations

import gzip
import hashlib
import io
import json
from functools import lru_cache
import os
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict


_DURABLE_WRITES = ContextVar("emuflow_durable_json_writes", default=True)


@contextmanager
def json_write_policy(*, durable: bool):
    """Select durability for a managed staging transaction.

    Managed DAG outputs are atomically renamed and then sealed by the cache
    publisher.  Per-artifact fsync inside that disposable staging directory is
    redundant; the final checkpoint publication remains durable.
    """
    token = _DURABLE_WRITES.set(durable)
    try:
        yield
    finally:
        _DURABLE_WRITES.reset(token)


def read_json(path: Path) -> Dict[str, Any]:
    stream = (
        gzip.open(path, "rt", encoding="utf-8")
        if path.suffix == ".gz"
        else path.open("r", encoding="utf-8")
    )
    with stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object at the document root")
    schema = value.get("schema")
    if schema == "emuflow.managed-json-storage/v1":
        from .managed_json_storage import expand_managed_json

        return expand_managed_json(value, path)
    if schema in {
        "emuflow.phase3-clusters-storage/v1",
        "emuflow.phase3-assignment-storage/v1",
    }:
        from .phase3_storage import (
            PACKED_ASSIGNMENT_SCHEMA,
            expand_phase3_assignment,
            expand_phase3_clusters,
        )

        if schema == PACKED_ASSIGNMENT_SCHEMA:
            return expand_phase3_assignment(value, path, read_json)
        return expand_phase3_clusters(value)
    return value


def write_json(
    path: Path,
    value: Dict[str, Any],
    *,
    compact: bool = False,
    durable: bool | None = None,
    sort_keys: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{uuid.uuid4().hex}.tmp"
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o666,
    )
    try:
        binary_stream = os.fdopen(descriptor, "wb")
        descriptor = -1
        if path.suffix == ".gz":
            compressed_stream = gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=1,
                fileobj=binary_stream,
                mtime=0,
            )
            stream = io.TextIOWrapper(compressed_stream, encoding="utf-8")
        else:
            stream = io.TextIOWrapper(binary_stream, encoding="utf-8")
        effective_durable = (
            _DURABLE_WRITES.get() if durable is None else durable
        )
        with stream:
            json.dump(
                value,
                stream,
                indent=None if compact else 2,
                separators=(",", ":") if compact else None,
                sort_keys=sort_keys,
            )
            stream.write("\n")
            stream.flush()
        if effective_durable:
            with temporary.open("rb") as sync_stream:
                os.fsync(sync_stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
@lru_cache(maxsize=128)
def _sha256_for_identity(
    path: str,
    device: int,
    inode: int,
    size: int,
    mtime_ns: int,
    ctime_ns: int,
) -> str:
    """Hash one immutable file identity once per process.

    The complete stat identity is deliberately part of the cache key.  A
    replacement, truncation, or in-place rewrite therefore cannot reuse the
    digest of the previous artifact.  This cache removes repeated multi-hundred
    MiB reads from validation hot paths without weakening source seals.
    """

    del device, inode, size, mtime_ns, ctime_ns
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    """Return a stat-invalidated, process-local SHA-256 digest."""

    resolved = path.resolve()
    stat = resolved.stat()
    return _sha256_for_identity(
        str(resolved),
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )
