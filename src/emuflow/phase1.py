from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .emuir_storage import open_emuir_sqlite_read_only
from .io import write_json
from .ir import EmuIR
from .platform import Platform
from .resources import ResourceVector
from .yosys import import_yosys_json_indexed


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


def analyze_indexed_clock_topology(ir_path: Path) -> Dict[str, Any]:
    """Compute the Phase 1 clock audit without materializing the graph."""

    connection = open_emuir_sqlite_read_only(ir_path)
    try:
        sink_rows = connection.execute(
            """
            SELECT n.seq AS net_seq, n.id AS net_id, e.port, i.type
              FROM nets AS n
              JOIN endpoints AS e
                ON e.net_seq = n.seq AND e.role = 1
              JOIN instances AS i ON i.id = e.instance_id
             ORDER BY n.seq, e.ordinal
            """
        )
        current_net = None
        current_id = None
        clock_sinks = 0
        clock_nets = 0
        fabric_clock_nets = []

        def finish_net() -> None:
            nonlocal clock_nets
            if current_net is None or clock_sinks == 0:
                return
            clock_nets += 1
            lut_drivers = sorted(
                row["instance_id"]
                for row in connection.execute(
                    """
                    SELECT e.instance_id, i.type
                      FROM endpoints AS e
                      JOIN instances AS i ON i.id = e.instance_id
                     WHERE e.net_seq = ? AND e.role = 0
                     ORDER BY e.ordinal
                    """,
                    (current_net,),
                )
                if _fabric_logic(row["type"])
            )
            if lut_drivers:
                fabric_clock_nets.append(
                    {
                        "net": current_id,
                        "lut_drivers": lut_drivers,
                        "clocked_ffs": clock_sinks,
                    }
                )

        for row in sink_rows:
            net_seq = row["net_seq"]
            if current_net is not None and net_seq != current_net:
                finish_net()
                clock_sinks = 0
            current_net = net_seq
            current_id = row["net_id"]
            if _clock_pin(row["type"], row["port"]):
                clock_sinks += 1
        finish_net()
        clocked_ffs = sum(item["clocked_ffs"] for item in fabric_clock_nets)
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
    finally:
        connection.close()


def indexed_phase1_facts(
    ir_path: Path,
) -> tuple[str, list[str], ResourceVector, Counter[str]]:
    """Read only the bounded summaries needed by the Phase 1 report."""

    connection = open_emuir_sqlite_read_only(ir_path)
    try:
        metadata = {
            row["key"]: row["value"]
            for row in connection.execute(
                "SELECT key, value FROM metadata WHERE key IN (?, ?)",
                ("design_json", "warnings_json"),
            )
        }
        design = json.loads(metadata["design_json"])
        warnings = json.loads(metadata["warnings_json"])
        totals = ResourceVector.from_mapping(
            {
                row["resource"]: row["amount"]
                for row in connection.execute(
                    """
                    SELECT resource, SUM(amount) AS amount
                      FROM instance_resources
                     GROUP BY resource
                    """
                )
            },
            context="indexed EmuIR resource totals",
        )
        cut_classes: Counter[str] = Counter(
            {
                row["cut_class"]: row["amount"]
                for row in connection.execute(
                    """
                    SELECT cut_class, COUNT(*) AS amount
                      FROM nets
                     GROUP BY cut_class
                    """
                )
            }
        )
        return design["name"], warnings, totals, cut_classes
    finally:
        connection.close()


def run_phase1(
    yosys_json: Path,
    platform_path: Path,
    output_dir: Path,
    top: Optional[str] = None,
    clocks: Iterable[str] = (),
    require_no_fabric_clock: bool = False,
) -> Dict[str, Any]:
    platform = Platform.load(platform_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    ir_path = output_dir / "design.emuir.sqlite"
    import_yosys_json_indexed(
        yosys_json,
        ir_path,
        top=top,
        clocks=clocks,
    )
    design_name, warnings, totals, cut_classes = indexed_phase1_facts(ir_path)

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
    clock_topology = analyze_indexed_clock_topology(ir_path)
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
        "design": design_name,
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
        "warnings": warnings,
        "artifacts": {
            "emuir": "design.emuir.sqlite",
            "platform": "platform.normalized.json",
            "report": "phase1_report.json",
        },
    }

    write_json(output_dir / "platform.normalized.json", platform.to_dict())
    write_json(output_dir / "phase1_report.json", report)
    return report
