"""Strict integer resource and balance bounds shared by partition providers."""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from .errors import ValidationError


def strict_balance_limits(
    fpgas: Sequence[Any], totals: Mapping[str, int], tolerance: float,
    overrides: Optional[Mapping[str, float]] = None,
    balance_dimensions: Optional[Iterable[str]] = None,
) -> dict[str, dict[str, int]]:
    if not fpgas:
        raise ValidationError("strict balance needs at least one FPGA")
    for value in [tolerance, *(overrides or {}).values()]:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValidationError("strict balance tolerance must be finite and non-negative")
    balanced = (
        set(totals)
        if balance_dimensions is None
        else set(balance_dimensions)
    )
    balanced &= set(totals)
    limits = {fpga.id: {} for fpga in fpgas}
    for field, total in totals.items():
        capacity_total = sum(fpga.effective_capacity.get(field, 0) for fpga in fpgas)
        for fpga in fpgas:
            if field not in balanced:
                limits[fpga.id][field] = (
                    total
                    if field == "cells"
                    else fpga.effective_capacity.get(field, 0)
                )
                continue
            share = 1.0 / len(fpgas) if field == "cells" else (
                fpga.effective_capacity.get(field, 0) / capacity_total
                if capacity_total else 0.0
            )
            allowed = math.floor(total * share * (1.0 + (overrides or {}).get(field, tolerance)) + 1e-9)
            if field != "cells":
                allowed = min(allowed, fpga.effective_capacity.get(field, 0))
            limits[fpga.id][field] = allowed
    return limits


def validate_indivisible_limits(
    records: Iterable[Tuple[str, Mapping[str, int], Optional[str]]],
    limits: Mapping[str, Mapping[str, int]],
) -> None:
    """Reject necessary infeasibility without changing any configured bound."""
    fields = tuple(next(iter(limits.values())))
    totals = {field: 0 for field in fields}
    fixed = {fpga: {field: 0 for field in fields} for fpga in limits}
    for identity, weights, fixed_fpga in records:
        candidates = (fixed_fpga,) if fixed_fpga is not None else limits
        if not any(all(weights.get(field, 0) <= limits[fpga][field] for field in fields)
                   for fpga in candidates):
            raise ValidationError(
                f"strict balance/capacity infeasible: indivisible cluster {identity!r}, "
                f"fixed_fpga={fixed_fpga!r}, resources={dict(weights)!r}; "
                "automatic relaxation is forbidden"
            )
        for field in fields:
            totals[field] += weights.get(field, 0)
            if fixed_fpga is not None:
                fixed[fixed_fpga][field] += weights.get(field, 0)
    for field in fields:
        if totals[field] > sum(bound[field] for bound in limits.values()):
            raise ValidationError(f"strict balance/capacity infeasible: total {field} demand {totals[field]} exceeds integer bounds; automatic relaxation is forbidden")
        for fpga in limits:
            if fixed[fpga][field] > limits[fpga][field]:
                raise ValidationError(f"strict balance/capacity infeasible: fixed {fpga} {field} demand {fixed[fpga][field]} exceeds {limits[fpga][field]}; automatic relaxation is forbidden")
