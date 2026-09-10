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


def qualify_snapshot_clock_coverage(routed, report, *, top="top", clock_port="clk_25mhz"):
    """Check the fixed snapshot board's physical FF clock connectivity.

    One external oscillator, one ungated DCCA, positive-edge TRELLIS_FF state.
    This checks reported-clock coverage, NOT setup/hold path coverage or CDC.
    Unexpected hard blocks or inferred LUT RAM require a separate adapter.
    """
    qualify_ecp5_endpoint_report(report)
    if any(report["utilization"][name]["used"] for name in ("DP16KD","MULT18X18D")):
        raise ValidationError("hard-block clock coverage is not implemented")
    module=routed.get("modules",{}).get(top)
    if not isinstance(module,dict): raise ValidationError("missing routed clock top")
    cells=module.get("cells",{}); nets=module.get("netnames",{})
    port=module.get("ports",{}).get(clock_port,{})
    def bit(bits):
        if not isinstance(bits,list) or len(bits)!=1 or type(bits[0]) is not int or bits[0]<0:
            raise ValidationError("clock binding requires one connected physical bit")
        return bits[0]
    if port.get("direction")!="input": raise ValidationError("missing physical oscillator input")
    pad=bit(port.get("bits"))
    clocks=report["fmax"]
    if len(clocks)!=1: raise ValidationError("snapshot board requires exactly one reported clock")
    clock_name=next(iter(clocks)); clock=bit(nets.get(clock_name,{}).get("bits"))
    buffers=[c for c in cells.values() if c.get("type")=="DCCA" and c.get("connections",{}).get("CLKO")==[clock]]
    if len(buffers)!=1: raise ValidationError("reported clock has no unique DCCA driver")
    buffer=buffers[0]; connections=buffer["connections"]
    if connections.get("CE",[]) not in ([],["1"]):
        raise ValidationError("snapshot oscillator must not be gated")
    input_wire=bit(connections.get("CLKI"))
    pads=[c for c in cells.values() if c.get("type")=="TRELLIS_IO" and
          c.get("connections",{}).get("O")==[input_wire] and
          c.get("connections",{}).get("B")==[pad] and
          c.get("parameters",{}).get("DIR")=="INPUT"]
    if len(pads)!=1: raise ValidationError("clock buffer is not bound to the oscillator pad")
    count=0
    for name,cell in cells.items():
        kind=cell.get("type")
        if kind not in {"TRELLIS_FF","TRELLIS_COMB","TRELLIS_IO","DCCA","GSR"}:
            raise ValidationError(f"unqualified clock/state primitive: {kind}")
        if kind=="TRELLIS_COMB" and cell.get("parameters",{}).get("MODE","LOGIC") not in {"LOGIC","CCU2"}:
            raise ValidationError("LUT memory clock coverage is not implemented")
        if kind=="TRELLIS_FF":
            if cell.get("connections",{}).get("CLK")!=[clock] or cell.get("parameters",{}).get("CLKMUX")!="CLK":
                raise ValidationError(f"FF outside the reported positive-edge clock: {name}")
            count+=1
    if not count or count!=report["utilization"]["TRELLIS_FF"]["used"]:
        raise ValidationError("routed FF count disagrees with physical report")
    return {"status":"pass","scope":"snapshot_ff_clock_connectivity",
            "clock":clock_name,"ff_count":count,"ff_clock_coverage_qualified":True,
            "timing_path_coverage_qualified":False,"cdc_qualified":False,
            "global_timing_qualified":False}
