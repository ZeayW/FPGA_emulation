"""Canonical identity for the frontend timing-I/O contract."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

def timing_io_sha256(value: Any) -> Optional[str]:
    """Return the canonical identity of a benchmark timing-I/O contract.

    The benchmark run remains the canonical owner. Timing producers persist
    only this compact identity so a holdout assembler can prove that PPro and
    EmuFlow consumed the same environment without duplicating all port groups.
    """

    if value is None:
        return None
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
