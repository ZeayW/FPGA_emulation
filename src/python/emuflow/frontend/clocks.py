"""Finite command-line clock configuration."""
from __future__ import annotations
from typing import Dict, Iterable
from emuflow.common.errors import ValidationError

def parse_clock_definitions(values: Iterable[str]) -> Dict[str, float]:
    clocks: Dict[str, float] = {}
    for value in values:
        name, separator, raw_period = value.partition("=")
        if not separator or not name or not raw_period:
            raise ValidationError(
                f"--clock-period: expected CLOCK=PERIOD_NS, got {value!r}"
            )
        if name in clocks:
            raise ValidationError(
                f"--clock-period: duplicate clock {name!r}"
            )
        try:
            clocks[name] = float(raw_period)
        except ValueError as error:
            raise ValidationError(
                f"--clock-period: invalid period in {value!r}"
            ) from error
    return clocks
