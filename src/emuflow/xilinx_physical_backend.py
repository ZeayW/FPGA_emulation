"""Complete per-partition RapidWright research backend."""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .architecture import ArchitectureDB
from .errors import ValidationError
from .io import file_sha256, read_json, write_json
from .physical_backend import PHYSICAL_PARTITION_RESULT_SCHEMA
from .xilinx_netlist import emit_xilinx_mapped_json
from .xilinx_openparf import run_xilinx_openparf_guidance
from .xilinx_openparf_atomic import (
    build_xilinx_openparf_atomic_source,
    run_xilinx_openparf_atomic_qualification,
)
from .xilinx_openparf_bridge import (
    materialize_xilinx_openparf_atomic_contract,
)
from .xilinx_opensta import run_xilinx_routed_opensta
from .xilinx_packing import pack_xilinx_sites, validate_xilinx_packing
from .xilinx_placement import place_xilinx_clusters, validate_xilinx_placement
from .xilinx_rwroute import (
    export_rwroute_input,
    run_rwroute,
)
from .xilinx_segment_timing import (
    build_xilinx_segment_timing_bundle,
)
from .xilinx_timing import (
    build_xilinx_routed_timing,
)


def _cluster_resource(cluster: Mapping[str, Any]) -> str:
    kind = cluster.get("kind")
    if kind in {"slice", "carry"}:
        return "slice"
    types = {item.get("cell_type") for item in cluster.get("assignments", [])}
    if types == {"DSP48E2"}:
        return "dsp"
    if types and all(str(cell_type).startswith("RAMB") for cell_type in types):
        return "bram"
    if types == {"URAM288"}:
        return "uram"
    raise ValidationError(f"packed cluster {cluster.get('id')!r} has no region resource")


def _site_resource(site_type: str) -> Optional[str]:
    upper = site_type.upper()
    if upper.startswith("SLICE"):
        return "slice"
    if upper.startswith("DSP"):
        return "dsp"
    if upper.startswith("RAMB"):
        return "bram"
    if upper.startswith("URAM"):
        return "uram"
    return None


def _select_xilinx_slr_window(
    packed_path: Path, architecture_path: Path
) -> Tuple[str, ...]:
    """Certify full-device SLR capacity for production implementation.

    Capacity proves that the packed sites fit, but it does not prove that a
    smaller SLR subset has enough routing resources for the post-split design.
    Production implementation therefore exposes the complete physical device
    to OpenPARF and RWRoute.  Explicit regional experiments continue to use
    the separately named single-SLR planning path.
    """

    packed = read_json(packed_path)
    architecture = ArchitectureDB.load(architecture_path)
    demand = Counter(_cluster_resource(cluster) for cluster in packed["clusters"])
    capacities: Dict[str, Counter] = {}
    rows: Dict[str, list[int]] = {}
    for site in architecture.value["sites"]:
        region = site.get("physical_region")
        if not isinstance(region, dict) or not isinstance(region.get("slr"), str):
            continue
        slr = region["slr"]
        resource = _site_resource(site["type"])
        if resource is not None:
            capacities.setdefault(slr, Counter())[resource] += 1
        tile = site.get("tile")
        row = tile.get("grid_row") if isinstance(tile, dict) else site.get("y")
        if isinstance(row, int):
            rows.setdefault(slr, []).append(row)
    if not capacities or set(capacities) != set(rows):
        raise ValidationError("ArchitectureDB has no complete physical SLR inventory")
    ordered = sorted(capacities, key=lambda name: (sum(rows[name]) / len(rows[name]), name))
    device_capacity = sum((capacities[name] for name in ordered), Counter())
    if any(
        demand[key] > math.floor(0.75 * device_capacity[key])
        for key in demand
    ):
        raise ValidationError(
            "packed partition exceeds the complete Xilinx device capacity"
        )
    return tuple(ordered)


def _sha256(path: Path) -> str:
    return file_sha256(path)


def _artifact(path: Path, sha256: Optional[str] = None) -> Dict[str, str]:
    return {"path": str(path), "sha256": sha256 or _sha256(path)}


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


