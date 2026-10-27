"""Strict integer resource and balance bounds shared by partition providers."""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from .errors import ValidationError


def planning_resource_capacities(
    fpgas: Sequence[Any],
    resource_reservations_by_fpga: Optional[
        Mapping[str, Mapping[str, int]]
    ] = None,
) -> dict[str, dict[str, int]]:
    """Subtract sealed downstream reservations from Phase 3 capacities.

    ``effective_capacity`` remains the physical device limit.  The returned
    capacity is only the amount that the original design may consume before
    transport and boundary logic are added later in the flow.
    """

    reservations = resource_reservations_by_fpga or {}
    fpga_by_id = {fpga.id: fpga for fpga in fpgas}
    unknown_fpgas = sorted(set(reservations) - set(fpga_by_id))
    if unknown_fpgas:
        raise ValidationError(
            "resource reservations reference unknown/inactive FPGAs "
            f"{unknown_fpgas}"
        )
    result: dict[str, dict[str, int]] = {}
    for fpga_id, fpga in fpga_by_id.items():
        row = reservations.get(fpga_id, {})
        unknown_fields = sorted(set(row) - set(fpga.effective_capacity))
        if unknown_fields:
            raise ValidationError(
                f"resource reservations for {fpga_id!r} reference unsupported "
                f"resources {unknown_fields}"
            )
        capacity: dict[str, int] = {}
        for field, physical in fpga.effective_capacity.items():
            reserved = row.get(field, 0)
            if (
                isinstance(reserved, bool)
                or not isinstance(reserved, int)
                or reserved < 0
                or reserved > physical
            ):
                raise ValidationError(
                    f"resource reservation {fpga_id}.{field}={reserved!r} "
                    f"must be an integer in [0, {physical}]"
                )
            capacity[field] = physical - reserved
        result[fpga_id] = capacity
    return result


def strict_balance_limits(
    fpgas: Sequence[Any], totals: Mapping[str, int], tolerance: float,
    overrides: Optional[Mapping[str, float]] = None,
    balance_dimensions: Optional[Iterable[str]] = (),
    resource_reservations_by_fpga: Optional[
        Mapping[str, Mapping[str, int]]
    ] = None,
) -> dict[str, dict[str, int]]:
    """Default to device bounds; explicit None reads historical all-field limits."""
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
    planning_capacity = planning_resource_capacities(
        fpgas, resource_reservations_by_fpga
    )
    for field, total in totals.items():
        capacity_total = sum(fpga.effective_capacity.get(field, 0) for fpga in fpgas)
        for fpga in fpgas:
            if field not in balanced:
                limits[fpga.id][field] = (
                    total
                    if field == "cells"
                    else planning_capacity[fpga.id].get(field, 0)
                )
                continue
            share = 1.0 / len(fpgas) if field == "cells" else (
                fpga.effective_capacity.get(field, 0) / capacity_total
                if capacity_total else 0.0
            )
            allowed = math.floor(total * share * (1.0 + (overrides or {}).get(field, tolerance)) + 1e-9)
            if field != "cells":
                allowed = min(allowed, planning_capacity[fpga.id].get(field, 0))
            limits[fpga.id][field] = allowed
    return limits
