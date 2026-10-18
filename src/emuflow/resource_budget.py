"""Whole-device resource budgets, independent of placement region geometry."""

from __future__ import annotations

import math
from typing import Any, Mapping

from .errors import ValidationError


def validate_materialized_resource_counts(
    mapped: Mapping[str, int], physical: Mapping[str, int],
    materialization: Mapping[str, Any],
) -> None:
    """Check device-unit counts against the audited primitive transformations."""
    fields = ("lut", "ff", "bram18k", "dsp48", "carry8", "uram288")
    expected = {field: mapped.get(field, 0) for field in fields}
    # A direct clock gate becomes BUFGCE; a prefiltered gate retains its LUT.
    # DSP components remain in one DSP site; LUT6_2 halves share one CLB LUT.
    expected["lut"] -= (
        materialization.get("dedicated_clock_gate_cells", 0)
        - materialization.get("fabric_prefilter_clock_gate_cells", 0)
    )
    if dict(physical) != expected:
        raise ValidationError(
            f"physical resource inventory disagrees with audited mapped circuit: "
            f"expected={expected}, observed={dict(physical)}"
        )


def validate_device_resource_usage(
    counts: Mapping[str, int], budget: Mapping[str, Any], *, stage: str,
) -> dict[str, Any]:
    capacity = budget.get("capacity")
    limit = budget.get("utilization_limit")
    if (not isinstance(capacity, Mapping) or not capacity
        or isinstance(limit, bool) or not isinstance(limit, (int, float))
        or not 0.0 < limit <= 1.0):
        raise ValidationError("physical device resource budget is invalid")
    if stage == "post-physical-materialization" and not set(capacity).issubset(counts):
        raise ValidationError("physical resource inventory has missing resource dimensions")
    used, allowed = {}, {}
    for field, total in capacity.items():
        count = counts.get(field, 0)
        if (isinstance(total, bool) or not isinstance(total, int) or total < 0
            or isinstance(count, bool) or not isinstance(count, int) or count < 0):
            raise ValidationError(f"physical resource {field} count/capacity is invalid")
        used[field] = count
        allowed[field] = math.floor(total * limit)
        if count > allowed[field]:
            raise ValidationError(
                f"{stage}: {budget.get('id')} exceeds strict device resource budget: "
                f"{field} used={count}, allowed={allowed[field]}, "
                f"raw_capacity={total}, utilization_limit={limit:g}; "
                "automatic relaxation is forbidden"
            )
    if budget.get("effective_capacity", allowed) != allowed:
        raise ValidationError("physical effective capacity disagrees with raw device budget")
    return {
        "schema": "emuflow.device-resource-usage/v1", "status": "pass",
        "stage": stage, "fpga": budget.get("id"), "part": budget.get("part"),
        "capacity_basis": "whole-device-raw-capacity",
        "capacity": dict(capacity), "utilization_limit": float(limit),
        "allowed": allowed, "counts": used,
        "device_utilization": {field: used[field] / total if total else 0.0
                               for field, total in capacity.items()},
        "budget_utilization": {field: used[field] / count if count else 0.0
                               for field, count in allowed.items()},
    }