def _compact_openparf_qualification(
    qualification: Mapping[str, Any], certificate_path: Path
) -> Dict[str, Any]:
    """Keep the native certificate authoritative without embedding its payload.

    Real partitions contain hundreds of thousands of atomic assignments.  The
    independently checked placement certificate therefore belongs in its own
    scratch artifact; copying its complete ``clusters`` array into every
    physical report only creates a second large JSON hot path.  The report
    retains the constant-size certificate identity, source seals, summary, and
    exact artifact digest needed for audit.
    """

    certificate = qualification.get("certificate")
    if qualification.get("status") != "pass" or not isinstance(
        certificate, Mapping
    ):
        raise ValidationError("native OpenPARF qualification is invalid")
    required = {
        "schema", "status", "part", "provider", "runtime_validation",
        "source", "summary",
    }
    if not required.issubset(certificate):
        raise ValidationError(
            "native OpenPARF placement certificate identity is incomplete"
        )
    compact = {
        key: value for key, value in qualification.items()
        if key != "certificate"
    }
    compact["certificate"] = {
        key: certificate[key] for key in (
            "schema", "status", "part", "provider", "runtime_validation",
            "source", "summary",
        )
    }
    if "native_convergence" in certificate:
        compact["certificate"]["native_convergence"] = certificate[
            "native_convergence"
        ]
    compact["certificate"]["artifact"] = {
        **_artifact(certificate_path),
        "bytes": certificate_path.stat().st_size,
    }
    return compact


