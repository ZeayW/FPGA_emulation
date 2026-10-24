"""Complete per-partition RapidWright research backend."""

from __future__ import annotations

import gc
import hashlib
import json
import math
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .architecture import ArchitectureDB
from .errors import ValidationError
from .emuir_compact import source_metadata
from .emuir_storage import is_emuir_sqlite, open_emuir_sqlite_read_only
from .io import file_sha256, read_json, write_json
from .physical_backend import PHYSICAL_PARTITION_RESULT_SCHEMA
from .resource_budget import validate_device_resource_usage, validate_materialized_resource_counts
from .sta import iter_sta_path_database_paths
from .xilinx_netlist import (
    emit_xilinx_mapped_index,
    open_xilinx_mapped_index,
)
from .xilinx_openparf_atomic import (
    build_xilinx_openparf_atomic_source,
    run_xilinx_openparf_atomic_qualification,
)
from .xilinx_openparf_bridge import (
    materialize_xilinx_openparf_atomic_contract,
)
from .xilinx_opensta import run_xilinx_routed_opensta
from .xilinx_packing import validate_xilinx_packing
from .xilinx_placement import validate_xilinx_placement
from .xilinx_rwroute import (
    export_rwroute_input,
    run_rwroute,
    validate_xilinx_route_db,
)
from .xilinx_segment_timing import (
    build_xilinx_segment_timing_bundle,
)
from .xilinx_timing import (
    build_xilinx_routed_timing,
)


