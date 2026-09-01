"""Checked Phase-6-to-physical backend for every FPGA partition."""

from __future__ import annotations

import hashlib
import math
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .boundary_timing import (
    validate_boundary_identity_database,
    validate_boundary_timing_database,
)
from .errors import EmuFlowError, ValidationError
from .io import read_json, write_json
from .logic_segment_timing import (
    import_vpr_logic_segment_timing,
    prepare_logic_segment_query_inputs,
    validate_logic_segment_timing,
    write_vivado_logic_segment_query,
    write_vpr_logic_segment_query,
)
from .local_path_timing import (
    import_vpr_local_path_timing,
    prepare_vpr_local_path_query_inputs,
    validate_local_path_timing,
    write_vpr_local_path_query,
)
from .lowering import run_placement_ir_lowering
from .netlist import SPLIT_MANIFEST_SCHEMA
from .packed_netlist import run_packed_netlist_import
from .packed_placement import run_packed_openparf_placement
from .openparf import validate_openparf_runtime
from .physical_backend import (
    PHYSICAL_PARTITION_RESULT_SCHEMA,
    physical_backend_descriptor,
    physical_summary_item,
    validate_physical_backend_descriptor,
    validate_physical_partition_result,
)
from .platform import Platform
from .runtime import (
    PHYSICAL_SUMMARY_SCHEMA,
    build_virtual_runtime,
    validate_physical_summary,
)
from .synthesis import run_generic_yosys
from .vpr import VTR_HARD_BLOCK_PROFILE, run_vpr_pack_place, run_vpr_route_packed
from .vpr_boundary_timing import (
    import_vpr_boundary_timing,
    write_vpr_boundary_timing_query,
)
from .vtr_architecture import (
    fetch_pinned_vtr_architecture,
    read_vpr_placement_dimensions,
    run_vtr_architecture_import,
)
from .vtr_eblif import emit_vtr_eblif
from .vivado_backend import run_vivado_partition_backend
from .vivado_netlist import emit_vivado_mapped_verilog
from .yosys import import_yosys_json


MULTI_FPGA_PHYSICAL_SCHEMA = "emuflow.multi-fpga-physical-flow/v1"
_TRANSPORT_MODULE = re.compile(r"^module\s+([A-Za-z_][A-Za-z0-9_$]*)", re.M)

# VPR stores SDC times in signed 32-bit picoseconds. Leave a deliberate
# margin below INT_MAX so a rounded parser value cannot overflow. This only
# affects VPR's local implementation constraint; system timing continues to
# use the exact virtual-runtime period recorded upstream.
_VPR_SDC_MAX_TIME_NS = 2_000_000.0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _record_chimew_fixed_io_target(
    targets: Dict[str, float],
    packed_groups: Dict[str, str],
    group_packed_blocks: Dict[str, str],
    *,
    packed_name: str,
    group: str,
    target_y: float,
) -> None:
    """Record one endpoint while preserving group-level TDM sharing.

    Chimew assigns a physical channel to a signal group, not to every signal
    independently.  Multiple schedule entries in the same group therefore
    intentionally lower to the same packed boundary I/O block.  Across groups
    the mapping remains one-to-one.
    """

    previous_group = packed_groups.setdefault(packed_name, group)
    if previous_group != group:
        raise ValidationError(
            "Chimew packed I/O cluster is shared by different signal groups"
        )
    previous_packed = group_packed_blocks.setdefault(group, packed_name)
    if previous_packed != packed_name:
        raise ValidationError(
            "Chimew signal group is split across packed I/O clusters"
        )
    previous_target = targets.setdefault(packed_name, target_y)
    if not math.isclose(previous_target, target_y, abs_tol=1.0e-12):
        raise ValidationError("Chimew packed I/O cluster has conflicting anchors")


