"""Read the current control envelope emitted by the native manifest owner."""

from __future__ import annotations

import base64
import json
import zlib
from pathlib import Path
from typing import Any, Dict, Mapping


MANAGED_JSON_STORAGE_SCHEMA = "emuflow.managed-json-storage/v1"
MANAGED_JSON_STORAGE_CODEC = "zlib-level-1-base64-canonical-json"




def expand_managed_json(value: Mapping[str, Any], path: Path) -> Dict[str, Any]:
    if (
        value.get("schema") != MANAGED_JSON_STORAGE_SCHEMA
        or value.get("codec") != MANAGED_JSON_STORAGE_CODEC
        or not isinstance(value.get("logical_schema"), str)
        or not value["logical_schema"]
        or isinstance(value.get("uncompressed_bytes"), bool)
        or not isinstance(value.get("uncompressed_bytes"), int)
        or value["uncompressed_bytes"] < 0
        or not isinstance(value.get("data"), str)
    ):
        raise ValueError(f"{path}: managed JSON storage header is malformed")
    expected_size = value["uncompressed_bytes"]
    try:
        encoded = base64.b64decode(value["data"], validate=True)
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(encoded, expected_size + 1)
        if len(raw) > expected_size or decompressor.unconsumed_tail:
            raise ValueError("compressed length exceeds declaration")
        raw += decompressor.flush(expected_size - len(raw) + 1)
        if (
            len(raw) != expected_size
            or not decompressor.eof
            or decompressor.unused_data
        ):
            raise ValueError("compressed length is inconsistent")
        result = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, zlib.error, json.JSONDecodeError) as error:
        raise ValueError(f"{path}: managed JSON storage is corrupt") from error
    if (
        not isinstance(result, dict)
        or result.get("schema") != value["logical_schema"]
    ):
        raise ValueError(f"{path}: managed JSON logical schema disagrees")
    return result