_ACTIVE_REGION_RESOURCE_HEADROOM = 1.5


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
    packed_path: Path,
    architecture_path: Path,
    *,
    packed_value: Optional[Mapping[str, Any]] = None,
    architecture: Optional[ArchitectureDB] = None,
) -> Tuple[str, ...]:
    """Use every SLR while retaining a fail-closed capacity admission check.

    The per-FPGA resource contract limits use to 25% of the complete device;
    it does not define a 25%-area pblock.  Confining a large partition to the
    smallest capacity-feasible SLR window turns otherwise available routing
    fabric into an artificial bottleneck.  High-fanout transport nets are
    buffered before placement, so the production default spreads placement
    over the complete device and lets OpenPARF trade wirelength for routability.

    The input is the final post-Phase-6 packed/atomic netlist, including
    transport logic.  Keep the 1.5x resource-headroom check as an admission
    guard, but do not use it to shrink the legal placement region.
    """

    packed = read_json(packed_path) if packed_value is None else packed_value
    architecture = (
        ArchitectureDB.load(architecture_path)
        if architecture is None else architecture
    )
    clusters = packed.get("clusters")
    if not isinstance(clusters, Sequence) or not clusters:
        raise ValidationError("packed partition has no clusters")
    if packed.get("schema") == "emuflow.openparf-atomic-source/v1":
        atoms = Counter(
            assignment.get("cell_type")
            for cluster in clusters
            for assignment in cluster.get("assignments", [])
        )
        demand = Counter({
            "slice": max(
                math.ceil(sum(
                    count for cell_type, count in atoms.items()
                    if isinstance(cell_type, str)
                    and (cell_type.startswith("LUT") or cell_type == "LUT6_2")
                ) / 8),
                math.ceil(sum(
                    count for cell_type, count in atoms.items()
                    if isinstance(cell_type, str) and cell_type.startswith("FD")
                ) / 16),
                atoms["CARRY8"],
                math.ceil(atoms["MUXF7"] / 4),
                math.ceil(atoms["MUXF8"] / 2),
                atoms["MUXF9"],
            ),
            "dsp": atoms["DSP48E2"],
            "bram": atoms["RAMB18E2"] + atoms["RAMB36E2"],
            "uram": atoms["URAM288"],
        })
        demand += Counter()
    else:
        demand = Counter(_cluster_resource(cluster) for cluster in clusters)
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
    required = Counter({
        resource: math.ceil(count * _ACTIVE_REGION_RESOURCE_HEADROOM)
        for resource, count in demand.items()
        if count
    })
    device_capacity = sum((capacities[name] for name in ordered), Counter())
    if any(required[key] > device_capacity[key] for key in required):
        raise ValidationError(
            "packed partition exceeds the complete Xilinx device capacity "
            f"with {_ACTIVE_REGION_RESOURCE_HEADROOM:g}x placement headroom"
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
            domains = runtime.get("virtual_dut_clocks", {})
            if name not in domains:
                raise ValidationError(
                    f"physical DUT clock {name!r} is absent from the runtime "
                    "clock contract"
                )
            clocks[name] = float(domains[name]["nominal_period_ns"])
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
    mapped_sha256: Optional[str] = None,
    virtual_io_contract: Optional[Mapping[str, Any]] = None,
    resume: bool = False,
) -> Dict[str, Any]:
    """Route and time one already materialized packed placement."""

    packed_sha256 = _sha256(packed_path)
    placement_sha256 = (
        packed_sha256
        if packed_path == placement_path else _sha256(placement_path)
    )
    source_sha256 = {
        "mapped_sha256": mapped_sha256 or _sha256(mapped_path),
        "packed_sha256": packed_sha256,
        "placement_sha256": placement_sha256,
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
        virtual_io_contract=virtual_io_contract,
        resource_limits=runtime.get("resource_budget", {}).get("effective_capacity"),
    )
    route_path = output_dir / "route.json"
    route_value: Dict[str, Any] = {}
    route_log_path = output_dir / "rwroute.log"
    if resume and route_path.is_file():
        loaded_route = read_json(route_path)
        if not isinstance(loaded_route, dict):
            raise ValidationError("resumed XilinxRouteDB header is invalid")
        route_value.update(loaded_route)
        route_report = {
            **validate_xilinx_route_db(
                route_path,
                rwroute_input_path=rwroute_input,
                mapped_path=mapped_path,
                packed_path=packed_path,
                placement_path=placement_path,
                mapped_value=mapped_value,
                packed_value=packed_value,
                source_sha256=source_sha256,
                virtual_io_contract=virtual_io_contract,
                _value=route_value,
            ),
            "output": str(route_path),
            "log": str(route_log_path) if route_log_path.is_file() else None,
            "reused": True,
        }
    else:
        route_report = run_rwroute(
            rwroute_input,
            route_path,
            rapidwright_jar=rapidwright_jar,
            java=java,
            classes_dir=classes_dir,
            java_source=java_source,
            device_data_root=device_data_root,
            timing_data_dir=timing_data_dir,
            log_path=route_log_path,
            mapped_path=mapped_path,
            packed_path=packed_path,
            placement_path=placement_path,
            mapped_value=mapped_value,
            packed_value=packed_value,
            source_sha256=source_sha256,
            route_value_sink=route_value,
            virtual_io_contract=virtual_io_contract,
        )
    return _finish_rapidwright_routed_backend(
        fpga=fpga,
        part=part,
        merged_ir_path=merged_ir_path,
        mapped_path=mapped_path,
        mapped_report=mapped_report,
        packed_path=packed_path,
        placement_path=placement_path,
        mapped_value=mapped_value,
        packed_value=packed_value,
        placement_value=placement_value,
        runtime=runtime,
        original_cells=original_cells,
        transport_cells=transport_cells,
        output_dir=output_dir,
        boundary_identity_path=boundary_identity_path,
        opensta=opensta,
        logic_identity_path=logic_identity_path,
        local_identity_path=local_identity_path,
        provider=provider,
        packing_stage=packing_stage,
        placement_stage=placement_stage,
        placement_artifacts=placement_artifacts,
        source_sha256=source_sha256,
        route_path=route_path,
        route_input_report=route_input_report,
        route_report=route_report,
        route_value=route_value,
        virtual_io_contract=virtual_io_contract,
    )


