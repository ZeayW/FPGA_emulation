from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict


def read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object at the document root")
    schema = value.get("schema")
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
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
