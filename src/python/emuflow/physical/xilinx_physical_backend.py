"""Complete per-partition RapidWright research backend."""

from __future__ import annotations
from emuflow.physical.xilinx_physical_handoff import open_xilinx_physical_handoff

import gc
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from emuflow.common.native_tools import native_file_generation
from emuflow.common.errors import ValidationError
from emuflow.ir.emuir_compact import source_metadata
from emuflow.ir.emuir_storage import is_emuir_sqlite, open_emuir_sqlite_read_only
from emuflow.common.io import file_sha256, read_json, write_json
from emuflow.physical.physical_backend import PHYSICAL_PARTITION_RESULT_SCHEMA
from emuflow.ir.resource_budget import validate_device_resource_usage, validate_materialized_resource_counts
from emuflow.physical.xilinx_netlist import emit_xilinx_mapped_index, open_xilinx_mapped_index
from emuflow.physical.openparf.xilinx_openparf_atomic import build_xilinx_openparf_atomic_source
from emuflow.physical.openparf.qualification_native import run_qualification
from emuflow.timing.xilinx_opensta import run_xilinx_routed_opensta
from emuflow.physical.rapidwright.routed_endpoints_native import bind_routed_timing_sources
from emuflow.physical.rapidwright.route_stage import run_native_rwroute_stage
from emuflow.timing.xilinx_segment_timing import build_xilinx_segment_timing_bundle
from emuflow.timing.xilinx_timing import build_xilinx_routed_timing


_ACTIVE_REGION_RESOURCE_HEADROOM = 1.5


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
    virtual_io_contract: Optional[Mapping[str, Any]] = None,
    resume: bool = False,
) -> Dict[str, Any]:
    """Route and time one already materialized packed placement."""

    routed = run_native_rwroute_stage(
        None, output_dir / "route.json", rapidwright_jar=rapidwright_jar,
        java=java, java_source=java_source, device_data_root=device_data_root,
        timing_data_dir=timing_data_dir, mapped_path=mapped_path,
        handoff_path=packed_path, placement_path=placement_path,
        virtual_io_contract=virtual_io_contract,
        resource_limits=runtime.get("resource_budget", {}).get("effective_capacity"),
        export_input=True, resume=resume,
    )
    source_sha256 = routed["source_sha256"]
    route_input_report, route_report = routed["export"], routed["route"]
    route_path, route_value = Path(route_report["output"]), routed["manifest"]
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

    inventory = mapped_report["audit"]["cell_types"]
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
        timing_sources=bind_routed_timing_sources(routed_timing_path,routed_timing_value),
        source_sha256={
            "mapped_sha256": source_sha256["mapped_sha256"],
            "routed_timing_sha256": timing_check["timing_sha256"],
        },
    )
    critical_path_ns = float(opensta_check["critical_path_ns"])
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
        **({"routed_graph": segment_imports["native_graph"]} if "native_graph" in segment_imports else {}),
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

    from emuflow.common.native_tools import require_native_schema
    require_native_schema("rwroute-export-request-v2")
    require_native_schema("routed-segment-request-v1")
    require_native_schema("routed-endpoint-request-v1")
    require_native_schema("routed-opensta-request-v1")
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
    atomic_summary = dict(atomic_source["summary"])
    qualification_root = output_dir / "openparf-native"
    qualification = run_qualification(
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
        expected_part=part,
    )
    selected_slrs = tuple(qualification["allowed_slrs"])
    region_path = Path(qualification["region_path"])
    certificate_path = Path(qualification["certificate_path"])
    compact_qualification = _compact_openparf_qualification(
        qualification, certificate_path
    )
    bridge = qualification["bridge"]
    physical_handoff_path = Path(bridge["physical_handoff"])
    packed, placement = open_xilinx_physical_handoff(
        physical_handoff_path, validated_sha256=bridge["physical_handoff_sha256"],
        validated_generation=qualification["handoff_generation"],
    )
    del qualification
    packed_path = placement_path = physical_handoff_path
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
            "site_database": {
                "schema": "emuflow.openparf-atomic-site-database/v1",
                "path": str((qualification_root / "site-map.sqlite3").resolve()),
                "generation": list(native_file_generation(
                    qualification_root / "site-map.sqlite3")),
                "sha256": compact_qualification["manifest"]["site_database"]["sha256"],
            },
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
        virtual_io_contract=virtual_io_contract,
        resume=resume,
    )