def _write_vpr_runtime_sdc(
    path: Path,
    eblif_report: Mapping[str, Any],
    *,
    fabric_period_ns: float,
    dut_period_ns: float,
    cross_period_ns: float,
    dut_clock_required: bool = True,
) -> Dict[str, Any]:
    """Write VPR constraints matching the provider-neutral runtime contract."""
    for name, value in (
        ("fabric_period_ns", fabric_period_ns),
        ("dut_period_ns", dut_period_ns),
        ("cross_period_ns", cross_period_ns),
    ):
        if not math.isfinite(value) or value <= 0.0:
            raise ValidationError(f"{name} must be finite and positive")
    # The virtual DUT period can be much longer than a physical FPGA clock.
    # VPR cannot represent those long SDC values (its parser scales to signed
    # 32-bit picoseconds), while the physical implementation only needs a
    # non-binding local relation between fabric and DUT clocks. Preserve the
    # original slack after capping the DUT period, rather than silently
    # changing the relation to a zero-slack constraint.
    vpr_dut_period_ns = min(dut_period_ns, _VPR_SDC_MAX_TIME_NS)
    dut_to_cross_slack_ns = max(0.0, dut_period_ns - cross_period_ns)
    vpr_cross_period_ns = min(
        cross_period_ns,
        max(0.0, vpr_dut_period_ns - dut_to_cross_slack_ns),
    )
    # set_max_delay 0 is legal SDC but would make the implementation
    # artificially infeasible. Retain a positive, bounded relation instead.
    vpr_cross_period_ns = max(vpr_cross_period_ns, 1.0e-6)
    clock_nets = eblif_report.get("clock_nets")
    if not isinstance(clock_nets, dict) or not clock_nets:
        raise ValidationError("VTR eBLIF report has no physical clock nets")
    fabric_net = clock_nets.get("fabric_clk")
    if not isinstance(fabric_net, str) or not fabric_net:
        raise ValidationError("VTR eBLIF report has no fabric clock net")
    dut_nets = sorted(
        {
            net
            for clock_id, net in clock_nets.items()
            if clock_id not in {"fabric_clk", "virtual_clock_enable"}
            and isinstance(net, str)
            and net
        }
    )
    if not dut_nets and dut_clock_required:
        raise ValidationError("VTR eBLIF report has no DUT clock net")
    lines = [
        "# EmuFlow endpoint-complete Phase 7 timing contract.",
        f"create_clock -name emuflow_fabric_clk -period {fabric_period_ns:.9f} [get_ports {{{fabric_net}}}]",
    ]
    dut_clocks = []
    for index, net in enumerate(dut_nets):
        clock_name = f"emuflow_dut_clk_{index}"
        dut_clocks.append(clock_name)
        lines.append(
            f"create_clock -name {clock_name} -period {vpr_dut_period_ns:.9f} [get_ports {{{net}}}]"
        )
        lines.extend(
            (
                f"set_max_delay {vpr_cross_period_ns:.9f} -from [get_clocks {{emuflow_fabric_clk}}] -to [get_clocks {{{clock_name}}}]",
                f"set_max_delay {vpr_cross_period_ns:.9f} -from [get_clocks {{{clock_name}}}] -to [get_clocks {{emuflow_fabric_clk}}]",
            )
        )
    path.write_text("\n".join((*lines, "")), encoding="utf-8")
    return {
        "status": "pass",
        "provider": "emuflow-vpr-runtime-sdc-v1",
        "path": str(path),
        "sha256": _sha256(path),
        "fabric_clock": "emuflow_fabric_clk",
        "dut_clocks": dut_clocks,
        "dut_clock_required": dut_clock_required,
        "dut_clock_present": bool(dut_clocks),
        "requested_periods_ns": {
            "fabric": fabric_period_ns,
            "dut": dut_period_ns,
            "cross": cross_period_ns,
        },
        "effective_vpr_periods_ns": {
            "fabric": fabric_period_ns,
            "dut": vpr_dut_period_ns,
            "cross": vpr_cross_period_ns,
        },
        "vpr_sdc_time_capped": (
            vpr_dut_period_ns != dut_period_ns
            or vpr_cross_period_ns != cross_period_ns
        ),
    }


def _partition_declares_dut_clock(netlist: Mapping[str, Any]) -> bool:
    """Return whether the original split partition exposes a DUT clock."""
    ports = netlist.get("ports")
    if not isinstance(ports, list) or any(
        not isinstance(port, dict) for port in ports
    ):
        raise ValidationError("per-FPGA netlist ports are invalid")
    return any(port.get("clock") is True for port in ports)


def _physical_clock_delays(
    route_report: Mapping[str, Any],
    eblif_report: Mapping[str, Any],
) -> Tuple[Dict[str, float], Dict[str, bool]]:
    metrics = route_report.get("metrics", {})
    overall = metrics.get("critical_path_ns")
    if not isinstance(overall, (int, float)) or overall < 0:
        raise ValidationError("VPR route lacks a valid critical path")
    domains = metrics.get("clock_domain_cpd_ns", {})
    clock_nets = eblif_report.get("clock_nets", {})
    fabric = clock_nets.get("fabric_clk")
    dut_nets = {
        net
        for clock_id, net in clock_nets.items()
        if clock_id not in {"fabric_clk", "virtual_clock_enable"}
        and isinstance(net, str)
        and net
    }
    fabric_delays = []
    dut_delays = []
    cross_delays = []
    if isinstance(domains, dict):
        for pair, delay in domains.items():
            if not isinstance(pair, str) or not isinstance(delay, (int, float)):
                continue
            launch, separator, capture = pair.partition("->")
            if not separator:
                continue
            if fabric is not None and launch == fabric and capture == fabric:
                fabric_delays.append(float(delay))
            elif launch in dut_nets and capture in dut_nets:
                dut_delays.append(float(delay))
            elif fabric is not None and (
                (launch == fabric and capture in dut_nets)
                or (capture == fabric and launch in dut_nets)
            ):
                cross_delays.append(float(delay))
    # VPR reports per-clock CPDs for multi-clock circuits. The fallback to the
    # overall CPD is conservative for old VPR logs that lack that table.
    dut_present = bool(dut_nets)
    return (
        {
            "overall": float(overall),
            "fabric": max(fabric_delays, default=float(overall)),
            # A partition containing no original sequential DUT logic has no
            # local DUT or fabric/DUT clock-domain path.  Its combinational
            # contribution is certified separately by routed logic-segment
            # timing and must not be invented from an unrelated fabric CPD.
            "dut": max(dut_delays, default=float(overall)) if dut_present else 0.0,
            "cross": (
                max(cross_delays, default=float(overall))
                if dut_present
                else 0.0
            ),
        },
        {
            "fabric": fabric is not None,
            "dut": dut_present,
            "cross": dut_present and fabric is not None,
        },
    )