def _finish_rapidwright_routed_backend(
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
    opensta: Optional[str],
    logic_identity_path: Optional[Path],
    local_identity_path: Optional[Path],
    provider: str,
    packing_stage: Mapping[str, Any],
    placement_stage: Mapping[str, Any],
    placement_artifacts: Mapping[str, Path],
    source_sha256: Mapping[str, str],
    route_path: Path,
    route_input_report: Mapping[str, Any],
    route_report: Mapping[str, Any],
    route_value: Mapping[str, Any],
    virtual_io_contract: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Time a route after the caller independently validates its exact inputs.

    Normal routing and explicit debugging continuation share this timing tail.
    The fixed differential-I/O contract accompanies either execution path.
    """

    route_check = {
        key: value for key, value in route_report.items()
        if key not in {"output", "log", "reused"}
    }
    resource_usage = None
    if "resource_budget" in runtime:
        counts = route_value.get("physical_resources")
        if not isinstance(counts, Mapping):
            raise ValidationError("routed physical resource inventory is missing")
        validate_materialized_resource_counts(
            mapped_report["audit"]["resource_totals"], counts,
            route_value.get("materialization", {}),
        )
        resource_usage = {
            "mapped": validate_device_resource_usage(
                mapped_report["audit"]["resource_totals"], runtime["resource_budget"],
                stage="post-transport-mapping",
            ),
            "physical": validate_device_resource_usage(
                counts, runtime["resource_budget"], stage="post-physical-materialization",
            ),
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
    if is_emuir_sqlite(merged_ir_path):
        connection = open_emuir_sqlite_read_only(merged_ir_path)
        try:
            mapped_ir = source_metadata(connection)
        finally:
            connection.close()
    else:
        mapped_ir = read_json(merged_ir_path)
    clocks = _physical_clock_periods(mapped_ir, runtime)
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
        source_ir_path=merged_ir_path,
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

    inventory = Counter(
        cell["type"]
        for cell in mapped_value["modules"][mapped_report["top"]]["cells"].values()
    )
    # All mapped-graph consumers have finished. Release their storage before
    # OpenSTA stages a routed timing graph of its own.
    close_mapped = getattr(mapped_value, "close", None)
    if callable(close_mapped):
        close_mapped()
    if isinstance(mapped_value, dict):
        mapped_value.clear()
    if isinstance(packed_value, dict):
        packed_value.clear()
    if isinstance(placement_value, dict):
        placement_value.clear()
    gc.collect()

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
        timing_value=routed_timing_value,
        source_sha256={
            "mapped_sha256": source_sha256["mapped_sha256"],
            "routed_timing_sha256": timing_check["timing_sha256"],
        },
    )
    opensta_database = read_json(path_database_path)
    critical_path_ns = max(
        (float(path.get("fixed_delay_ns", 0.0))
         for path in iter_sta_path_database_paths(
             path_database_path, opensta_database
         )),
        default=0.0,
    )
    opensta_summary = read_json(opensta_summary_path)
    qor = opensta_summary["qor"]
    expansion_cells = (
        int(route_input_report["expanded_lut6_2_cells"])
        + 7 * int(route_input_report["transformed_dsp48e2_cells"])
        + int(route_value.get("materialization", {}).get("fabric_prefilter_clock_gate_cells", 0))
    )
    virtual_io_cells = int(route_input_report.get("virtual_io_cells", 0))
    artifacts = {
        "mapped": _artifact(mapped_path, source_sha256["mapped_sha256"]),
        "route": _artifact(route_path, route_check["route_sha256"]),
        "routed_timing": _artifact(
            routed_timing_path, timing_check["timing_sha256"]
        ),
        "opensta_summary": _artifact(
            opensta_summary_path, opensta_check["summary_sha256"]
        ),
    }
    if packed_path == placement_path:
        if source_sha256["packed_sha256"] != source_sha256["placement_sha256"]:
            raise ValidationError("shared physical handoff source seals disagree")
        artifacts["physical_handoff"] = _artifact(
            packed_path, source_sha256["packed_sha256"]
        )
    else:
        artifacts["packed"] = _artifact(
            packed_path, source_sha256["packed_sha256"]
        )
        artifacts["placement"] = _artifact(
            placement_path, source_sha256["placement_sha256"]
        )
    artifacts.update({
        name: _artifact(path) for name, path in placement_artifacts.items()
    })
    result = {
        "schema": PHYSICAL_PARTITION_RESULT_SCHEMA,
        "status": "pass",
        "identity": {"backend": "rapidwright", "fpga": fpga, "part": part},
        **({"resource_usage": resource_usage} if resource_usage is not None else {}),
        "cell_accounting": {
            "original_cells": original_cells,
            "transport_cells": transport_cells,
            "routed_cells": original_cells + transport_cells,
            "physical_cells": (
                original_cells + transport_cells + expansion_cells
                + virtual_io_cells
            ),
            "infrastructure_cells": virtual_io_cells,
            "optimization_cells": expansion_cells,
        },
        "closure": {
            "unrouted_nets": 0,
            "drc_violations": 0,
            "drc_warnings": 0,
        },
        "clocks": {
            "fabric_period_ns": float(runtime["fabric_clock"]["period_ns"]),
            "dut_periods_ns": {
                clock: float(record["nominal_period_ns"])
                for clock, record in sorted(
                    runtime["virtual_dut_clocks"].items()
                )
            },
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
        "source_constraints": segment_imports["source_constraints"],
        **(
            {
                "virtual_io_implementation": {
                    "status": "pass",
                    "provider": (
                        "rapidwright-fixed-lvds-iob-routed-boundary-v1"
                    ),
                    "qualification": (
                        "P-side LVDS IOB materialized and routed; N-side "
                        "dedicated differential mate verified by the fixed "
                        "device-pin contract"
                    ),
                    "contract_sha256": hashlib.sha256(
                        json.dumps(
                            virtual_io_contract,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    "differential_pairs": virtual_io_cells,
                    "physical_ports": 2 * virtual_io_cells,
                    "route_validation": {
                        "virtual_io_cells": route_check["virtual_io_cells"],
                        "maximum_route_delay_ps": route_check[
                            "maximum_route_delay_ps"
                        ],
                    },
                }
            }
            if virtual_io_contract is not None
            else {}
        ),
        **({"logic_segment_timing": logic_stage} if logic_stage else {}),
        **({"local_path_timing": local_stage} if local_stage else {}),
        "result": result,
    }




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
    virtual_io_contract: Optional[Mapping[str, Any]] = None,
    resume: bool = False,
) -> Dict[str, Any]:
    """Run the fail-closed native OpenPARF placement candidate backend."""

    architecture = ArchitectureDB.load(architecture_path)
    if architecture.part != part:
        raise ValidationError(
            f"RapidWright architecture part {architecture.part!r} does not "
            f"match BoardDB part {part!r}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    mapped_path = output_dir / "partition.mapped.sqlite"
    mapped_report = emit_xilinx_mapped_index(
        merged_ir_path,
        mapped_path,
        output_dir / "mapped-netlist-report.json",
    )
    # SQLite is the sole production mapped-netlist representation.  Every
    # physical consumer shares this bounded read view; no graph-sized JSON is
    # serialized, parsed, or retained in the Phase 7 hot path.
    if "resource_budget" in runtime:
        validate_device_resource_usage(
            mapped_report["audit"]["resource_totals"], runtime["resource_budget"],
            stage="post-transport-mapping",
        )
    mapped_value = open_xilinx_mapped_index(mapped_path)
    mapped_sha256 = mapped_report.get("output_sha256") or _sha256(mapped_path)
    atomic_source_path = output_dir / "openparf-atomic-source.json"
    atomic_source_started = time.perf_counter()
    atomic_source = build_xilinx_openparf_atomic_source(
        mapped_path,
        atomic_source_path,
        top=mapped_report["top"],
        mapped_value=mapped_value,
        mapped_sha256=mapped_sha256,
    )
    atomic_source_seconds = time.perf_counter() - atomic_source_started
    atomic_source_store_path = getattr(atomic_source, "store_path", None)
    atomic_source_selector = getattr(atomic_source, "selector", None)
    atomic_source_storage = {
        "dataflow": (
            "mapped-index-derived-singletons+typed-exception-sidecar"
            if isinstance(atomic_source_selector, Mapping)
            else "legacy-logical-view"
        ),
        "build_seconds": atomic_source_seconds,
        "selector_bytes": atomic_source_path.stat().st_size,
        "store_bytes": (
            atomic_source_store_path.stat().st_size
            if atomic_source_store_path is not None else None
        ),
        "stored_macro_clusters": (
            atomic_source_selector.get("counts", {}).get("macro_clusters")
            if isinstance(atomic_source_selector, Mapping) else None
        ),
        "derived_singleton_clusters": (
            atomic_source_selector.get("counts", {}).get("singleton_clusters")
            if isinstance(atomic_source_selector, Mapping) else None
        ),
    }
    selected_slrs = _select_xilinx_slr_window(
        atomic_source_path,
        architecture_path,
        packed_value=atomic_source,
        architecture=architecture,
    )
    atomic_summary = dict(atomic_source["summary"])
    region_path = output_dir / "placement-region.json"
    write_json(region_path, {
        "schema": "emuflow.xilinx-placement-constraints/v1",
        "policy": {
            "provider": "full-device-routability-v1",
            "resource_headroom": _ACTIVE_REGION_RESOURCE_HEADROOM,
        },
        "global": {"allowed_slrs": list(selected_slrs)},
        "clusters": [],
    }, compact=True)
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
        packed_value=atomic_source,
        architecture=architecture,
        allowed_slrs=selected_slrs,
    )
    certificate_path = qualification_root / "placement-certificate.json"
    compact_qualification = _compact_openparf_qualification(
        qualification, certificate_path
    )
    # The serialized certificate remains the independently checked boundary.
    # The complete Python assignment tree is not needed during RWRoute/OpenSTA.
    del qualification
    physical_handoff_path = output_dir / "physical-handoff.sqlite"
    for legacy_name in ("packed-sites.json", "placement.json"):
        (output_dir / legacy_name).unlink(missing_ok=True)
    packed_path = physical_handoff_path
    placement_path = physical_handoff_path
    packed: Dict[str, Any] = {}
    placement: Dict[str, Any] = {}
    bridge = materialize_xilinx_openparf_atomic_contract(
        mapped_path,
        architecture_path,
        certificate_path,
        packed_path,
        placement_path,
        top=mapped_report["top"],
        source_packed_path=atomic_source_path,
        source_packed_value=atomic_source,
        constraints_path=region_path,
        native_constraints_path=openparf_native_constraints,
        provider_manifest_path=openparf_provider_manifest,
        mapped_value=mapped_value,
        mapped_sha256=mapped_sha256,
        architecture=architecture,
        packed_value_sink=packed,
        placement_value_sink=placement,
        physical_handoff_path=physical_handoff_path,
    )
    # The source view and shared physical handoff were already independently
    # validated before atomic publication. Reuse their lazy indexed views
    # instead of materializing either historical JSON contract.
    del atomic_source
    gc.collect()
    packing_check = bridge["validation"]["packing"]
    placement_check = bridge["validation"]["placement"]
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
            "source": atomic_summary,
            "source_storage": atomic_source_storage,
            "result": packed["summary"],
            "validation": packing_check,
        },
        placement_stage={
            "region": {
                "scope": "full-device",
                "allowed_slrs": list(selected_slrs),
                "resource_headroom": _ACTIVE_REGION_RESOURCE_HEADROOM,
            },
            "native_openparf": compact_qualification,
            "bridge": bridge,
            "result": placement["summary"],
            "validation": placement_check,
        },
        placement_artifacts={
            "placement_region": region_path,
            "openparf_atomic_source": atomic_source_path,
            **({
                "openparf_atomic_source_store": atomic_source_store_path,
            } if atomic_source_store_path is not None else {}),
            "openparf_atomic_certificate": certificate_path,
        },
        mapped_sha256=mapped_sha256,
        virtual_io_contract=virtual_io_contract,
        resume=resume,
    )
