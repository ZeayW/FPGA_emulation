"""Validated board-link delays for the native physical event binder."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional
from .board_link_timing import validate_board_link_timing
from .platform import Platform


def _board_link_delay_database(
    physical_summary: Mapping[str, Any], platform: Platform
) -> Optional[Dict[tuple[str, str, str], float]]:
    raw = physical_summary.get("board_link_timing")
    if raw is None:
        return None
    validate_board_link_timing(raw, platform)
    return {
        (item["link"], item["from"], item["to"]): float(
            item["delay_bound_ns"]
        )
        for item in raw["links"]
    }
