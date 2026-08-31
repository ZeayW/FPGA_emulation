from __future__ import annotations

import json
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
    with path.open("r", encoding="utf-8") as stream:
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
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(
                value,
                stream,
                indent=None if compact else 2,
                separators=(",", ":") if compact else None,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            effective_durable = (
                _DURABLE_WRITES.get() if durable is None else durable
            )
            if effective_durable:
                os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
