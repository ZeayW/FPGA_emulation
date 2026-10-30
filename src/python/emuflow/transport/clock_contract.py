"""Finite clock-event oracle for bounded Phase 6 behavioral qualification."""
from __future__ import annotations

import math
from fractions import Fraction
from functools import reduce
from math import gcd
from typing import Any, Dict, Mapping, Optional

from emuflow.common.errors import ValidationError


def _lcm(left: int, right: int) -> int:
    return abs(left * right) // gcd(left, right)



def build_virtual_clock_contract(
    clock_periods: Optional[Mapping[str, float]],
    frame_period_ns: float,
) -> Dict[str, Any]:
    """Build a finite, exact root-clock event calendar.

    One complete transport frame advances the original DUT time by one
    ``timebase_period_ns`` quantum.  A root clock receives one complete pulse
    only on the frames listed in ``events``.  This preserves relative clock
    rates instead of silently collapsing every declared clock into one edge.

    The public compile path always supplies the declared clock periods.  The
    synthetic single ``clk`` default is retained only for low-level callers
    that construct a schedule directly in unit tests.
    """

    periods = dict(clock_periods or {"clk": 1.0})
    if not periods:
        raise ValidationError("virtual runtime requires at least one DUT clock")
    normalized: Dict[str, Fraction] = {}
    for clock, raw_period in sorted(periods.items()):
        if (
            not isinstance(clock, str)
            or not clock
            or isinstance(raw_period, bool)
            or not isinstance(raw_period, (int, float))
            or not math.isfinite(float(raw_period))
            or float(raw_period) <= 0.0
        ):
            raise ValidationError("virtual runtime DUT clock periods are invalid")
        normalized[clock] = Fraction(str(float(raw_period)))
    denominator = reduce(_lcm, (item.denominator for item in normalized.values()))
    period_units = {
        clock: int(period * denominator)
        for clock, period in normalized.items()
    }
    quantum_units = reduce(gcd, period_units.values())
    period_ticks = {
        clock: units // quantum_units for clock, units in period_units.items()
    }
    calendar_ticks = reduce(_lcm, period_ticks.values())
    if calendar_ticks > 4096:
        raise ValidationError(
            "virtual runtime multi-clock event calendar exceeds 4096 ticks"
        )
    # Keep every timebase tick, including ticks on which no root clock has an
    # edge.  A complete transport frame advances time by exactly one quantum;
    # dropping an empty tick would compress elapsed time for relatively-prime
    # periods such as 20 ns and 30 ns (their 10 ns tick 1 has no edge).
    events = [
        {
            "offset_ticks": tick,
            "clocks": [
                clock
                for clock, ticks in sorted(period_ticks.items())
                if tick % ticks == 0
            ],
        }
        for tick in range(1, calendar_ticks + 1)
    ]
    quantum = Fraction(quantum_units, denominator)
    clocks = {
        clock: {
            "target_period_ns": float(normalized[clock]),
            "period_ticks": period_ticks[clock],
            "nominal_period_ns": period_ticks[clock] * frame_period_ns,
            "nominal_frequency_mhz": (
                1000.0 / (period_ticks[clock] * frame_period_ns)
            ),
            "phase_ticks": 0,
            "edge": "complete-pulse-per-calendar-event",
        }
        for clock in sorted(normalized)
    }
    fastest_period = min(item["nominal_period_ns"] for item in clocks.values())
    return {
        "timebase_period_ns": float(quantum),
        "calendar_ticks": calendar_ticks,
        "events": events,
        "clocks": clocks,
        "fastest_nominal_period_ns": fastest_period,
    }