def _run_rapidwright_routed_backend_tail(
    *,
    fpga: str,
    part: str,
    merged_ir_path: Path,
    mapped_path: Path,
    mapped_report: Mapping[str, Any],
    packed_path: Path,
    placement_path: Path,
    mapped_value: Mapping[str, Any],
    packed_value: Mapping[str, Any],
    placement_value: Mapping[str, Any],
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
    opensta: Optional[str],
    logic_identity_path: Optional[Path],
    local_identity_path: Optional[Path],
    provider: str,
    packing_stage: Mapping[str, Any],
    placement_stage: Mapping[str, Any],
    placement_artifacts: Mapping[str, Path],
) -> Dict[str, Any]:
    """Route and time one already materialized packed placement."""

    source_sha256 = {
        "mapped_sha256": _sha256(mapped_path),
        "packed_sha256": _sha256(packed_path),
        "placement_sha256": _sha256(placement_path),
    }
    rwroute_input = output_dir / "rwroute.tsv"
    route_input_report = export_rwroute_input(
        mapped_path,
        packed_path,
        placement_path,
        rwroute_input,
        mapped_value=mapped_value,
        packed_value=packed_value,
        placement_value=placement_value,
        source_sha256=source_sha256,
    )
    route_path = output_dir / "route.json"
    route_value: Dict[str, Any] = {}
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
        mapped_path=mapped_path,
        packed_path=packed_path,
        placement_path=placement_path,
        mapped_value=mapped_value,
        packed_value=packed_value,
        source_sha256=source_sha256,
        route_value_sink=route_value,
    )
    route_check = {
        key: value for key, value in route_report.items()
        if key not in {"output", "log"}
    }
    routed_timing_path = output_dir / "routed-timing.json"
    routed_timing_value: Dict[str, Any] = {}
    routed_timing = build_xilinx_routed_timing(
        mapped_path,
        packed_path,
        placement_path,
        route_path,
        routed_timing_path,
        route_validation=route_check,
        mapped_value=mapped_value,
        packed_value=packed_value,
        placement_value=placement_value,
        route_value=route_value,
        source_sha256=source_sha256,
        timing_value_sink=routed_timing_value,
    )
    timing_check = dict(routed_timing)
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
        timing_validation=timing_check,
        mapped_value=mapped_value,
        timing_value=routed_timing_value,
        source_sha256={
            "mapped_sha256": source_sha256["mapped_sha256"],
            "routed_timing_sha256": timing_check["timing_sha256"],
        },
    )
    boundary_timing_path = output_dir / "boundary-timing.json"
    logic_timing_path = (
        output_dir / "logic-segment-timing.json"
        if logic_identity_path is not None else None
    )
    local_timing_path = (
        output_dir / "local-path-timing.json"
        if local_identity_path is not None else None
    )
    segment_imports = build_xilinx_segment_timing_bundle(
        boundary_identity_path=boundary_identity_path,
        mapped_path=mapped_path,
        timing_path=routed_timing_path,
        boundary_output_path=boundary_timing_path,
        logic_identity_path=logic_identity_path,
        logic_output_path=logic_timing_path,
        local_identity_path=local_identity_path,
        local_output_path=local_timing_path,
        mapped_value=mapped_value,
        timing_value=routed_timing_value,
        timing_validation=timing_check,
    )
    boundary_import = segment_imports["boundary"]
    logic_stage = (
        {"status": "pass", "import": segment_imports["logic_segment"]}
        if "logic_segment" in segment_imports else None
    )
    local_stage = (
        {"status": "pass", "import": segment_imports["local_path"]}
        if "local_path" in segment_imports else None
    )

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
        for cell in mapped_value["modules"][mapped_report["top"]]["cells"].values()
    )
    expansion_cells = (
        int(route_input_report["expanded_lut6_2_cells"])
        + 7 * int(route_input_report["transformed_dsp48e2_cells"])
    )
    artifacts = {
        "mapped": _artifact(mapped_path, source_sha256["mapped_sha256"]),
        "packed": _artifact(packed_path, source_sha256["packed_sha256"]),
        "placement": _artifact(
            placement_path, source_sha256["placement_sha256"]
        ),
        "route": _artifact(route_path, route_check["route_sha256"]),
        "routed_timing": _artifact(
            routed_timing_path, timing_check["timing_sha256"]
        ),
        "opensta_summary": _artifact(
            opensta_summary_path, opensta_check["summary_sha256"]
        ),
    }
    artifacts.update({
        name: _artifact(path) for name, path in placement_artifacts.items()
    })
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
        "artifacts": artifacts,
    }
    return {
        "status": "pass",
        "provider": provider,
        "qualification": opensta_summary["qualification"],
        "mapped_netlist": mapped_report,
        "packing": dict(packing_stage),
        "placement": dict(placement_stage),
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
        "boundary_timing": {"status": "pass", "import": boundary_import},
        **({"logic_segment_timing": logic_stage} if logic_stage else {}),
        **({"local_path_timing": local_stage} if local_stage else {}),
        "result": result,
    }


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
    openparf_native_constraints: Optional[Path] = None,
    openparf_provider_manifest: Optional[Path] = None,
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
    mapped_value = read_json(mapped_path)
    packed_path = output_dir / "packed-sites.json"
    packed = pack_xilinx_sites(mapped_path, packed_path)
    packing_check = validate_xilinx_packing(
        mapped_path, packed_path, architecture_path=architecture_path
    )
    selected_slrs = _select_xilinx_slr_window(packed_path, architecture_path)
    region_path = output_dir / "placement-region.json"
    write_json(region_path, {
        "schema": "emuflow.xilinx-placement-constraints/v1",
        "global": {"allowed_slrs": list(selected_slrs)},
        "clusters": [],
    }, compact=True)
    guidance_root = output_dir / "openparf-guidance"
    guidance_report = run_xilinx_openparf_guidance(
        mapped_path,
        packed_path,
        architecture_path,
        guidance_root,
        top=mapped_report["top"],
        openparf_install=openparf_install,
        openparf_python=openparf_python,
        native_constraints_path=openparf_native_constraints,
        provider_manifest_path=openparf_provider_manifest,
        slrs=selected_slrs,
    )
    guidance_path = guidance_root / "guidance.json"
    placement_path = output_dir / "placement.json"
    placement = place_xilinx_clusters(
        packed_path,
        architecture_path,
        placement_path,
        guidance_path=guidance_path,
        constraints_path=region_path,
    )
    placement_check = validate_xilinx_placement(
        packed_path,
        architecture_path,
        placement_path,
        constraints_path=region_path,
    )
    return _run_rapidwright_routed_backend_tail(
        fpga=fpga,
        part=part,
        merged_ir_path=merged_ir_path,
        mapped_path=mapped_path,
        mapped_report=mapped_report,
        packed_path=packed_path,
        placement_path=placement_path,
        mapped_value=mapped_value,
        packed_value=packed,
        placement_value=placement,
        runtime=runtime,
        original_cells=original_cells,
        transport_cells=transport_cells,
        output_dir=output_dir,
        boundary_identity_path=boundary_identity_path,
        rapidwright_jar=rapidwright_jar,
        java=java,
        classes_dir=classes_dir,
        java_source=java_source,
        device_data_root=device_data_root,
        timing_data_dir=timing_data_dir,
        opensta=opensta,
        logic_identity_path=logic_identity_path,
        local_identity_path=local_identity_path,
        provider="rapidwright-rwroute-research-backend-v1",
        packing_stage={
            "result": packed["summary"], "validation": packing_check,
        },
        placement_stage={
            "region": {
                "scope": "contiguous-slr-window",
                "allowed_slrs": list(selected_slrs),
            },
            "global_guidance": guidance_report,
            "result": placement["summary"],
            "validation": placement_check,
        },
        placement_artifacts={"placement_region": region_path},
    )


