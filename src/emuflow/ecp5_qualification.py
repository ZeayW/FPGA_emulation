"""Strict, scoped interpretation of nextpnr ECP5 physical reports.

Device database availability is not a PCB I/O budget. Timing here is local
clock timing only; no original-path/global or external I/O claim is derived.
"""
import math
from collections.abc import Mapping

from .errors import ValidationError

# Physical primitives, not aliases for LUT6, AMD RAMB18 or DSP48 resources.
# LFE5U-85F nextpnr/Trellis device inventory; package/board I/O is separate.
ECP5_85F_CAPACITY = {
    "TRELLIS_COMB": 83640,
    "TRELLIS_FF": 83640,
    "DP16KD": 208,
    "MULT18X18D": 156,
}


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{name} must be numeric")
    if not math.isfinite(value) or value <= 0:
        raise ValidationError(f"{name} must be finite and positive")
    return value


def qualify_ecp5_endpoint_report(report: Mapping, *, required_mhz=25.0,
                                  utilization_limit=0.75) -> dict:
    """Check device resource accounting and every reported clock constraint.

    No inferred zero violations: missing fields cause failure. This does not
    establish that all intended clocks or asynchronous paths were constrained.
    """
    _number(required_mhz, "required_mhz")
    _number(utilization_limit, "utilization_limit")
    if utilization_limit > 1:
        raise ValidationError("utilization_limit must not exceed one")
    if not isinstance(report, Mapping):
        raise ValidationError("physical report must be an object")
    resources = report.get("utilization")
    clocks = report.get("fmax")
    if not isinstance(resources, Mapping) or not isinstance(clocks, Mapping) or not clocks:
        raise ValidationError("physical report requires resource and clock accounting")
    for name, expected in ECP5_85F_CAPACITY.items():
        record = resources.get(name)
        if not isinstance(record, Mapping) or record.get("available") != expected:
            raise ValidationError(f"wrong or missing LFE5U-85F capacity: {name}")
    for name, record in resources.items():
        if not isinstance(record, Mapping):
            raise ValidationError(f"invalid resource record: {name}")
        used, available = record.get("used"), record.get("available")
        if type(used) is not int or type(available) is not int:
            raise ValidationError(f"nonintegral resource accounting: {name}")
        if used < 0 or available < 0 or used > available:
            raise ValidationError(f"invalid physical resource count: {name}")
        # Apply 75% to divisible DUT+transport logic/memory/DSP resources.
        # A single required clock buffer is not a fractional allocatable DUT.
        if name in ECP5_85F_CAPACITY and used > math.floor(available * utilization_limit):
            raise ValidationError(f"DUT plus transport exceeds utilization limit: {name}")
    for name, record in clocks.items():
        if not isinstance(record, Mapping):
            raise ValidationError(f"invalid clock record: {name}")
        achieved = _number(record.get("achieved"), f"{name}.achieved")
        constraint = _number(record.get("constraint"), f"{name}.constraint")
        if constraint < required_mhz or achieved < constraint:
            raise ValidationError(f"local clock constraint not satisfied: {name}")
    return {"status": "pass", "scope": "device_resources_and_reported_local_clocks",
            "utilization_limit": utilization_limit, "reported_clocks": len(clocks),
            "external_timing_qualified": False, "global_timing_qualified": False,
            "intended_clock_coverage_qualified": False}
