"""Shared DUT implementation targets, independent of the runtime calendar."""
from __future__ import annotations

import math
from typing import Any, Mapping

from .errors import ValidationError

IMPLEMENTATION_CLOCK_POLICY = "shared-dut-target-local-baseline-v1"


def validate_dut_frequency_mhz(value, *, enabled=True):
    """Validate an explicit fastest-root target before starting expensive work."""
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValidationError("Vivado DUT frequency must be finite and positive")
    if not enabled:
        raise ValidationError("Vivado DUT frequency requires Vivado physical execution")
    return float(value)


def implementation_timing_contract(runtime: Mapping[str, Any], dut_frequency_mhz=None) -> dict[str, Any]:
    def period(value, name):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            raise ValidationError(f"Vivado implementation requires an explicit positive target period: {name}")
        return float(value)

    frequency = validate_dut_frequency_mhz(dut_frequency_mhz)
    timing = runtime["timing_model"]
    clocks = timing["dut_clock_ports"]
    if (not clocks or any(not isinstance(name, str) or not name for name in clocks)
            or len(set(clocks)) != len(clocks) or timing["fabric_clock_port"] in clocks):
        raise ValidationError("Vivado implementation clock identities are invalid")
    original = {
        name: period(runtime["virtual_dut_clocks"].get(name, {}).get("target_period_ns"), name)
        for name in clocks
    }
    scale = 1.0 if frequency is None else (1000.0 / frequency) / min(original.values())
    return {
        "policy": IMPLEMENTATION_CLOCK_POLICY,
        "scope": "local-implementation-without-per-cut-budget-feedback",
        "tdm_clock": {"port": timing["fabric_clock_port"],
                      "period_ns": period(runtime["fabric_clock"].get("period_ns"), "tdm")},
        "source_dut_clock_periods_ns": original,
        "dut_frequency_mhz_override": frequency,
        "dut_period_scale": period(scale, "DUT period scale"),
        "dut_clock_periods_ns": {name: period(value * scale, name) for name, value in original.items()},
    }


def implementation_clocks(runtime: Mapping[str, Any], ports, dut_frequency_mhz=None):
    """Keep root identities and relative periods identical across partitions."""
    contract = implementation_timing_contract(runtime, dut_frequency_mhz)
    tdm = contract["tdm_clock"]
    if tdm["port"] not in ports:
        raise ValidationError("Vivado physical partition lacks required clock ports: " + tdm["port"])
    return [("emuflow_tdm_clk", tdm["port"], tdm["period_ns"]), *(
        (f"emuflow_dut_clk_{index}", port, period)
        for index, (port, period) in enumerate(contract["dut_clock_periods_ns"].items()) if port in ports
    )]