def run_rapidwright_openparf_native_candidate_backend(
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
    openparf_native_constraints: Optional[Path] = None,
    openparf_provider_manifest: Optional[Path] = None,
    opensta: Optional[str] = None,
    logic_identity_path: Optional[Path] = None,
    local_identity_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run the fail-closed native OpenPARF placement candidate backend."""

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
    # The mapped netlist is hundreds of MiB for realistic DLA partitions.
    # Parse it once and pass the immutable object through packing, native
    # placement, bridge, and validation instead of reparsing it at every
    # contract boundary.
    mapped_value = read_json(mapped_path)
    atomic_source_path = output_dir / "openparf-atomic-source.json"
    atomic_source = build_xilinx_openparf_atomic_source(
        mapped_path,
        atomic_source_path,
        top=mapped_report["top"],
        mapped_value=mapped_value,
    )
    qualification_root = output_dir / "openparf-native"
    qualification = run_xilinx_openparf_atomic_qualification(
        mapped_path,
        atomic_source_path,
        architecture_path,
        qualification_root,
        top=mapped_report["top"],
        openparf_install=openparf_install,
        openparf_python=openparf_python,
        native_constraints_path=openparf_native_constraints,
        provider_manifest_path=openparf_provider_manifest,
        mapped_value=mapped_value,
        architecture=architecture,
    )
    certificate_path = qualification_root / "placement-certificate.json"
    compact_qualification = _compact_openparf_qualification(
        qualification, certificate_path
    )
    # The serialized certificate remains the independently checked boundary.
    # The complete Python assignment tree is not needed during RWRoute/OpenSTA.
    del qualification
    packed_path = output_dir / "packed-sites.json"
    placement_path = output_dir / "placement.json"
    bridge = materialize_xilinx_openparf_atomic_contract(
        mapped_path,
        architecture_path,
        certificate_path,
        packed_path,
        placement_path,
        top=mapped_report["top"],
        source_packed_path=atomic_source_path,
        native_constraints_path=openparf_native_constraints,
        provider_manifest_path=openparf_provider_manifest,
        mapped_value=mapped_value,
        architecture=architecture,
    )
    packed = read_json(packed_path)
    placement = read_json(placement_path)
    packing_check = validate_xilinx_packing(
        mapped_path,
        packed_path,
        architecture_path=architecture_path,
        mapped_value=mapped_value,
        architecture=architecture.value,
    )
    placement_check = validate_xilinx_placement(
        packed_path,
        architecture_path,
        placement_path,
        native_constraints_path=openparf_native_constraints,
        provider_manifest_path=openparf_provider_manifest,
        architecture=architecture,
        packed_value=packed,
        placement_value=placement,
    )
    return _run_rapidwright_routed_backend_tail(
        fpga=fpga,
        part=part,
        merged_ir_path=merged_ir_path,
        mapped_path=mapped_path,
        mapped_report=mapped_report,
        packed_path=packed_path,
        placement_path=placement_path,
        mapped_value=mapped_value,
        packed_value=packed,
        placement_value=placement,
        runtime=runtime,
        original_cells=original_cells,
        transport_cells=transport_cells,
        output_dir=output_dir,
        boundary_identity_path=boundary_identity_path,
        rapidwright_jar=rapidwright_jar,
        java=java,
        classes_dir=classes_dir,
        java_source=java_source,
        device_data_root=device_data_root,
        timing_data_dir=timing_data_dir,
        opensta=opensta,
        logic_identity_path=logic_identity_path,
        local_identity_path=local_identity_path,
        provider="rapidwright-rwroute-openparf-native-candidate-v1",
        packing_stage={
            "source": atomic_source["summary"],
            "result": packed["summary"],
            "validation": packing_check,
        },
        placement_stage={
            "region": {"scope": "openparf-native-full-device"},
            "native_openparf": compact_qualification,
            "bridge": bridge,
            "result": placement["summary"],
            "validation": placement_check,
        },
        placement_artifacts={
            "openparf_atomic_source": atomic_source_path,
            "openparf_atomic_certificate": certificate_path,
        },
    )
