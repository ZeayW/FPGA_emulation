"""Bounded native-stage control object parsing."""
import json
from pathlib import Path

from emuflow.common.errors import ValidationError


def read_small_object(path, maximum=8 * 1024 * 1024):
    path = Path(path)
    if path.stat().st_size > maximum:
        raise ValidationError("physical handoff control document exceeds bound")

    def unique(rows):
        value = {}
        for key, item in rows:
            if key in value:
                raise ValidationError("physical handoff control document repeats a key")
            value[key] = item
        return value

    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValidationError("physical handoff control document must be an object")
    return value
