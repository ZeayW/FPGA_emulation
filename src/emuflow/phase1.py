from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .io import write_json
from .ir import EmuIR
from .platform import Platform
from .yosys import import_yosys_json


PHASE1_REPORT_SCHEMA = "emuflow.phase1-report/v1"


def _clock_pin(instance_type: str, port: str) -> bool:
    if instance_type.startswith("FD"):
        return port == "C"
    if instance_type.startswith("$_DFF_"):
        return port == "C"
    return instance_type in {"VTR_SP_RAM", "VTR_DP_RAM"} and port == "clk"


def _fabric_logic(instance_type: str) -> bool:
    return instance_type.startswith("LUT") or instance_type == "$lut"


def analyze_clock_topology(ir: EmuIR) -> Dict[str, Any]:
    instance_types = {
        instance["id"]: instance["type"]
        for instance in ir.value["instances"]
    }
    clock_nets = 0
    fabric_clock_nets = []
    for net in ir.value["nets"]:
        clock_sinks = [
            endpoint
            for endpoint in net["sinks"]
            if endpoint["instance"] is not None
            and _clock_pin(
                instance_types[endpoint["instance"]], endpoint["port"]
            )
        ]
        if not clock_sinks:
            continue
        clock_nets += 1
        lut_drivers = sorted(
            endpoint["instance"]
            for endpoint in net["drivers"]
            if endpoint["instance"] is not None
            and _fabric_logic(instance_types[endpoint["instance"]])
        )
        if lut_drivers:
            fabric_clock_nets.append(
                {
                    "net": net["id"],
                    "lut_drivers": lut_drivers,
                    "clocked_ffs": len(clock_sinks),
                }
            )
    clocked_ffs = sum(
        item["clocked_ffs"] for item in fabric_clock_nets
    )
    return {
        "status": "pass" if not fabric_clock_nets else "unsafe",
        "ff_clock_nets": clock_nets,
        "fabric_logic_clock_nets": len(fabric_clock_nets),
        "fabric_logic_clocked_ffs": clocked_ffs,
        "maximum_fabric_clock_fanout": max(
            (item["clocked_ffs"] for item in fabric_clock_nets),
            default=0,
        ),
        "fabric_clock_details": fabric_clock_nets,
    }


def run_phase1(
    yosys_json: Path,
    platform_path: Path,
    output_dir: Path,
    top: Optional[str] = None,
    clocks: Iterable[str] = (),
    require_no_fabric_clock: bool = False,
) -> Dict[str, Any]:
    platform = Platform.load(platform_path)
    ir = import_yosys_json(yosys_json, top=top, clocks=clocks)
    totals = ir.resource_totals()

    fit_by_fpga = {
        fpga.id: totals.fits_capacity(fpga.effective_capacity)
        for fpga in platform.fpgas
    }
    capacity_fields = sorted(
        {
            resource
            for fpga in platform.fpgas
            for resource in fpga.effective_capacity
        }
    )
    aggregate_capacity = {
        resource: sum(
            fpga.effective_capacity.get(resource, 0)
            for fpga in platform.fpgas
        )
        for resource in capacity_fields
    }
    fits_on_platform = totals.fits_capacity(aggregate_capacity)
    single_fpga_fit = any(fit_by_fpga.values())
    cut_classes = Counter(net["cut_class"] for net in ir.value["nets"])
    clock_topology = analyze_clock_topology(ir)
    status = (
        "capacity_error"
        if not fits_on_platform
        else "clock_topology_error"
        if (
            require_no_fabric_clock
            and clock_topology["fabric_logic_clock_nets"] != 0
        )
        else "pass"
    )
    report: Dict[str, Any] = {
        "schema": PHASE1_REPORT_SCHEMA,
        "phase": 1,
        "status": status,
        "design": ir.value["design"]["name"],
        "platform": platform.name,
        "resource_totals": totals.to_dict(include_zeros=False),
        "fits_on_fpga": fit_by_fpga,
        "fits_on_platform": fits_on_platform,
        "fit_scope": (
            "single_fpga"
            if single_fpga_fit
            else "aggregate_platform"
            if fits_on_platform
            else None
        ),
        "aggregate_effective_capacity": aggregate_capacity,
        "cut_classes": dict(sorted(cut_classes.items())),
        "clock_topology": clock_topology,
        "require_no_fabric_clock": require_no_fabric_clock,
        "warnings": list(ir.value.get("warnings", [])),
        "artifacts": {
            "emuir": "design.emuir.sqlite",
            "platform": "platform.normalized.json",
            "report": "phase1_report.json",
        },
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    ir.save(output_dir / "design.emuir.sqlite")
    write_json(output_dir / "platform.normalized.json", platform.to_dict())
    write_json(output_dir / "phase1_report.json", report)
    return report