def validate_multi_fpga_physical_report(
    report: Mapping[str, Any],
    physical_summary: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    if report.get("schema") != MULTI_FPGA_PHYSICAL_SCHEMA:
        raise ValidationError("multi-FPGA physical-flow schema is invalid")
    if report.get("status") != "pass":
        raise ValidationError("multi-FPGA physical flow did not pass")
    descriptor = report.get("backend")
    if not isinstance(descriptor, dict):
        raise ValidationError("multi-FPGA physical backend is missing")
    backend_validation = validate_physical_backend_descriptor(descriptor)
    backend_id = backend_validation["backend"]
    expected = report.get("expected_fpgas")
    records = report.get("fpgas")
    if not isinstance(expected, list) or not expected:
        raise ValidationError("multi-FPGA physical expected FPGA list is invalid")
    if not isinstance(records, list):
        raise ValidationError("multi-FPGA physical FPGA records are invalid")
    by_id = {item.get("fpga"): item for item in records if isinstance(item, dict)}
    if set(by_id) != set(expected) or len(by_id) != len(records):
        raise ValidationError("multi-FPGA physical FPGA coverage is incomplete")
    original_cells = 0
    transport_cells = 0
    emitted_atoms = 0
    worst_critical_path = 0.0
    for fpga_id in expected:
        item = by_id[fpga_id]
        if item.get("status") != "pass":
            raise ValidationError(f"physical flow for {fpga_id} did not pass")
        stages = item.get("stages")
        required = ("transport_synthesis", "placement_ir")
        if not isinstance(stages, dict) or any(
            name not in stages for name in required
        ):
            raise ValidationError(f"physical stages for {fpga_id} are incomplete")
        if any(
            not isinstance(stage, dict) or stage.get("status") != "pass"
            for stage in stages.values()
        ):
            raise ValidationError(f"one or more physical stages for {fpga_id} failed")
        lowering = stages["placement_ir"]
        boundary = lowering.get("boundary_identity")
        if (
            not isinstance(boundary, dict)
            or boundary.get("validation", {}).get("status") != "pass"
        ):
            raise ValidationError(
                f"physical boundary identities for {fpga_id} are incomplete"
            )
        if lowering.get("instances") != (
            item.get("original_cells") + item.get("transport_cells")
        ):
            raise ValidationError(f"merged cell accounting for {fpga_id} disagrees")
        part = item.get("part")
        if not isinstance(part, str) or not part:
            raise ValidationError(f"physical part for {fpga_id} is invalid")
        result = item.get("physical_result")
        if not isinstance(result, dict):
            raise ValidationError(f"physical result for {fpga_id} is missing")
        validate_physical_partition_result(
            result,
            backend=backend_id,
            fpga=fpga_id,
            part=part,
            original_cells=item["original_cells"],
            transport_cells=item["transport_cells"],
        )
        if backend_id == "open":
            open_required = (
                "eblif",
                "vpr_pack_place",
                "architecture_import",
                "packed_contract",
                "openparf_placement",
                "vpr_route",
                "runtime_sdc",
                "boundary_timing",
            )
            if any(name not in stages for name in open_required):
                raise ValidationError(
                    f"open physical stages for {fpga_id} are incomplete"
                )
            eblif = stages["eblif"]
            pack = stages["vpr_pack_place"]
            route = stages["vpr_route"]
            runtime_sdc = stages["runtime_sdc"]
            if eblif.get("source_instances") != lowering.get("instances"):
                raise ValidationError(
                    f"eBLIF source coverage for {fpga_id} disagrees"
                )
            if pack.get("circuit", {}).get("sha256") != eblif.get(
                "output_sha256"
            ):
                raise ValidationError(
                    f"VPR packing for {fpga_id} used another circuit"
                )
            if route.get("circuit", {}).get("sha256") != eblif.get(
                "output_sha256"
            ):
                raise ValidationError(
                    f"VPR routing for {fpga_id} used another circuit"
                )
            if route.get("route_check", {}).get("status") != "pass":
                raise ValidationError(
                    f"independent route check for {fpga_id} failed"
                )
            if route.get("sdc_file", {}).get("sha256") != runtime_sdc.get(
                "sha256"
            ):
                raise ValidationError(
                    f"VPR routing for {fpga_id} used another timing contract"
                )
            timing_summary = route.get("timing_summary")
            if (
                not isinstance(timing_summary, dict)
                or timing_summary.get("status") != "pass"
            ):
                raise ValidationError(
                    f"endpoint-complete timing summary for {fpga_id} failed"
                )
            route_metrics = route.get("metrics", {})
            result_timing = result["timing"]
            for route_metric, result_metric in (
                ("setup_worst_slack_ns", "wns_ns"),
                ("setup_tns_ns", "tns_ns"),
                ("setup_failing_endpoints", "failing_endpoints"),
                (
                    "setup_failing_endpoint_constraints",
                    "failing_endpoint_constraints",
                ),
            ):
                if route_metrics.get(route_metric) != result_timing.get(
                    result_metric
                ):
                    raise ValidationError(
                        f"physical timing for {fpga_id} disagrees with VPR"
                    )
            emitted_atoms += eblif["emitted_atoms"]
        elif not {"mapped_verilog", "vivado_implementation"}.issubset(stages):
            raise ValidationError(
                f"Vivado physical stages for {fpga_id} are incomplete"
            )
        original_cells += item["original_cells"]
        transport_cells += item["transport_cells"]
        critical_path = result["timing"].get("critical_path_ns", 0.0)
        if not isinstance(critical_path, (int, float)) or critical_path < 0:
            raise ValidationError(
                f"physical critical path for {fpga_id} is invalid"
            )
        worst_critical_path = max(worst_critical_path, float(critical_path))
    summary = (
        physical_summary
        if physical_summary is not None
        else report.get("physical_summary")
    )
    if not isinstance(summary, dict) or summary.get("status") != "pass":
        raise ValidationError("multi-FPGA physical summary did not pass")
    if sum(item["original_cells"] for item in summary["fpgas"]) != original_cells:
        raise ValidationError("physical summary original cell coverage disagrees")
    return {
        "status": "pass",
        "fpgas": len(expected),
        "original_cells": original_cells,
        "transport_cells": transport_cells,
        "merged_cells": original_cells + transport_cells,
        **({"emitted_atoms": emitted_atoms} if backend_id == "open" else {}),
        "backend": backend_id,
        "worst_critical_path_ns": worst_critical_path,
    }


def run_multi_fpga_physical_flow(
    split_root: Path,
    platform_path: Path,
    schedule_path: Path,
    output_dir: Path,
    *,
    backend: str = "open",
    architecture: Optional[Path] = None,
    architecture_id: str = VTR_HARD_BLOCK_PROFILE,
    yosys: Optional[str] = None,
    vpr: Optional[str] = None,
    architecture_importer: Optional[str] = None,
    packed_importer: Optional[str] = None,
    route_checker: Optional[str] = None,
    openparf_install: Optional[Path] = None,
    openparf_python: Optional[Path] = None,
    seed: int = 1,
    route_channel_width: int = 300,
    vivado: Optional[str] = None,
    vivado_max_timing_paths: int = 10000,
    vivado_place_directive: str = "Default",
    vivado_route_directive: str = "Default",
    original_ir_path: Optional[Path] = None,
    assignment_path: Optional[Path] = None,
    routes_path: Optional[Path] = None,
    path_database_path: Optional[Path] = None,
    logic_path_database_path: Optional[Path] = None,
    workers: int = 1,
    managed_storage: bool = False,
    resume: bool = False,
) -> Dict[str, Any]:
    if workers < 1:
        raise ValidationError("physical workers must be at least one")
    if logic_path_database_path is not None and path_database_path is None:
        raise ValidationError(
            "physical logic timing database requires the complete original "
            "STA path database"
        )
    split_root = split_root.resolve()
    manifest_path = split_root / "manifest.json"
    manifest = read_json(manifest_path)
    if manifest.get("schema") != SPLIT_MANIFEST_SCHEMA:
        raise ValidationError("multi-FPGA physical input split manifest is invalid")
    platform = Platform.load(platform_path)
    schedule = read_json(schedule_path)
    position_hints = None
    electrical_binding = None
    if "position_hints" in manifest:
        position_hints = read_json(split_root / manifest["position_hints"])
    if "electrical_binding" in manifest:
        electrical_binding = read_json(split_root / manifest["electrical_binding"])
    if electrical_binding is not None and position_hints is None:
        raise ValidationError(
            "physical Chimew anchors require position hints with electrical binding"
        )
    runtime = build_virtual_runtime(schedule, platform)
    backend_descriptor = physical_backend_descriptor(backend)
    validate_physical_backend_descriptor(backend_descriptor)
    expected_fpgas = [fpga.id for fpga in platform.fpgas]
    logic_context = (
        original_ir_path,
        assignment_path,
        routes_path,
        path_database_path,
    )
    if any(path is not None for path in logic_context) and not all(
        path is not None for path in logic_context
    ):
        raise ValidationError(
            "physical logic timing requires original IR, assignment, routes, "
            "and STA path database together"
        )
    effective_logic_path_database_path = (
        logic_path_database_path
        if logic_path_database_path is not None
        else path_database_path
    )
    prepared_logic_inputs = None
    prepared_local_inputs = None
    if all(path is not None for path in logic_context):
        assert original_ir_path is not None
        assert assignment_path is not None
        assert routes_path is not None
        assert path_database_path is not None
        assert effective_logic_path_database_path is not None
        prepared_logic_inputs = prepare_logic_segment_query_inputs(
            original_ir_path,
            assignment_path,
            effective_logic_path_database_path,
            routes_path,
            schedule_path,
            platform,
        )
        if backend == "open":
            prepared_local_inputs = prepare_vpr_local_path_query_inputs(
                original_ir_path,
                assignment_path,
                path_database_path,
                routes_path,
                original_ir=prepared_logic_inputs.original_ir,
                assignment=prepared_logic_inputs.assignment,
                database=(
                    prepared_logic_inputs.path_database
                    if path_database_path.resolve()
                    == effective_logic_path_database_path.resolve()
                    else None
                ),
                routes=prepared_logic_inputs.routes,
            )
    manifest_fpgas = [item.get("fpga") for item in manifest.get("fpgas", [])]
    if set(manifest_fpgas) != set(expected_fpgas):
        raise ValidationError("split manifest does not cover the BoardDB FPGAs")

    output_dir = output_dir.resolve()
    if output_dir.exists() and not output_dir.is_dir():
        raise EmuFlowError(
            f"multi-FPGA physical output must be an empty directory: {output_dir}"
        )
    if output_dir.exists() and any(output_dir.iterdir()) and not resume:
        raise EmuFlowError(
            f"multi-FPGA physical output must be an empty directory: {output_dir}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    architecture_path = None
    if backend == "open":
        # Import the exact analytical-placement dependency chain before any
        # expensive per-FPGA synthesis or VPR work.  A wrong Python environment
        # must fail fast rather than after all pack/place checkpoints finish.
        validate_openparf_runtime(
            install_root=openparf_install,
            python_executable=openparf_python,
        )
        architecture_root = output_dir / "architecture"
        architecture_root.mkdir(parents=True, exist_ok=True)
        if architecture is None:
            architecture_path = architecture_root / "vtr-flagship.xml"
            architecture_source = fetch_pinned_vtr_architecture(
                architecture_path
            )
        else:
            architecture_input = architecture.resolve()
            if not architecture_input.is_file():
                raise EmuFlowError(
                    f"VTR architecture does not exist: {architecture_input}"
                )
            architecture_path = architecture_root / "vtr-flagship.xml"
            shutil.copy2(architecture_input, architecture_path)
            architecture_source = {
                "status": "pass",
                "mode": "provided",
                "path": str(architecture_path),
                "sha256": _sha256(architecture_path),
                "input_path": str(architecture_input),
            }
    else:
        if architecture is not None:
            raise ValidationError(
                "--physical-architecture applies only to backend=open"
            )
        architecture_source = {
            "status": "pass",
            "mode": "platform-parts",
            "parts": sorted({fpga.part for fpga in platform.fpgas}),
            "provider": "vivado-device-database",
        }

    runtime_controller = split_root / manifest["runtime_controller_rtl"]
    def _run_partition(
        item: Mapping[str, Any],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        fpga_id = item["fpga"]
        source_root = split_root / fpga_id
        fpga_root = output_dir / fpga_id
        fpga_root.mkdir(parents=True, exist_ok=True)
        transport_rtl = split_root / item["transport_rtl"]
        module_match = _TRANSPORT_MODULE.search(
            transport_rtl.read_text(encoding="utf-8")
        )
        if module_match is None:
            raise ValidationError(f"cannot find transport top module for {fpga_id}")
        transport_json = fpga_root / "transport-synthesized.json"
        run_generic_yosys(
            [runtime_controller, transport_rtl],
            module_match.group(1),
            transport_json,
            executable=yosys,
            log_path=fpga_root / "transport-yosys.log",
        )
        transport_ir = import_yosys_json(
            transport_json,
            top=module_match.group(1),
            clocks=("fabric_clk",),
        )
        transport_ir_path = fpga_root / "transport.emuir.json"
        write_json(transport_ir_path, transport_ir.to_dict())
        transport_report = {
            "status": "pass",
            "provider": "yosys-generic-lut6-ff",
            "top": module_match.group(1),
            "instances": len(transport_ir.value["instances"]),
            "nets": len(transport_ir.value["nets"]),
            "output": str(transport_ir_path),
            "sha256": _sha256(transport_ir_path),
        }

        merged_ir = fpga_root / "placement.emuir.json"
        lowering_report = run_placement_ir_lowering(
            source_root / "netlist.json",
            source_root / "transport.json",
            transport_ir_path,
            merged_ir,
            fpga_root / "placement-ir-report.json",
        )
        netlist = read_json(source_root / "netlist.json")
        original_cells = len(netlist["instances"])
        transport_cells = lowering_report["transport_instances"]
        fpga_part = next(
            fpga.part for fpga in platform.fpgas if fpga.id == fpga_id
        )
        fabric_period = runtime["fabric_clock"]["period_ns"]
        dut_period = runtime["virtual_dut_clock"]["nominal_period_ns"]
        cross_period = runtime["timing_model"]["fabric_to_dut_max_delay_ns"]
        stages = {
            "transport_synthesis": transport_report,
            "placement_ir": lowering_report,
        }
        provider_fields: Dict[str, Any] = {}
        if backend == "open":
            if architecture_path is None:
                raise ValidationError("open backend architecture is missing")
            circuit = fpga_root / "partition.eblif"
            eblif_report = emit_vtr_eblif(
                merged_ir, circuit, fpga_root / "vtr-eblif-report.json"
            )
            runtime_sdc = fpga_root / "runtime.sdc"
            runtime_sdc_report = _write_vpr_runtime_sdc(
                runtime_sdc,
                eblif_report,
                fabric_period_ns=fabric_period,
                dut_period_ns=dut_period,
                cross_period_ns=cross_period,
                dut_clock_required=_partition_declares_dut_clock(netlist),
            )
            pack_report = run_vpr_pack_place(
                architecture_path,
                circuit,
                fpga_root / "vpr-pack-place",
                executable=vpr,
                seed=seed,
                resume=resume,
            )
            packed_netlist = Path(
                pack_report["artifacts"]["packed_netlist"]["path"]
            )
            baseline_placement = Path(
                pack_report["artifacts"]["placement"]["path"]
            )
            width, height = read_vpr_placement_dimensions(
                baseline_placement
            )
            architecture_db = fpga_root / "architecture.json"
            timing_db = fpga_root / "timing.json"
            architecture_report = run_vtr_architecture_import(
                input_path=architecture_path,
                architecture_output_path=architecture_db,
                timing_output_path=timing_db,
                architecture_id=architecture_id,
                width=width,
                height=height,
                source_url=architecture_source.get("source"),
                executable=architecture_importer,
            )
            packed_contract = fpga_root / "packed-contract.json"
            packed_report = run_packed_netlist_import(
                packed_netlist,
                packed_contract,
                architecture_path=architecture_path,
                circuit_path=circuit,
                executable=packed_importer,
            )
            boundary_identity_path = Path(
                lowering_report["boundary_identity"]["output"]
            )
            fixed_io_targets = None
            if position_hints is not None and electrical_binding is not None:
                hint_by_entry = {
                    item["schedule_entry"]: item
                    for item in position_hints.get("entries", [])
                }
                binding_by_entry = {}
                for binding in electrical_binding.get("entries", []):
                    for entry_id in binding.get("schedule_entries", []):
                        if entry_id in binding_by_entry:
                            raise ValidationError(
                                "Chimew electrical binding duplicates a schedule entry"
                            )
                        binding_by_entry[entry_id] = binding
                packed = read_json(packed_contract)
                packed_io_names = {
                    cluster["name"]
                    for cluster in packed["clusters"]
                    if cluster["block_type"] == "io"
                }
                fixed_io_targets = {}
                port_map = {
                    (item["port"], item["bit"]): item["packed_block"]
                    for item in eblif_report.get("top_ports", [])
                }
                boundary_endpoints = read_json(boundary_identity_path)["endpoints"]
                packed_groups: Dict[str, str] = {}
                group_packed_blocks: Dict[str, str] = {}
                for endpoint in boundary_endpoints:
                    entry_id = endpoint["schedule_entry"]
                    hint = hint_by_entry.get(entry_id)
                    binding = binding_by_entry.get(entry_id)
                    if hint is None or binding is None:
                        raise ValidationError(
                            "Chimew physical bindings do not cover every boundary endpoint"
                        )
                    if not binding.get("placement_anchor", True):
                        continue
                    # Bind the selected Chimew channel's actual physical site
                    # coordinate.  A lane number is only an electrical
                    # identity and cannot be used as a placement coordinate.
                    if electrical_binding.get("schema", "").endswith("/v2"):
                        endpoint_key = (
                            "pin_a_point"
                            if fpga_id == binding["fpga_a"]
                            else "pin_b_point"
                        )
                        point = binding.get(endpoint_key)
                        bounds_by_fpga = {
                            item["fpga"]: (item["y_min"], item["y_max"])
                            for item in electrical_binding.get("fpga_y_bounds", [])
                        }
                        bounds = bounds_by_fpga.get(fpga_id)
                        if not isinstance(point, dict) or bounds is None:
                            raise ValidationError(
                                "Chimew physical channel coordinates are missing"
                            )
                        low, high = bounds
                        target_y = (float(point["y"]) - low) / (high - low)
                    else:
                        link = next(
                            item for item in platform.links
                            if item.id == binding["link"]
                        )
                        lane_limit = (
                            link.transport_bits_per_cycle_per_direction - 1
                        )
                        target_y = (
                            0.5 if lane_limit == 0 else
                            float(binding["physical_lane"]) / float(lane_limit)
                        )
                    merged = endpoint["merged_ir"]
                    packed_name = port_map.get(
                        (merged["external_port"], merged["external_port_bit"])
                    )
                    if packed_name not in packed_io_names:
                        raise ValidationError(
                            f"Chimew boundary {endpoint['id']!r} has no packed I/O cluster"
                        )
                    _record_chimew_fixed_io_target(
                        fixed_io_targets,
                        packed_groups,
                        group_packed_blocks,
                        packed_name=packed_name,
                        group=binding["group"],
                        target_y=target_y,
                    )
            placement_report = run_packed_openparf_placement(
                packed_contract,
                architecture_db,
                fpga_root / "openparf-placement",
                seed_placement_path=baseline_placement,
                fixed_io_targets=fixed_io_targets,
                openparf_install=openparf_install,
                openparf_python=openparf_python,
            )
            boundary_query_path = fpga_root / "vpr-boundary-query.tsv"
            boundary_query_report = write_vpr_boundary_timing_query(
                merged_ir,
                boundary_identity_path,
                boundary_query_path,
                eblif_report=eblif_report,
            )
            boundary_raw_path = (
                fpga_root / "vpr-route" / "boundary-timing.tsv"
            )
            logic_query_report = None
            logic_identity_path = None
            logic_query_path = None
            logic_raw_path = None
            local_query_report = None
            local_identity_path = None
            local_query_path = None
            local_raw_path = None
            if all(path is not None for path in logic_context):
                logic_identity_path = fpga_root / "logic-segment-identity.json"
                logic_query_path = fpga_root / "vpr-logic-segment-query.tsv"
                logic_query_report = write_vpr_logic_segment_query(
                    original_ir_path,
                    assignment_path,
                    effective_logic_path_database_path,
                    routes_path,
                    schedule_path,
                    platform,
                    merged_ir,
                    boundary_identity_path,
                    fpga_id,
                    logic_query_path,
                    logic_identity_path,
                    eblif_report=eblif_report,
                    prepared_inputs=prepared_logic_inputs,
                )
                logic_raw_path = (
                    fpga_root / "vpr-route" / "logic-segment-timing.tsv"
                )
                local_identity_path = fpga_root / "local-path-identity.json"
                local_query_path = fpga_root / "vpr-local-path-query.tsv"
                local_query_report = write_vpr_local_path_query(
                    original_ir_path,
                    assignment_path,
                    path_database_path,
                    routes_path,
                    merged_ir,
                    fpga_id,
                    local_query_path,
                    local_identity_path,
                    prepared_inputs=prepared_local_inputs,
                )
                local_raw_path = (
                    fpga_root / "vpr-route" / "local-path-timing.tsv"
                )
            route_report = run_vpr_route_packed(
                architecture_path,
                circuit,
                packed_netlist,
                packed_contract,
                Path(placement_report["artifacts"]["vpr_placement"]),
                fpga_root / "vpr-route",
                executable=vpr,
                route_checker=route_checker,
                route_channel_width=route_channel_width,
                boundary_query=boundary_query_path,
                boundary_output=boundary_raw_path,
                logic_query=logic_query_path,
                logic_output=logic_raw_path,
                local_path_query=local_query_path,
                local_path_output=local_raw_path,
                sdc_file=runtime_sdc,
                resume=resume,
            )
            boundary_timing_path = fpga_root / "boundary-timing.json"
            boundary_import_report = import_vpr_boundary_timing(
                boundary_raw_path,
                boundary_identity_path,
                boundary_query_path,
                boundary_timing_path,
            )
            logic_timing_stage = None
            if (
                logic_query_report is not None
                and logic_identity_path is not None
                and logic_raw_path is not None
            ):
                logic_timing_path = fpga_root / "logic-segment-timing.json"
                logic_import_report = import_vpr_logic_segment_timing(
                    logic_raw_path,
                    logic_identity_path,
                    logic_timing_path,
                )
                logic_timing_stage = {
                    "status": "pass",
                    "query": logic_query_report,
                    "import": logic_import_report,
                }
            local_timing_stage = None
            if (
                local_query_report is not None
                and local_identity_path is not None
                and local_raw_path is not None
            ):
                local_timing_path = fpga_root / "local-path-timing.json"
                local_import_report = import_vpr_local_path_timing(
                    local_raw_path,
                    local_identity_path,
                    local_timing_path,
                )
                local_timing_stage = {
                    "status": "pass",
                    "query": local_query_report,
                    "import": local_import_report,
                }
            physical_delays, clock_domain_presence = _physical_clock_delays(
                route_report, eblif_report
            )
            timing = {
                "dut_wns_ns": dut_period - physical_delays["dut"],
                "fabric_wns_ns": fabric_period - physical_delays["fabric"],
                "fabric_to_dut_wns_ns": (
                    cross_period - physical_delays["cross"]
                ),
            }
            endpoint_metrics = route_report["metrics"]
            endpoint_wns = float(endpoint_metrics["setup_worst_slack_ns"])
            endpoint_tns = float(endpoint_metrics["setup_tns_ns"])
            physical_result = {
                "schema": PHYSICAL_PARTITION_RESULT_SCHEMA,
                "status": "pass",
                "identity": {
                    "backend": "open",
                    "fpga": fpga_id,
                    "part": fpga_part,
                },
                "cell_accounting": {
                    "original_cells": original_cells,
                    "transport_cells": transport_cells,
                    "routed_cells": original_cells + transport_cells,
                    "physical_cells": original_cells + transport_cells,
                    "infrastructure_cells": 0,
                    "optimization_cells": 0,
                },
                "closure": {"unrouted_nets": 0, "drc_violations": 0},
                "clocks": {
                    "fabric_period_ns": fabric_period,
                    "dut_period_ns": dut_period,
                },
                "timing": {
                    "wns_ns": endpoint_wns,
                    "tns_ns": endpoint_tns,
                    "failing_endpoints": int(
                        endpoint_metrics["setup_failing_endpoints"]
                    ),
                    "failing_endpoint_constraints": int(
                        endpoint_metrics[
                            "setup_failing_endpoint_constraints"
                        ]
                    ),
                    "timing_met": endpoint_wns >= 0,
                    **timing,
                    "critical_path_ns": physical_delays["overall"],
                    "clock_domain_delays_ns": physical_delays,
                    "clock_domain_presence": clock_domain_presence,
                },
                "artifacts": {
                    "eblif": {
                        "path": str(circuit),
                        "sha256": eblif_report["output_sha256"],
                    },
                    "packed_netlist": pack_report["artifacts"][
                        "packed_netlist"
                    ],
                    "placement": placement_report["artifacts"],
                    "route": route_report.get("artifacts", {}),
                },
            }
            stages.update(
                {
                    "eblif": eblif_report,
                    "vpr_pack_place": pack_report,
                    "architecture_import": architecture_report,
                    "packed_contract": packed_report,
                    "openparf_placement": placement_report,
                    "vpr_route": route_report,
                    "runtime_sdc": runtime_sdc_report,
                    "boundary_timing": {
                        "status": "pass",
                        "query": boundary_query_report,
                        "import": boundary_import_report,
                    },
                    **(
                        {"logic_segment_timing": logic_timing_stage}
                        if logic_timing_stage is not None
                        else {}
                    ),
                    **(
                        {"local_path_timing": local_timing_stage}
                        if local_timing_stage is not None
                        else {}
                    ),
                }
            )
            provider_fields["array"] = {"width": width, "height": height}
        else:
            boundary_identity_path = Path(
                lowering_report["boundary_identity"]["output"]
            )
            logic_identity_path = None
            logic_query_path = None
            logic_query_report = None
            if all(path is not None for path in logic_context):
                logic_identity_path = (
                    fpga_root / "logic-segment-identity.json"
                )
                logic_query_path = fpga_root / "vivado-logic-segment-query.tsv"
                logic_query_report = write_vivado_logic_segment_query(
                    original_ir_path,
                    assignment_path,
                    effective_logic_path_database_path,
                    routes_path,
                    schedule_path,
                    platform,
                    merged_ir,
                    boundary_identity_path,
                    fpga_id,
                    logic_query_path,
                    logic_identity_path,
                    prepared_inputs=prepared_logic_inputs,
                )
            mapped_verilog = fpga_root / "partition.v"
            mapped_report = emit_vivado_mapped_verilog(
                merged_ir,
                mapped_verilog,
                fpga_root / "mapped-verilog-report.json",
            )
            vivado_report = run_vivado_partition_backend(
                fpga=fpga_id,
                part=fpga_part,
                ir_path=merged_ir,
                mapped_verilog_path=mapped_verilog,
                runtime=runtime,
                original_cells=original_cells,
                transport_cells=transport_cells,
                output_dir=fpga_root / "vivado",
                boundary_identity_path=boundary_identity_path,
                logic_identity_path=logic_identity_path,
                logic_query_path=logic_query_path,
                executable=vivado,
                max_timing_paths=vivado_max_timing_paths,
                place_directive=vivado_place_directive,
                route_directive=vivado_route_directive,
            )
            if logic_query_report is not None:
                vivado_report["logic_segment_timing"][
                    "query"
                ] = logic_query_report
            physical_result = vivado_report["result"]
            stages.update(
                {
                    "mapped_verilog": mapped_report,
                    "vivado_implementation": vivado_report,
                }
            )
        validate_physical_partition_result(
            physical_result,
            backend=backend,
            fpga=fpga_id,
            part=fpga_part,
            original_cells=original_cells,
            transport_cells=transport_cells,
        )
        record = {
            "fpga": fpga_id,
            "part": fpga_part,
            "status": "pass",
            "original_cells": original_cells,
            "transport_cells": transport_cells,
            "critical_path_ns": physical_result["timing"][
                "critical_path_ns"
            ],
            **provider_fields,
            "stages": stages,
            "physical_result": physical_result,
        }
        return record, physical_summary_item(physical_result)

    items_by_fpga = {item["fpga"]: item for item in manifest["fpgas"]}
    ordered_items = [items_by_fpga[fpga_id] for fpga_id in expected_fpgas]
    effective_workers = min(workers, len(ordered_items))
    if effective_workers == 1:
        partition_results = [_run_partition(item) for item in ordered_items]
    else:
        results_by_fpga: Dict[
            str, Tuple[Dict[str, Any], Dict[str, Any]]
        ] = {}
        failures = []
        with ThreadPoolExecutor(max_workers=effective_workers) as executor:
            future_to_fpga = {
                executor.submit(_run_partition, item): item["fpga"]
                for item in ordered_items
            }
            for future in as_completed(future_to_fpga):
                fpga_id = future_to_fpga[future]
                try:
                    results_by_fpga[fpga_id] = future.result()
                except Exception as error:  # Preserve all partition failures.
                    failures.append((fpga_id, error))
        if failures:
            details = "; ".join(
                f"{fpga_id}: {error}"
                for fpga_id, error in sorted(failures, key=lambda item: item[0])
            )
            raise EmuFlowError(
                f"parallel multi-FPGA physical flow failed: {details}"
            ) from failures[0][1]
        partition_results = [
            results_by_fpga[fpga_id] for fpga_id in expected_fpgas
        ]
    records = [record for record, _summary in partition_results]
    physical_fpgas = [summary for _record, summary in partition_results]

    physical_summary = {
        "schema": PHYSICAL_SUMMARY_SCHEMA,
        "status": "pass",
        "design": manifest["design"],
        "platform": platform.name,
        "provider": f"{backend}-physical-summary-v1",
        "qualification": backend_descriptor["qualification"],
        "backend": backend_descriptor,
        "fpgas": physical_fpgas,
        "boundary_identities": {
            item["fpga"]: read_json(
                Path(
                    item["stages"]["placement_ir"]["boundary_identity"][
                        "output"
                    ]
                )
            )
            for item in records
        },
    }
    transports_by_fpga = {
        item["fpga"]: read_json(split_root / item["transport"])
        for item in manifest["fpgas"]
    }
    for fpga_id, database in physical_summary[
        "boundary_identities"
    ].items():
        validate_boundary_identity_database(
            database, transports_by_fpga[fpga_id]
        )
    physical_summary["boundary_timing"] = {
        item["fpga"]: read_json(
            Path(
                (
                    item["stages"]["vivado_implementation"][
                        "boundary_timing"
                    ]
                    if backend == "vivado"
                    else item["stages"]["boundary_timing"]
                )["import"]["output"]
            )
        )
        for item in records
    }
    for fpga_id, database in physical_summary[
        "boundary_timing"
    ].items():
        validate_boundary_timing_database(
            database,
            physical_summary["boundary_identities"][fpga_id],
        )
    if all(path is not None for path in logic_context):
        physical_summary["logic_segment_timing"] = {
            item["fpga"]: read_json(
                Path(
                    (
                        item["stages"]["vivado_implementation"][
                            "logic_segment_timing"
                        ]
                        if backend == "vivado"
                        else item["stages"]["logic_segment_timing"]
                    )["import"]["output"]
                )
            )
            for item in records
        }
        for database in physical_summary["logic_segment_timing"].values():
            validate_logic_segment_timing(database)
        if backend == "open":
            physical_summary["local_path_timing"] = {
                item["fpga"]: read_json(
                    Path(
                        item["stages"]["local_path_timing"]["import"][
                            "output"
                        ]
                    )
                )
                for item in records
            }
            for database in physical_summary["local_path_timing"].values():
                validate_local_path_timing(database)
    physical_summary["validation"] = validate_physical_summary(
        physical_summary, runtime, platform
    )
    write_json(output_dir / "physical-summary.json", physical_summary)
    report = {
        "schema": MULTI_FPGA_PHYSICAL_SCHEMA,
        "status": "pass",
        "provider": "phase6-emuir+physical-backend-v1",
        "backend": backend_descriptor,
        "design": manifest["design"],
        "platform": platform.name,
        "split_manifest": {
            "path": str(manifest_path),
            "sha256": _sha256(manifest_path),
        },
        "architecture": architecture_source,
        "execution": {
            "requested_workers": workers,
            "effective_workers": effective_workers,
            "ordering": "boarddb-fpga-order",
            "pack_place_resume": resume,
            "route_resume": resume,
        },
        "expected_fpgas": expected_fpgas,
        "fpgas": records,
    }
    if managed_storage:
        report["physical_summary_ref"] = "physical-summary.json"
    else:
        report["physical_summary"] = physical_summary
    report["summary"] = validate_multi_fpga_physical_report(
        report, physical_summary
    )
    write_json(output_dir / "multi-fpga-physical-flow-report.json", report)
    return report
