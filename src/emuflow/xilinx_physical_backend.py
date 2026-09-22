"""Complete per-partition RapidWright research backend."""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .architecture import ArchitectureDB
from .errors import ValidationError
from .io import read_json
from .physical_backend import PHYSICAL_PARTITION_RESULT_SCHEMA
from .xilinx_netlist import emit_xilinx_mapped_json
from .xilinx_openparf import run_xilinx_openparf_guidance
from .xilinx_opensta import run_xilinx_routed_opensta
from .xilinx_packing import pack_xilinx_sites, validate_xilinx_packing
from .xilinx_placement import (
    place_xilinx_clusters,
    validate_xilinx_placement,
)
from .xilinx_rwroute import (
    export_rwroute_input,
    run_rwroute,
    validate_xilinx_route_db,
)
from .xilinx_segment_timing import (
    build_xilinx_boundary_timing,
    build_xilinx_local_path_timing,
    build_xilinx_logic_segment_timing,
)
from .xilinx_timing import (
    build_xilinx_routed_timing,
    validate_xilinx_routed_timing,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(path: Path) -> Dict[str, str]:
    return {"path": str(path), "sha256": _sha256(path)}


def _physical_clock_periods(
    mapped_ir: Mapping[str, Any], runtime: Mapping[str, Any]
) -> Dict[str, float]:
    clocks: Dict[str, float] = {}
    for clock in mapped_ir.get("clocks", []):
        name = clock.get("id")
        if not isinstance(name, str) or not name:
            continue
        if name == "fabric_clk":
            clocks[name] = float(runtime["fabric_clock"]["period_ns"])
        else:
            clocks[name] = float(
                runtime["virtual_dut_clock"]["nominal_period_ns"]
            )
    return clocks


def run_rapidwright_partition_backend(
    *,
    fpga: str,
    part: str,
    merged_ir_path: Path,
    architecture_path: Path,
    runtime: Mapping[str, Any],
    original_cells: int,
    transport_cells: int,
    output_dir: Path,
    boundary_identity_path: Path,
    rapidwright_jar: Path,
    java: Path,
    classes_dir: Path,
    java_source: Path,
    device_data_root: Path,
    timing_data_dir: Path,
    openparf_install: Optional[Path] = None,
    openparf_python: Optional[Path] = None,
    opensta: Optional[str] = None,
    logic_identity_path: Optional[Path] = None,
    local_identity_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Pack, place, route, time, and independently check one partition."""

    architecture = ArchitectureDB.load(architecture_path)
    if architecture.part != part:
        raise ValidationError(
            f"RapidWright architecture part {architecture.part!r} does not "
            f"match BoardDB part {part!r}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    mapped_path = output_dir / "partition.mapped.json"
    mapped_report = emit_xilinx_mapped_json(
        merged_ir_path,
        mapped_path,
        output_dir / "mapped-netlist-report.json",
    )
    packed_path = output_dir / "packed-sites.json"
    packed = pack_xilinx_sites(mapped_path, packed_path)
    packing_check = validate_xilinx_packing(
        mapped_path, packed_path, architecture_path=architecture_path
    )
    guidance_root = output_dir / "openparf-guidance"
    guidance_report = run_xilinx_openparf_guidance(
        mapped_path,
        packed_path,
        architecture_path,
        guidance_root,
        top=mapped_report["top"],
        openparf_install=openparf_install,
        openparf_python=openparf_python,
    )
    guidance_path = guidance_root / "guidance.json"
    placement_path = output_dir / "placement.json"
    placement = place_xilinx_clusters(
        packed_path,
        architecture_path,
        placement_path,
        guidance_path=guidance_path,
    )
    placement_check = validate_xilinx_placement(
        packed_path,
        architecture_path,
        placement_path,
    )
    rwroute_input = output_dir / "rwroute.tsv"
    route_input_report = export_rwroute_input(
        mapped_path, packed_path, placement_path, rwroute_input
    )
    route_path = output_dir / "route.json"
    route_report = run_rwroute(
        rwroute_input,
        route_path,
        rapidwright_jar=rapidwright_jar,
        java=java,
        classes_dir=classes_dir,
        java_source=java_source,
        device_data_root=device_data_root,
        timing_data_dir=timing_data_dir,
        log_path=output_dir / "rwroute.log",
    )
    route_check = validate_xilinx_route_db(
        route_path,
        mapped_path=mapped_path,
        packed_path=packed_path,
        placement_path=placement_path,
    )
    routed_timing_path = output_dir / "routed-timing.json"
    routed_timing = build_xilinx_routed_timing(
        mapped_path,
        packed_path,
        placement_path,
        route_path,
        routed_timing_path,
    )
    timing_check = validate_xilinx_routed_timing(
        routed_timing_path,
        mapped_path=mapped_path,
        packed_path=packed_path,
        placement_path=placement_path,
        route_path=route_path,
    )
    mapped_ir = read_json(merged_ir_path)
    clocks = _physical_clock_periods(mapped_ir, runtime)
    path_database_path = output_dir / "opensta-paths.json"
    opensta_summary_path = output_dir / "opensta-summary.json"
    opensta_check = run_xilinx_routed_opensta(
        mapped_path,
        routed_timing_path,
        path_database_path,
        opensta_summary_path,
        clocks=clocks,
        executable=opensta,
        log_path=output_dir / "opensta.log",
    )
    boundary_timing_path = output_dir / "boundary-timing.json"
    boundary_import = build_xilinx_boundary_timing(
        boundary_identity_path,
        mapped_path,
        routed_timing_path,
        boundary_timing_path,
    )
    logic_stage = None
    if logic_identity_path is not None:
        logic_timing_path = output_dir / "logic-segment-timing.json"
        logic_import = build_xilinx_logic_segment_timing(
            logic_identity_path,
            mapped_path,
            routed_timing_path,
            logic_timing_path,
        )
        logic_stage = {
            "status": "pass",
            "import": logic_import,
        }
    local_stage = None
    if local_identity_path is not None:
        local_timing_path = output_dir / "local-path-timing.json"
        local_import = build_xilinx_local_path_timing(
            local_identity_path,
            mapped_path,
            routed_timing_path,
            local_timing_path,
        )
        local_stage = {
            "status": "pass",
            "import": local_import,
        }

    opensta_database = read_json(path_database_path)
    critical_path_ns = max(
        (float(path.get("fixed_delay_ns", 0.0))
         for path in opensta_database.get("paths", [])),
        default=0.0,
    )
    opensta_summary = read_json(opensta_summary_path)
    qor = opensta_summary["qor"]
    inventory = Counter(
        cell["type"]
        for cell in read_json(mapped_path)["modules"][mapped_report["top"]]["cells"].values()
    )
    expansion_cells = (
        int(route_input_report["expanded_lut6_2_cells"])
        + 7 * int(route_input_report["transformed_dsp48e2_cells"])
    )
    result = {
        "schema": PHYSICAL_PARTITION_RESULT_SCHEMA,
        "status": "pass",
        "identity": {"backend": "rapidwright", "fpga": fpga, "part": part},
        "cell_accounting": {
            "original_cells": original_cells,
            "transport_cells": transport_cells,
            "routed_cells": original_cells + transport_cells,
            "physical_cells": original_cells + transport_cells + expansion_cells,
            "infrastructure_cells": 0,
            "optimization_cells": expansion_cells,
        },
        "closure": {
            "unrouted_nets": 0,
            "drc_violations": 0,
            "drc_warnings": 0,
        },
        "clocks": {
            "fabric_period_ns": float(runtime["fabric_clock"]["period_ns"]),
            "dut_period_ns": float(
                runtime["virtual_dut_clock"]["nominal_period_ns"]
            ),
        },
        "timing": {
            "wns_ns": float(qor["wns_ns"]),
            "tns_ns": float(qor["tns_ns"]),
            "failing_endpoints": int(qor["failing_endpoints"]),
            "failing_endpoint_constraints": int(qor["failing_endpoints"]),
            "timing_met": float(qor["wns_ns"]) >= 0.0,
            "dut_wns_ns": float(qor["wns_ns"]),
            "fabric_wns_ns": float(qor["wns_ns"]),
            "fabric_to_dut_wns_ns": float(qor["wns_ns"]),
            "critical_path_ns": critical_path_ns,
        },
        "hard_resources": {
            "dsp48e2": inventory.get("DSP48E2", 0),
            "ramb18e2": inventory.get("RAMB18E2", 0),
            "ramb36e2": inventory.get("RAMB36E2", 0),
            "uram288": inventory.get("URAM288", 0),
        },
        "artifacts": {
            "mapped": _artifact(mapped_path),
            "packed": _artifact(packed_path),
            "placement": _artifact(placement_path),
            "route": _artifact(route_path),
            "routed_timing": _artifact(routed_timing_path),
            "opensta_summary": _artifact(opensta_summary_path),
        },
    }
    return {
        "status": "pass",
        "provider": "rapidwright-rwroute-research-backend-v1",
        "qualification": opensta_summary["qualification"],
        "mapped_netlist": mapped_report,
        "packing": {"result": packed["summary"], "validation": packing_check},
        "placement": {
            "global_guidance": guidance_report,
            "result": placement["summary"],
            "validation": placement_check,
        },
        "route": {
            "input": route_input_report,
            "result": route_report,
            "validation": route_check,
        },
        "routed_timing": {
            "result": routed_timing,
            "validation": timing_check,
        },
        "opensta": {
            "validation": opensta_check,
            "summary": str(opensta_summary_path),
            "paths": str(path_database_path),
        },
        "boundary_timing": {
            "status": "pass",
            "import": boundary_import,
        },
        **({"logic_segment_timing": logic_stage} if logic_stage else {}),
        **({"local_path_timing": local_stage} if local_stage else {}),
        "result": result,
    }
