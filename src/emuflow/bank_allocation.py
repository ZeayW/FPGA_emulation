"""Deterministic integer direction allocation for shared inter-FPGA banks."""

from __future__ import annotations

from typing import Any, Mapping, Optional


def allocate_shared_banks(
    forward_signals: int,
    reverse_signals: int,
    bank_count: int,
    wires_per_bank: int,
    *,
    min_ratio: int = 1,
    ratio_quantum: int = 8,
    max_ratio: Optional[int] = None,
) -> Optional[dict[str, int]]:
    """Allocate banks by worst/total ratio, then unquantized wire pressure.

    The estimate is a load-based prediction, not a concrete slot/readiness
    certificate.  A supplied maximum rejects infeasible splits rather than
    clipping their ratios and hiding excess load.  With no maximum, callers
    can retain over-budget pressure as a prediction instead of a hard guard.
    """
    for name, value, minimum in (
        ("forward_signals", forward_signals, 0),
        ("reverse_signals", reverse_signals, 0),
        ("bank_count", bank_count, 1),
        ("wires_per_bank", wires_per_bank, 1),
        ("min_ratio", min_ratio, 1),
        ("ratio_quantum", ratio_quantum, 1),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    if max_ratio is not None and (
        isinstance(max_ratio, bool) or not isinstance(max_ratio, int)
        or max_ratio < min_ratio
    ):
        raise ValueError("max_ratio must be an integer >= min_ratio")

    def ratio(signals: int, banks: int) -> Optional[int]:
        if signals == 0:
            return 1
        if banks == 0:
            return None
        lanes = banks * wires_per_bank
        raw = max(min_ratio, (signals + lanes - 1) // lanes)
        value = 1 if raw == 1 else (
            (raw + ratio_quantum - 1) // ratio_quantum * ratio_quantum
        )
        return value if max_ratio is None or value <= max_ratio else None

    best = None
    for forward in range(bank_count + 1):
        reverse = bank_count - forward
        forward_ratio = ratio(forward_signals, forward)
        reverse_ratio = ratio(reverse_signals, reverse)
        if forward_ratio is None or reverse_ratio is None:
            continue
        forward_pressure = forward_signals / (forward * wires_per_bank) if forward else 0.0
        reverse_pressure = reverse_signals / (reverse * wires_per_bank) if reverse else 0.0
        objective = (max(forward_ratio, reverse_ratio),
                     forward_ratio + reverse_ratio,
                     max(forward_pressure, reverse_pressure),
                     forward_pressure + reverse_pressure, forward)
        if best is None or objective < best[0]:
            best = (objective, {
                "forward_banks": forward,
                "reverse_banks": reverse,
                "forward_lanes": forward * wires_per_bank,
                "reverse_lanes": reverse * wires_per_bank,
                "forward_ratio": forward_ratio,
                "reverse_ratio": reverse_ratio,
            })
    return None if best is None else best[1]


def apply_bank_domain_ratios(
    capacities: Mapping[str, Mapping[str, Any]],
    loads: Mapping[str, int],
    ratios: dict[str, int],
    constraints: Mapping[str, Any],
) -> dict[str, int]:
    """Replace pooled estimates with paired, integer-bank predictions."""
    result = dict(ratios)
    for key, capacity in capacities.items():
        opposite = capacity.get("opposite_capacity_key")
        if not capacity.get("bank_group") or key >= opposite:
            continue
        allocation = allocate_shared_banks(
            loads[key], loads[opposite], capacity["bank_count"],
            capacity["wires_per_bank"],
            min_ratio=int(constraints.get("tdm_min_ratio", 1)),
            ratio_quantum=int(constraints["tdm_ratio_quantum"]),
        )
        if allocation is None:
            # Both directions active with fewer than two banks.  Preserve an
            # over-budget prediction; downstream routing/slot validation owns
            # authoritative feasibility.
            result[key] = result[opposite] = int(constraints["frame_slots"]) + 1
        else:
            result[key] = allocation["forward_ratio"]
            result[opposite] = allocation["reverse_ratio"]
    return result
