"""Checked Phase-6-to-physical backend for every FPGA partition."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import sqlite3
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Sequence, Tuple

from .boundary_timing import (
    boundary_timing_header,
    validate_boundary_identity_database,
    validate_boundary_timing_database,
)
from .boundary_identity_storage import read_boundary_identity_storage
from .boundary_timing_storage import (
    read_boundary_timing_storage,
    write_boundary_timing_storage,
)
from .chimew_binding_storage import (
    open_chimew_binding_schedule_entry_lookup,
    read_chimew_binding,
)
from .errors import EmuFlowError, ValidationError
from .store_publication import bind_view_sources, bind_store_source
from .io import read_json, write_json
from .logic_segment_timing import (
    LOGIC_SEGMENT_TIMING_SCHEMA,
    prepare_logic_segment_query_inputs,
    validate_logic_segment_timing,
    write_vivado_logic_segment_query,
    write_xilinx_logic_segment_query,
)
from .local_path_timing import (
    local_path_timing_header,
    prepare_local_path_query_inputs,
    read_local_path_identity,
    validate_local_path_timing,
    write_xilinx_local_path_query,
)
from .local_path_timing_storage import (
    read_local_path_timing_storage,
    write_local_path_timing_storage,
)
from .ir import EMUIR_SCHEMA, EmuIR
from .emuir_storage import open_emuir_sqlite_read_only
from .lowering import (
    PLACEMENT_IR_REPORT_SCHEMA,
    run_placement_ir_lowering_from_split,
    run_placement_ir_lowering_from_value,
)
from .split_netlist_storage import SplitNetlistView
from .netlist import SPLIT_MANIFEST_SCHEMA
from .openparf import validate_openparf_runtime
from .physical_backend import (
    PHYSICAL_PARTITION_RESULT_SCHEMA,
    physical_backend_descriptor,
    physical_summary_item,
    validate_physical_backend_descriptor,
    validate_physical_partition_result,
)
from .physical_summary_storage import (
    publish_boundary_identity_store,
    publish_boundary_timing_store,
    publish_logic_segment_timing_store,
    publish_local_path_timing_store,
    publish_physical_evidence,
)
from .platform import Platform
from .partition_limits import planning_resource_capacities
from .position_hints_storage import PositionHintsView, read_position_hints
from .resource_budget import validate_device_resource_usage
from .resources import RESOURCE_FIELDS, ResourceVector
from .rapidwright_provider import validate_rapidwright_provider_manifest
from .runtime import (
    PHYSICAL_SUMMARY_SCHEMA,
    build_virtual_runtime,
    validate_physical_summary,
)
from .synthesis import run_generic_yosys_stream
from .transport_fanout import buffer_transport_high_fanout_records
from .tdm_schedule_storage import (
    canonical_tdm_schedule_sha256,
    read_tdm_schedule,
)
from .transport_storage import read_lane_map, read_transport, read_virtual_binding
from .virtual_board import (
    build_virtual_io_physical_contract,
    validate_virtual_binding,
    validate_virtual_board,
)
from .vivado_physical import run_indexed_vivado_partition
from .vivado_netlist import emit_vivado_mapped_verilog
from .yosys import import_yosys_json_indexed
from .xilinx_physical_backend import (
    run_rapidwright_openparf_native_candidate_backend,
)


MULTI_FPGA_PHYSICAL_SCHEMA = "emuflow.multi-fpga-physical-flow/v1"
_TRANSPORT_MODULE = re.compile(r"^module\s+([A-Za-z_][A-Za-z0-9_$]*)", re.M)
_INACTIVE_PARTITION_PROVIDER = "inactive-empty-partition-v1"


@contextmanager
def _open_position_hint_lookup(
    position_hints: Mapping[str, Any],
) -> Iterator[Callable[[str], bool]]:
    """Return a bounded-memory membership lookup for physical I/O anchors."""

    if isinstance(position_hints, PositionHintsView):
        connection = sqlite3.connect(
            f"file:{position_hints.store_path.resolve()}?mode=ro&immutable=1",
            uri=True,
        )
        try:
            def contains(entry_id: str) -> bool:
                try:
                    row = connection.execute(
                        "SELECT json_extract(record_json,'$.schedule_entry') "
                        "FROM entries WHERE schedule_entry=?",
                        (entry_id,),
                    ).fetchone()
                except sqlite3.DatabaseError as error:
                    raise ValidationError(
                        "position-hint entry is corrupt"
                    ) from error
                if row is None:
                    return False
                if row[0] != entry_id:
                    raise ValidationError(
                        "position-hint indexed identity disagrees"
                    )
                return True

            yield contains
        finally:
            connection.close()
        return

    entries = position_hints.get("entries", [])
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise ValidationError("position hint entries must be an array")
    entry_ids = set()
    for record in entries:
        if not isinstance(record, Mapping):
            raise ValidationError("position hint is malformed")
        entry_id = record.get("schedule_entry")
        if not isinstance(entry_id, str) or not entry_id:
            raise ValidationError("position hint is malformed")
        entry_ids.add(entry_id)
    yield entry_ids.__contains__


def _implementation_stage(
    item: Mapping[str, Any], backend: str, stage: str
) -> Mapping[str, Any]:
    """Return one physical stage from its backend-specific report owner."""
    if backend not in {"rapidwright", "vivado"}:
        raise ValidationError(f"unsupported physical backend {backend!r}")
    if item.get("active") is False:
        return item["stages"][stage]
    owner = "vivado_implementation" if backend == "vivado" else "rapidwright_implementation"
    return item["stages"][owner][stage]


def _indexed_emuir_counts(path: Path) -> tuple[int, int]:
    """Read transport synthesis counts without reconstructing its graph."""

    connection = open_emuir_sqlite_read_only(path)
    try:
        return (
            int(connection.execute("SELECT count(*) FROM cell_records").fetchone()[0]),
            int(connection.execute("SELECT count(*) FROM net_records").fetchone()[0]),
        )
    finally:
        connection.close()


def _indexed_emuir_resource_totals(path: Path) -> Dict[str, int]:
    """Aggregate indexed resources without materializing transport cells."""

    connection = open_emuir_sqlite_read_only(path)
    try:
        raw = {
            str(resource): int(amount)
            for resource, amount in connection.execute(
                "SELECT resource, SUM(amount) FROM instance_resources "
                "GROUP BY resource"
            )
            if int(amount)
        }
    finally:
        connection.close()
    return ResourceVector.from_mapping(
        raw, "indexed transport resources"
    ).to_dict(include_zeros=False)


def _validate_transport_resource_reservation(
    fpga_id: str,
    used: Mapping[str, int],
    reservations: Mapping[str, Mapping[str, int]],
) -> Dict[str, Any]:
    reserved = reservations.get(fpga_id, {})
    violations = [
        {
            "resource": field,
            "used": int(used.get(field, 0)),
            "reserved": int(reserved.get(field, 0)),
        }
        for field in RESOURCE_FIELDS
        if int(used.get(field, 0)) > int(reserved.get(field, 0))
    ]
    if violations:
        raise ValidationError(
            f"transport resources exceed sealed Phase 3 reservation for "
            f"{fpga_id}: {violations}; automatic capacity relaxation is forbidden"
        )
    fields = sorted(set(used) | set(reserved))
    return {
        "status": "pass",
        "provider": "phase3-sealed-transport-resource-reservation-v1",
        "used": {field: int(used.get(field, 0)) for field in fields},
        "reserved": {field: int(reserved.get(field, 0)) for field in fields},
        "headroom": {
            field: int(reserved.get(field, 0)) - int(used.get(field, 0))
            for field in fields
        },
        "violations": 0,
        "automatic_relaxation": False,
    }


def _partition_has_physical_work(
    netlist: Mapping[str, Any], transport: Mapping[str, Any]
) -> bool:
    """Return whether one installed FPGA needs an implementation image.

    A selected DUT partition has instances/nets/ports.  A routing-only relay
    has transport endpoints or internal source/shadow signals.  Only a device
    with neither kind of evidence is inactive.  In particular, do not infer
    inactivity merely from an empty DUT instance list: that would silently
    drop a legal multi-hop relay.
    """

    fields = (
        (netlist, "instances"),
        (netlist, "nets"),
        (netlist, "ports"),
        (transport, "endpoints"),
        (transport, "source_signals"),
        (transport, "shadow_signals"),
    )
    for document, field in fields:
        value = document.get(field)
        if not isinstance(value, list):
            raise ValidationError(
                f"physical partition {field} inventory must be an array"
            )
        if value:
            return True
    return False


def _inactive_transport_ir(transport: Mapping[str, Any]) -> EmuIR:
    """Create the exact empty transport IR for an unused installed FPGA."""

    design = transport.get("design")
    fpga = transport.get("fpga")
    if not isinstance(design, str) or not design or not isinstance(fpga, str):
        raise ValidationError("inactive partition transport identity is invalid")
    return EmuIR(
        {
            "schema": EMUIR_SCHEMA,
            "design": {
                "name": f"{design}__{fpga}__inactive_transport",
                "top": f"{design}__{fpga}__inactive_transport",
                "source_format": _INACTIVE_PARTITION_PROVIDER,
            },
            "ports": [],
            "instances": [],
            "nets": [],
            "clocks": [],
            "warnings": [],
        }
    )


def _write_inactive_logic_timing(
    identity_path: Path, output_path: Path
) -> Dict[str, Any]:
    from .logic_segment_identity_storage import (
        read_logic_segment_identity_storage,
    )

    identity = read_logic_segment_identity_storage(identity_path)
    header = {
        "schema": LOGIC_SEGMENT_TIMING_SCHEMA,
        "status": "pass",
        "design": identity["design"],
        "platform": identity["platform"],
        "fpga": identity["fpga"],
        "provider": _INACTIVE_PARTITION_PROVIDER,
        "qualification": "not-applicable-empty-partition",
        "system_routes_sha256": identity["system_routes_sha256"],
        "tdm_schedule_sha256": identity["tdm_schedule_sha256"],
        "coverage": {
            **identity["coverage"],
            "endpoint_exact_segments": 0,
            "cone_bound_segments": 0,
        },
        **(
            {"semantic_contract_sha256": identity["semantic_contract_sha256"]}
            if "semantic_contract_sha256" in identity
            else {}
        ),
    }
    from .logic_segment_timing_storage import write_logic_segment_timing_storage

    try:
        view = write_logic_segment_timing_storage(
            output_path,
            header=header,
            segments=(),
            unsupported_member_paths=identity["unsupported_member_paths"],
            source_bindings=bind_view_sources(
                (identity, "logic-segment-identity-store-v1")
            ),
        )
        try:
            validation = view.validate_timing()
        finally:
            view.close()
    finally:
        close = getattr(identity, "close", None)
        if callable(close):
            close()
    return {**validation, "output": str(output_path)}


def _write_inactive_local_timing(
    identity_path: Path, output_path: Path
) -> Dict[str, Any]:
    identity = read_local_path_identity(identity_path)
    try:
        header = local_path_timing_header(
            identity,
            provider=_INACTIVE_PARTITION_PROVIDER,
            qualification="not-applicable-empty-partition",
        )
        with write_local_path_timing_storage(
            output_path,
            header,
            (),
            source_bindings=bind_view_sources((identity, "local-path-timing-store-v1")),
        ) as database:
            validation = validate_local_path_timing(database)
        return {**validation, "output": str(output_path)}
    finally:
        close = getattr(identity, "close", None)
        if callable(close):
            close()


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
    worst_critical_path = 0.0
    active_fpgas = 0
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
        active = item.get("active", True)
        if not isinstance(active, bool):
            raise ValidationError(
                f"physical activity marker for {fpga_id} is invalid"
            )
        if not active:
            if (
                result.get("implementation_status") != "inactive"
                or item["original_cells"] != 0
                or item["transport_cells"] != 0
                or "boundary_timing" not in stages
                or any(
                    name in stages
                    for name in (
                        "rapidwright_implementation",
                        "vivado_implementation",
                    )
                )
            ):
                raise ValidationError(
                    f"inactive physical partition {fpga_id} is inconsistent"
                )
        elif result.get("implementation_status") == "inactive":
            raise ValidationError(
                f"active physical partition {fpga_id} is marked inactive"
            )
        elif backend_id == "rapidwright":
            implementation = stages.get("rapidwright_implementation")
            rapidwright_required = {
                "mapped_netlist",
                "packing",
                "placement",
                "route",
                "routed_timing",
                "opensta",
                "boundary_timing",
            }
            if (
                not isinstance(implementation, dict)
                or not rapidwright_required.issubset(implementation)
            ):
                raise ValidationError(
                    f"RapidWright physical stages for {fpga_id} are incomplete"
                )
        elif backend_id == "vivado":
            if not {"mapped_verilog", "vivado_implementation"}.issubset(
                stages
            ):
                raise ValidationError(
                    f"Vivado physical stages for {fpga_id} are incomplete"
                )
        else:  # The backend descriptor validator should make this unreachable.
            raise ValidationError(
                f"physical backend {backend_id!r} is unsupported"
            )
        active_fpgas += int(active)
        original_cells += item["original_cells"]
        transport_cells += item["transport_cells"]
        critical_path = result["timing"].get("critical_path_ns", 0.0)
        if not isinstance(critical_path, (int, float)) or critical_path < 0:
            raise ValidationError(
                f"physical critical path for {fpga_id} is invalid"
            )
        worst_critical_path = max(worst_critical_path, float(critical_path))
    if active_fpgas <= 0:
        raise ValidationError("multi-FPGA physical flow has no active FPGA")
    embedded_summary = report.get("physical_summary")
    summary_ref = report.get("physical_summary_ref")
    if embedded_summary is not None and summary_ref is not None:
        raise ValidationError(
            "multi-FPGA physical report duplicates its physical summary"
        )
    summary = physical_summary if physical_summary is not None else embedded_summary
    if summary is None:
        if summary_ref != "physical-summary.json":
            raise ValidationError("multi-FPGA physical summary reference is invalid")
    else:
        if not isinstance(summary, dict) or summary.get("status") != "pass":
            raise ValidationError("multi-FPGA physical summary did not pass")
        if (
            sum(item["original_cells"] for item in summary["fpgas"])
            != original_cells
        ):
            raise ValidationError(
                "physical summary original cell coverage disagrees"
            )
    return {
        "status": "pass",
        "fpgas": len(expected),
        "active_fpgas": active_fpgas,
        "inactive_fpgas": len(expected) - active_fpgas,
        "original_cells": original_cells,
        "transport_cells": transport_cells,
        "merged_cells": original_cells + transport_cells,
        "backend": backend_id,
        "worst_critical_path_ns": worst_critical_path,
    }


def run_multi_fpga_physical_flow(
    split_root: Path,
    platform_path: Path,
    schedule_path: Path,
    output_dir: Path,
    *,
    backend: str = "rapidwright",
    architecture: Optional[Path] = None,
    yosys: Optional[str] = None,
    openparf_install: Optional[Path] = None,
    openparf_python: Optional[Path] = None,
    seed: int = 1,
    vivado: Optional[str] = None,
    vivado_place_directive: str = "Default",
    vivado_route_directive: str = "Default",
    rapidwright_jar: Optional[Path] = None,
    rapidwright_java: Optional[Path] = None,
    rapidwright_classes: Optional[Path] = None,
    rapidwright_java_source: Optional[Path] = None,
    rapidwright_device_data: Optional[Path] = None,
    rapidwright_timing_data: Optional[Path] = None,
    rapidwright_opensta: Optional[str] = None,
    rapidwright_native_constraints: Optional[Path] = None,
    rapidwright_provider_manifest: Optional[Path] = None,
    original_ir_path: Optional[Path] = None,
    assignment_path: Optional[Path] = None,
    routes_path: Optional[Path] = None,
    path_database_path: Optional[Path] = None,
    logic_path_database_path: Optional[Path] = None,
    workers: int = 1,
    resume: bool = False,
    fpga_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    validate_physical_backend_descriptor(physical_backend_descriptor(backend))
    if workers < 1:
        raise ValidationError("physical workers must be at least one")
    if (rapidwright_native_constraints is None) != (
        rapidwright_provider_manifest is None
    ):
        raise ValidationError(
            "RapidWright native placement requires both the native constraints "
            "and provider manifest"
        )
    if backend == "rapidwright" and (
        rapidwright_native_constraints is None
        or rapidwright_provider_manifest is None
    ):
        raise ValidationError(
            "openparf-native requires source-sealed RapidWright native "
            "constraints and provider manifest"
        )
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
    raw_reservations = manifest.get("resource_reservations_by_fpga", {})
    if (
        not isinstance(raw_reservations, Mapping)
        or any(not isinstance(row, Mapping) for row in raw_reservations.values())
    ):
        raise ValidationError(
            "split manifest resource reservations are malformed"
        )
    transport_resource_reservations = {
        str(fpga_id): {str(field): amount for field, amount in row.items()}
        for fpga_id, row in raw_reservations.items()
    }
    # Revalidate the sealed reservation against this exact BoardDB.  The
    # returned planning capacities are consumed in Phase 3; Phase 7 retains
    # the physical capacity and checks the synthesized transport separately.
    planning_resource_capacities(
        platform.fpgas, transport_resource_reservations
    )
    schedule = read_tdm_schedule(schedule_path)
    position_hints = None
    electrical_binding = None
    virtual_board = None
    virtual_binding = None
    virtual_artifacts = manifest.get("virtual_board_binding")
    expected_fpgas = [fpga.id for fpga in platform.fpgas]
    if fpga_ids is not None:
        if (
            isinstance(fpga_ids, (str, bytes))
            or not fpga_ids
            or any(not isinstance(fpga, str) for fpga in fpga_ids)
            or len(set(fpga_ids)) != len(fpga_ids)
            or set(fpga_ids) - set(expected_fpgas)
        ):
            raise ValidationError("physical FPGA selection is empty, duplicated, or unknown")
        selected_fpgas = [fpga for fpga in expected_fpgas if fpga in fpga_ids]
    else:
        selected_fpgas = expected_fpgas
    if "position_hints" in manifest:
        position_hints = read_position_hints(
            split_root / manifest["position_hints"]
        )
    if "electrical_binding" in manifest:
        electrical_binding = read_chimew_binding(
            split_root / manifest["electrical_binding"]
        )
    if electrical_binding is not None and position_hints is None:
        raise ValidationError(
            "physical Chimew anchors require position hints with electrical binding"
        )
    if virtual_artifacts is not None:
        if not isinstance(virtual_artifacts, Mapping):
            raise ValidationError("physical virtual-board artifact manifest is invalid")
        wiring_ref = virtual_artifacts.get("wiring")
        binding_ref = virtual_artifacts.get("binding")
        xdc_refs = virtual_artifacts.get("xdc")
        adapter_refs = virtual_artifacts.get("differential_io")
        if (
            not isinstance(wiring_ref, str)
            or not isinstance(binding_ref, str)
            or not isinstance(xdc_refs, Mapping)
            or not isinstance(adapter_refs, Mapping)
            or set(xdc_refs) != set(expected_fpgas)
            or set(adapter_refs) != set(expected_fpgas)
        ):
            raise ValidationError("physical virtual-board artifact coverage is invalid")
        virtual_board = read_json(split_root / wiring_ref)
        virtual_binding = read_virtual_binding(split_root / binding_ref)
        validate_virtual_board(platform, virtual_board)
        virtual_phase6_artifacts = {
            "lane_map": read_lane_map(split_root / manifest["lane_map"]),
            "transports": {
                item["fpga"]: read_transport(
                    split_root / item["transport"]
                )
                for item in manifest["fpgas"]
            },
        }
        validate_virtual_binding(
            platform,
            virtual_board,
            schedule,
            virtual_phase6_artifacts,
            virtual_binding,
        )
        canonical = lambda value: hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        if (
            virtual_binding.get("board_sha256") != canonical(virtual_board)
            or virtual_binding.get("schedule_sha256")
            != canonical_tdm_schedule_sha256(schedule)
        ):
            raise ValidationError(
                "physical virtual-board binding source seals disagree"
            )
        if backend not in {"rapidwright", "vivado"}:
            raise ValidationError(
                f"the {backend} physical backend does not yet consume the fixed "
                "differential-I/O contract"
            )
    clock_periods = manifest.get("clock_periods")
    if not isinstance(clock_periods, dict) or not clock_periods:
        raise ValidationError(
            "split manifest lacks the Phase 6 DUT clock-period contract"
        )
    runtime = build_virtual_runtime(schedule, platform, clock_periods)
    backend_descriptor = physical_backend_descriptor(backend)
    validate_physical_backend_descriptor(backend_descriptor)
    output_dir = output_dir.resolve()
    if output_dir.exists() and not output_dir.is_dir():
        raise EmuFlowError(
            f"multi-FPGA physical output must be an empty directory: {output_dir}"
        )
    if output_dir.exists() and any(output_dir.iterdir()) and not resume:
        raise EmuFlowError(
            f"multi-FPGA physical output must be an empty directory: {output_dir}"
        )
    if backend == "rapidwright":
        # Validate the compiled OpenPARF/PyTorch runtime before reading the
        # graph-sized EmuIR and timing databases.  A loader mismatch must fail
        # in seconds, not after minutes of deterministic input preparation.
        validate_openparf_runtime(
            install_root=openparf_install,
            python_executable=openparf_python,
        )
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
        assignment_reservations = prepared_logic_inputs.assignment.get(
            "constraints", {}
        ).get("resource_reservations_by_fpga", {})
        if assignment_reservations != transport_resource_reservations:
            raise ValidationError(
                "split manifest resource reservations disagree with the "
                "Phase 3 assignment"
            )
        if backend in {"rapidwright", "vivado"}:
            prepared_local_inputs = prepare_local_path_query_inputs(
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

    output_dir.mkdir(parents=True, exist_ok=True)
    architecture_path = None
    if backend == "rapidwright":
        if architecture is None:
            raise ValidationError(
                "RapidWright backend requires --physical-architecture with "
                "the imported Xilinx ArchitectureDB"
            )
        architecture_path = architecture.resolve()
        if not architecture_path.is_file():
            raise EmuFlowError(
                f"RapidWright architecture does not exist: {architecture_path}"
            )
        required_runtime = {
            "rapidwright_jar": rapidwright_jar,
            "rapidwright_java": rapidwright_java,
            "rapidwright_device_data": rapidwright_device_data,
            "rapidwright_timing_data": rapidwright_timing_data,
        }
        missing_runtime = sorted(
            name
            for name, path in required_runtime.items()
            if (
                path is None
                or (
                    name in {
                        "rapidwright_device_data",
                        "rapidwright_timing_data",
                    }
                    and not path.is_dir()
                )
                or (
                    name not in {
                        "rapidwright_device_data",
                        "rapidwright_timing_data",
                    }
                    and not path.is_file()
                )
            )
        )
        if missing_runtime:
            raise ValidationError(
                "RapidWright backend runtime is incomplete: "
                + ", ".join(sorted(set(missing_runtime)))
            )
        if rapidwright_java_source is None:
            rapidwright_java_source = (
                Path(__file__).resolve().parents[2]
                / "scripts/rapidwright/EmuFlowRWRoute.java"
            )
        if not rapidwright_java_source.is_file():
            raise ValidationError("RapidWright Java adapter source is missing")
        if rapidwright_classes is None:
            rapidwright_classes = output_dir / ".rapidwright-classes"
        provider_identity = None
        if rapidwright_provider_manifest is not None:
            provider_identity = validate_rapidwright_provider_manifest(
                read_json(rapidwright_provider_manifest.resolve())
            )
            if any(
                fpga.part != provider_identity["part"]
                for fpga in platform.fpgas
            ):
                raise ValidationError(
                    "RapidWright provider part disagrees with the BoardDB"
                )
        architecture_source = {
            "status": "pass",
            "mode": "provided-xilinx-architecture-db",
            "path": str(architecture_path),
            "sha256": _sha256(architecture_path),
            "provider": "rapidwright-xilinx-device-v1",
            **(
                {
                    "provider_revision": provider_identity["revision"],
                    "part": provider_identity["part"],
                }
                if provider_identity is not None
                else {}
            ),
        }
    else:
        if architecture is not None:
            raise ValidationError(
                "--physical-architecture applies only to backend=rapidwright"
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
    ) -> Dict[str, Any]:
        fpga_id = item["fpga"]
        source_root = split_root / fpga_id
        fpga_root = output_dir / fpga_id
        fpga_root.mkdir(parents=True, exist_ok=True)
        virtual_io_contract = None
        if virtual_binding is not None:
            assert isinstance(virtual_artifacts, Mapping)
            virtual_io_contract = build_virtual_io_physical_contract(
                virtual_binding,
                fpga_id,
                xdc_path=split_root / virtual_artifacts["xdc"][fpga_id],
                adapter_path=(
                    split_root / virtual_artifacts["differential_io"][fpga_id]
                ),
            )
            write_json(
                fpga_root / "virtual-io-physical-contract.json",
                virtual_io_contract,
                compact=True,
            )
        netlist_path = split_root / item["netlist"]
        transport_path = split_root / item["transport"]
        transport = read_transport(transport_path)
        split_selector = item.get("netlist_selector")
        if split_selector is not None:
            with SplitNetlistView(netlist_path, split_selector) as split_view:
                split_counts = split_view.counts()
            netlist = None
            original_cells = split_counts["instances"]
            physical_work = any(split_counts.values()) or any(
                transport.get(field, [])
                for field in ("endpoints", "source_signals", "shadow_signals")
            )
        else:
            netlist = read_json(netlist_path)
            original_cells = len(netlist["instances"])
            physical_work = _partition_has_physical_work(netlist, transport)

        def lower_placement_ir(
            transport_ir_path: Path, merged_ir: Path
        ) -> Dict[str, Any]:
            if split_selector is not None:
                return run_placement_ir_lowering_from_split(
                    netlist_path,
                    split_selector,
                    transport_path,
                    transport_ir_path,
                    merged_ir,
                    fpga_root / "placement-ir-report.json",
                )
            assert netlist is not None
            return run_placement_ir_lowering_from_value(
                netlist,
                transport_path,
                transport_ir_path,
                merged_ir,
                fpga_root / "placement-ir-report.json",
            )
        fpga_part = next(
            fpga.part for fpga in platform.fpgas if fpga.id == fpga_id
        )
        fabric_period = runtime["fabric_clock"]["period_ns"]
        dut_period = runtime["virtual_dut_clock"]["nominal_period_ns"]
        dut_periods = {
            clock: float(record["nominal_period_ns"])
            for clock, record in runtime["virtual_dut_clocks"].items()
        }
        cross_period = runtime["timing_model"]["fabric_to_dut_max_delay_ns"]
        if not physical_work:
            # Do not synthesize the shared controller or invoke a placer for an
            # FPGA that is not part of this design's selected/relay subgraph.
            # The old path created five controller-only cells and could leave
            # OpenPARF's ISM loop running forever at zero HPWL.  Retain exact
            # empty coverage evidence instead of inventing physical timing.
            transport_ir_path = fpga_root / "transport.emuir.sqlite"
            _inactive_transport_ir(transport).save(transport_ir_path)
            transport_report = {
                "status": "pass",
                "provider": _INACTIVE_PARTITION_PROVIDER,
                "execution": "not-run-empty-partition",
                "instances": 0,
                "nets": 0,
                "output": str(transport_ir_path),
                "sha256": _sha256(transport_ir_path),
            }
            merged_ir = fpga_root / "placement.emuir.sqlite"
            lowering_report = lower_placement_ir(
                transport_ir_path, merged_ir
            )
            if (
                lowering_report.get("instances") != 0
                or lowering_report.get("nets") != 0
                or lowering_report.get("transport_instances") != 0
            ):
                raise ValidationError(
                    f"inactive partition {fpga_id} produced physical cells"
                )
            boundary_identity_path = Path(
                lowering_report["boundary_identity"]["output"]
            )
            boundary_identity = read_boundary_identity_storage(
                boundary_identity_path, allow_legacy=True
            )
            boundary_timing_path = fpga_root / "boundary-timing.json"
            boundary_header = boundary_timing_header(
                boundary_identity,
                provider=_INACTIVE_PARTITION_PROVIDER,
                qualification="not-applicable-empty-partition",
            )
            with write_boundary_timing_storage(
                boundary_timing_path,
                boundary_header,
                (),
                source_bindings=bind_view_sources(
                    (boundary_identity, "boundary-identity-store-v1")
                ),
            ) as boundary_timing:
                boundary_validation = validate_boundary_timing_database(
                    boundary_timing, boundary_identity
                )
            close_boundary = getattr(boundary_identity, "close", None)
            if callable(close_boundary):
                close_boundary()
            stages: Dict[str, Any] = {
                "transport_synthesis": transport_report,
                "placement_ir": lowering_report,
                "boundary_timing": {
                    "status": "pass",
                    "execution": "not-applicable-empty-partition",
                    "import": {
                        **boundary_validation,
                        "output": str(boundary_timing_path),
                    },
                },
            }
            if transport_resource_reservations:
                stages["transport_resource_reservation"] = (
                    _validate_transport_resource_reservation(
                        fpga_id, {}, transport_resource_reservations
                    )
                )
            artifact_paths = {
                "placement_ir": merged_ir,
                "boundary_identity": boundary_identity_path,
                "boundary_timing": boundary_timing_path,
            }
            if all(path is not None for path in logic_context):
                assert original_ir_path is not None
                assert assignment_path is not None
                assert routes_path is not None
                assert path_database_path is not None
                assert effective_logic_path_database_path is not None
                logic_identity_path = fpga_root / "logic-segment-identity.json"
                logic_query_path = fpga_root / "logic-segment-query.tsv"
                logic_writer = (
                    write_xilinx_logic_segment_query
                    if backend == "rapidwright"
                    else write_vivado_logic_segment_query
                )
                logic_query_report = logic_writer(
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
                logic_timing_path = fpga_root / "logic-segment-timing.json"
                logic_import_report = _write_inactive_logic_timing(
                    logic_identity_path, logic_timing_path
                )
                stages["logic_segment_timing"] = {
                    "status": "pass",
                    "execution": "not-applicable-empty-partition",
                    "query": logic_query_report,
                    "import": logic_import_report,
                }
                artifact_paths.update(
                    {
                        "logic_identity": logic_identity_path,
                        "logic_timing": logic_timing_path,
                    }
                )
                if backend in {"rapidwright", "vivado"}:
                    local_identity_path = fpga_root / "local-path-identity.json"
                    local_query_path = fpga_root / "local-path-query.tsv"
                    local_writer = (
                        write_xilinx_local_path_query
                    )
                    local_query_report = local_writer(
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
                    local_timing_path = fpga_root / "local-path-timing.json"
                    local_import_report = _write_inactive_local_timing(
                        local_identity_path, local_timing_path
                    )
                    stages["local_path_timing"] = {
                        "status": "pass",
                        "execution": "not-applicable-empty-partition",
                        "query": local_query_report,
                        "import": local_import_report,
                    }
                    artifact_paths.update(
                        {
                            "local_identity": local_identity_path,
                            "local_timing": local_timing_path,
                        }
                    )
            physical_result = {
                "schema": PHYSICAL_PARTITION_RESULT_SCHEMA,
                "status": "pass",
                "implementation_status": "inactive",
                "identity": {
                    "backend": backend,
                    "fpga": fpga_id,
                    "part": fpga_part,
                },
                "cell_accounting": {
                    "original_cells": 0,
                    "transport_cells": 0,
                    "routed_cells": 0,
                    "physical_cells": 0,
                    "infrastructure_cells": 0,
                    "optimization_cells": 0,
                },
                "closure": {
                    "unrouted_nets": 0,
                    "drc_violations": 0,
                    "drc_warnings": 0,
                },
                "clocks": {
                    "fabric_period_ns": fabric_period,
                    "dut_period_ns": dut_period,
                },
                "timing": {
                    "applicable": False,
                    "wns_ns": 0.0,
                    "tns_ns": 0.0,
                    "failing_endpoints": 0,
                    "failing_endpoint_constraints": 0,
                    "timing_met": True,
                    "dut_wns_ns": 0.0,
                    "fabric_wns_ns": 0.0,
                    "fabric_to_dut_wns_ns": 0.0,
                    "critical_path_ns": 0.0,
                    "clock_domain_delays_ns": {
                        "overall": 0.0,
                        "fabric": 0.0,
                        "dut": 0.0,
                        "cross": 0.0,
                    },
                    "clock_domain_presence": {
                        "fabric": False,
                        "dut": False,
                        "cross": False,
                    },
                },
                "hard_resources": {},
                "artifacts": {
                    name: {"path": str(path), "sha256": _sha256(path)}
                    for name, path in sorted(artifact_paths.items())
                },
            }
            validate_physical_partition_result(
                physical_result,
                backend=backend,
                fpga=fpga_id,
                part=fpga_part,
                original_cells=0,
                transport_cells=0,
            )
            record = {
                "fpga": fpga_id,
                "part": fpga_part,
                "status": "pass",
                "active": False,
                "original_cells": 0,
                "transport_cells": 0,
                "critical_path_ns": 0.0,
                "stages": stages,
                "physical_result": physical_result,
            }
            return record

        transport_rtl = split_root / item["transport_rtl"]
        transport_sources = tuple(
            bind_store_source(path, "yosys-source-document-v1")
            for path in (runtime_controller, transport_rtl)
        )
        module_match = _TRANSPORT_MODULE.search(
            transport_rtl.read_text(encoding="utf-8")
        )
        if module_match is None:
            raise ValidationError(f"cannot find transport top module for {fpga_id}")
        transport_ir_path = fpga_root / "transport.emuir.sqlite"
        fanout_report = None

        def finalize_transport_emuir(connection):
            nonlocal fanout_report
            fanout_report = buffer_transport_high_fanout_records(connection)

        run_generic_yosys_stream(
            [runtime_controller, transport_rtl],
            module_match.group(1),
            lambda stream: import_yosys_json_indexed(
                Path("<generic-yosys-stdout>"),
                transport_ir_path,
                top=module_match.group(1),
                clocks=("fabric_clk",),
                _source_stream=stream,
                _finalize_emuir=finalize_transport_emuir,
                source_bindings=transport_sources,
            ),
            executable=yosys,
            log_path=fpga_root / "transport-yosys.log",
        )
        if fanout_report is None:
            raise ValidationError("transport EmuIR finalization did not complete")
        fanout_report_path = fpga_root / "transport-fanout-buffering.json"
        write_json(fanout_report_path, fanout_report, compact=True)
        transport_instances, transport_nets = _indexed_emuir_counts(
            transport_ir_path
        )
        transport_resources = _indexed_emuir_resource_totals(
            transport_ir_path
        )
        transport_report = {
            "status": "pass",
            "provider": "yosys-generic-lut6-ff",
            "top": module_match.group(1),
            "instances": transport_instances,
            "nets": transport_nets,
            "resources": transport_resources,
            "fanout_buffering": {
                "output": str(fanout_report_path),
                "sha256": _sha256(fanout_report_path),
                "summary": fanout_report["summary"],
                "policy": fanout_report["policy"],
            },
            "output": str(transport_ir_path),
            "sha256": _sha256(transport_ir_path),
        }

        merged_ir = fpga_root / "placement.emuir.sqlite"
        lowering_report = lower_placement_ir(transport_ir_path, merged_ir)
        transport_cells = lowering_report["transport_instances"]
        stages = {
            "transport_synthesis": transport_report,
            "placement_ir": lowering_report,
        }
        if transport_resource_reservations:
            stages["transport_resource_reservation"] = (
                _validate_transport_resource_reservation(
                    fpga_id,
                    transport_resources,
                    transport_resource_reservations,
                )
            )
        provider_fields: Dict[str, Any] = {}
        if backend == "rapidwright":
            if architecture_path is None:
                raise ValidationError("RapidWright architecture is missing")
            boundary_identity_path = Path(
                lowering_report["boundary_identity"]["output"]
            )
            logic_identity_path = None
            logic_query_report = None
            local_identity_path = None
            local_query_report = None
            if all(path is not None for path in logic_context):
                logic_identity_path = fpga_root / "logic-segment-identity.json"
                logic_query_report = write_xilinx_logic_segment_query(
                    original_ir_path,
                    assignment_path,
                    effective_logic_path_database_path,
                    routes_path,
                    schedule_path,
                    platform,
                    merged_ir,
                    boundary_identity_path,
                    fpga_id,
                    fpga_root / "xilinx-logic-segment-query.tsv",
                    logic_identity_path,
                    prepared_inputs=prepared_logic_inputs,
                )
                local_identity_path = fpga_root / "local-path-identity.json"
                local_query_report = write_xilinx_local_path_query(
                    original_ir_path,
                    assignment_path,
                    path_database_path,
                    routes_path,
                    merged_ir,
                    fpga_id,
                    fpga_root / "xilinx-local-path-query.tsv",
                    local_identity_path,
                    prepared_inputs=prepared_local_inputs,
                )
            assert rapidwright_jar is not None
            assert rapidwright_java is not None
            assert rapidwright_classes is not None
            assert rapidwright_java_source is not None
            assert rapidwright_device_data is not None
            assert rapidwright_timing_data is not None
            rapidwright_report = run_rapidwright_openparf_native_candidate_backend(
                fpga=fpga_id,
                part=fpga_part,
                merged_ir_path=merged_ir,
                architecture_path=architecture_path,
                runtime={**runtime, "resource_budget": next(
                    fpga.to_dict() for fpga in platform.fpgas if fpga.id == fpga_id
                )},
                original_cells=original_cells,
                transport_cells=transport_cells,
                output_dir=fpga_root / "rapidwright",
                boundary_identity_path=boundary_identity_path,
                rapidwright_jar=rapidwright_jar,
                java=rapidwright_java,
                # Each worker receives an isolated class/runtime-data root.
                # RapidWright may extract device data beneath user.home, so a
                # shared directory would introduce cross-partition races.
                classes_dir=rapidwright_classes / fpga_id,
                java_source=rapidwright_java_source,
                device_data_root=rapidwright_device_data,
                timing_data_dir=rapidwright_timing_data,
                openparf_install=openparf_install,
                openparf_python=openparf_python,
                openparf_native_constraints=rapidwright_native_constraints,
                openparf_provider_manifest=rapidwright_provider_manifest,
                opensta=rapidwright_opensta,
                logic_identity_path=logic_identity_path,
                local_identity_path=local_identity_path,
                virtual_io_contract=virtual_io_contract,
                resume=resume,
            )
            if logic_query_report is not None:
                rapidwright_report["logic_segment_timing"][
                    "query"
                ] = logic_query_report
            if local_query_report is not None:
                rapidwright_report["local_path_timing"][
                    "query"
                ] = local_query_report
            physical_result = rapidwright_report["result"]
            stages["rapidwright_implementation"] = rapidwright_report
            if virtual_io_contract is not None:
                stages["virtual_io_implementation"] = rapidwright_report[
                    "virtual_io_implementation"
                ]
        else:
            boundary_identity_path = Path(
                lowering_report["boundary_identity"]["output"]
            )
            mapped_verilog = fpga_root / "partition.v"
            mapped_report = emit_vivado_mapped_verilog(
                merged_ir,
                mapped_verilog,
                fpga_root / "mapped-verilog-report.json",
            )
            logic_identity_path = None
            logic_query_path = None
            logic_query_report = None
            local_identity_path = None
            local_query_path = None
            local_query_report = None
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
                from .vivado_physical_timing import write_vivado_local_query

                local_identity_path = fpga_root / "vivado-local-identity.sqlite"
                local_query_path = fpga_root / "vivado-local-query.tsv"
                local_query_report = write_vivado_local_query(
                    merged_ir, fpga_id, local_query_path, local_identity_path,
                    prepared_local_inputs,
                )
            vivado_report = run_indexed_vivado_partition(
                fpga=fpga_id,
                part=fpga_part,
                ir_path=merged_ir,
                mapped_verilog_path=mapped_verilog,
                runtime={**runtime, "resource_budget": next(
                    fpga.to_dict() for fpga in platform.fpgas if fpga.id == fpga_id
                )},
                original_cells=original_cells,
                transport_cells=transport_cells,
                output_dir=fpga_root / "vivado",
                boundary_identity_path=boundary_identity_path,
                logic_identity_path=logic_identity_path,
                logic_query_path=logic_query_path,
                executable=vivado,
                place_directive=vivado_place_directive,
                route_directive=vivado_route_directive,
                mapped_report=mapped_report,
                virtual_io_contract=virtual_io_contract,
                local_identity_path=local_identity_path,
                local_query_path=local_query_path,
            )
            if logic_query_report is not None:
                vivado_report["logic_segment_timing"][
                    "query"
                ] = logic_query_report
            if local_query_report is not None:
                vivado_report["local_path_timing"]["query"] = local_query_report
            if virtual_io_contract is not None:
                stages["virtual_io_implementation"] = vivado_report["virtual_io_implementation"]
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
            "active": True,
            "original_cells": original_cells,
            "transport_cells": transport_cells,
            "critical_path_ns": physical_result["timing"][
                "critical_path_ns"
            ],
            **provider_fields,
            "stages": stages,
            "physical_result": physical_result,
        }
        return record

    items_by_fpga = {item["fpga"]: item for item in manifest["fpgas"]}
    ordered_items = [items_by_fpga[fpga_id] for fpga_id in selected_fpgas]

    def run_and_record(item: Mapping[str, Any]) -> Dict[str, Any]:
        record = _run_partition(item)
        write_json(
            output_dir / record["fpga"] / "partition-record.json",
            record,
            compact=True,
        )
        return record

    effective_workers = min(workers, len(ordered_items))
    if effective_workers == 1:
        partition_results = [run_and_record(item) for item in ordered_items]
    else:
        results_by_fpga: Dict[str, Dict[str, Any]] = {}
        failures = []
        with ThreadPoolExecutor(max_workers=effective_workers) as executor:
            future_to_fpga = {
                executor.submit(run_and_record, item): item["fpga"]
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
            results_by_fpga[fpga_id] for fpga_id in selected_fpgas
        ]
    execution = {
        "seed": seed,
        "requested_workers": workers,
        "effective_workers": effective_workers,
        "ordering": "boarddb-fpga-order",
        "pack_place_resume": resume,
        "route_resume": resume,
        **({"vivado_implementation": {
            "place_directive": vivado_place_directive,
            "route_directive": vivado_route_directive,
        }} if backend == "vivado" else {}),
        **(
            {"rapidwright_placer": "openparf-native"}
            if backend == "rapidwright"
            else {}
        ),
    }
    if fpga_ids is not None:
        # Explicit workers may complete independently on different HPC nodes.
        # Their records are scratch in the active experiment. Only the common
        # assembler may publish the complete BoardDB physical summary.
        report = {
            "schema": "emuflow.physical-partition-batch/v1",
            "status": "pass",
            "scope": "selected-physical-partitions; whole-design Phase7C pending",
            "backend": backend_descriptor,
            "architecture": architecture_source,
            "execution": execution,
            "expected_fpgas": expected_fpgas,
            "selected_fpgas": selected_fpgas,
            "source": {
                name: {"path": str(path.resolve()), "sha256": _sha256(path)}
                for name, path in (
                    ("split_manifest", manifest_path),
                    ("platform", platform_path),
                    ("schedule", schedule_path),
                )
            },
            "records": [
                {"fpga": record["fpga"], "path": str(path), "sha256": _sha256(path)}
                for record in partition_results
                for path in [output_dir / record["fpga"] / "partition-record.json"]
            ],
            "summary": {"status": "pass", "completed_partitions": len(partition_results)},
        }
        write_json(output_dir / "physical-partition-batch.json", report, compact=True)
        return report
    return _assemble_multi_fpga_physical_results(
        records=partition_results,
        manifest=manifest,
        manifest_path=manifest_path,
        platform=platform,
        runtime=runtime,
        split_root=split_root,
        output_dir=output_dir,
        backend_descriptor=backend_descriptor,
        architecture_source=architecture_source,
        include_logic_timing=all(path is not None for path in logic_context),
        execution=execution,
    )


def _assemble_multi_fpga_physical_results(
    *,
    records: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    manifest_path: Path,
    platform: Platform,
    runtime: Mapping[str, Any],
    split_root: Path,
    output_dir: Path,
    backend_descriptor: Mapping[str, Any],
    architecture_source: Mapping[str, Any],
    execution: Mapping[str, Any],
    include_logic_timing: bool,
) -> Dict[str, Any]:
    """Validate and aggregate completed partitions without rerunning tools.

    Fresh and explicitly continued runs use the same physical and timing
    validators. Fixed differential I/O remains required by the split manifest.
    Whole-original-design Phase 7C remains a separate completion gate.
    """

    backend = backend_descriptor["id"]
    expected_fpgas = [fpga.id for fpga in platform.fpgas]
    by_fpga = {}
    for record in records:
        fpga = record.get("fpga")
        if not isinstance(fpga, str) or fpga in by_fpga:
            raise ValidationError("physical result FPGA identity is invalid or duplicated")
        if record.get("status") != "pass":
            raise ValidationError(f"physical flow for {fpga} did not pass")
        by_fpga[fpga] = record
    if set(by_fpga) != set(expected_fpgas):
        raise ValidationError("physical result FPGA coverage is incomplete")
    records = [by_fpga[fpga] for fpga in expected_fpgas]
    requires_virtual_io = manifest.get("virtual_board_binding") is not None
    if requires_virtual_io:
        if backend not in {"rapidwright", "vivado"}:
            raise ValidationError("completed fixed differential I/O requires a supported Xilinx backend")
        for record in records:
            if record.get("active") is False:
                continue
            evidence = record.get("stages", {}).get("virtual_io_implementation")
            if not isinstance(evidence, Mapping) or evidence.get("status") != "pass":
                raise ValidationError(
                    f"completed fixed differential I/O evidence for {record['fpga']} is missing"
                )
    if backend in {"rapidwright", "vivado"}:
        fpga_by_id = {fpga.id: fpga for fpga in platform.fpgas}
        for record in records:
            if record.get("active") is False:
                continue
            usage = record["physical_result"].get("resource_usage", {}).get("physical")
            if not isinstance(usage, Mapping) or not isinstance(usage.get("counts"), Mapping):
                raise ValidationError(f"physical resource budget evidence for {record['fpga']} is missing")
            expected = validate_device_resource_usage(
                usage["counts"], fpga_by_id[record["fpga"]].to_dict(),
                stage="post-physical-materialization",
            )
            if dict(usage) != expected:
                raise ValidationError(f"physical resource budget evidence for {record['fpga']} disagrees with platform")
    physical_fpgas = [
        physical_summary_item(record["physical_result"]) for record in records
    ]
    output_dir.mkdir(parents=True, exist_ok=True)

    physical_summary = {
        "schema": PHYSICAL_SUMMARY_SCHEMA,
        "status": "pass",
        "design": manifest["design"],
        "platform": platform.name,
        "provider": f"{backend}-physical-summary-v1",
        "qualification": backend_descriptor["qualification"],
        "backend": backend_descriptor,
        "fpgas": physical_fpgas,
        "boundary_identities": {},
    }
    if backend == "vivado":
        from .physical_summary_storage import publish_optimization_proofs
        proof_references = {}
        for record in records:
            if record.get("active") is False:
                continue
            proof = record["stages"]["vivado_implementation"].get("optimization_proofs")
            if proof is None:
                # Historical measured-only reports have no elimination owner.
                # Any eliminated row still fails the global proof requirement.
                continue
            proof_references[record["fpga"]] = publish_optimization_proofs(
                Path(proof["output"]), output_dir, Path(record["fpga"]) / "optimization-proofs.sqlite",
                fpga=record["fpga"], mapped_sha256=record["stages"]["mapped_verilog"]["mapped_index"]["sha256"])
        if proof_references:
            physical_summary["optimization_proofs"] = proof_references
    if requires_virtual_io:
        physical_summary["virtual_io_implementation"] = {
            "status": "pass",
            "provider": ("rapidwright-fixed-lvds-iob-routed-boundary-v1" if backend == "rapidwright" else "vivado-fixed-lvds-routed-boundary-v1"),
            "fpgas": {
                item["fpga"]: item["stages"]["virtual_io_implementation"]
                for item in records
                if item.get("active") is not False
            },
            "differential_pairs": sum(
                item["stages"]["virtual_io_implementation"][
                    "differential_pairs"
                ]
                for item in records
                if item.get("active") is not False
            ),
        }
    transports_by_fpga = {
        item["fpga"]: read_transport(split_root / item["transport"])
        for item in manifest["fpgas"]
    }
    physical_summary["boundary_timing"] = {}
    for item in records:
        fpga_id = item["fpga"]
        identity_path = Path(
            item["stages"]["placement_ir"]["boundary_identity"]["output"]
        )
        _identity_selector, identity_reference = publish_boundary_identity_store(
            identity_path,
            output_dir,
            Path(fpga_id) / identity_path.name,
        )
        physical_summary["boundary_identities"][fpga_id] = identity_reference
        with read_boundary_identity_storage(
            output_dir / identity_reference["path"]
        ) as identity_database:
            validate_boundary_identity_database(
                identity_database, transports_by_fpga[fpga_id]
            )
            timing_path = Path(
                _implementation_stage(item, backend, "boundary_timing")[
                    "import"
                ]["output"]
            )
            _timing_selector, timing_reference = publish_boundary_timing_store(
                timing_path,
                output_dir,
                Path(fpga_id) / timing_path.name,
            )
            with read_boundary_timing_storage(
                output_dir / timing_reference["path"]
            ) as timing_database:
                validate_boundary_timing_database(
                    timing_database,
                    identity_database,
                )
        physical_summary["boundary_timing"][fpga_id] = timing_reference
    if include_logic_timing:
        physical_summary["logic_segment_timing"] = {}
        logic_semantic_seals = set()
        logic_route_seals = set()
        logic_schedule_seals = set()
        for item in records:
            path = Path(
                _implementation_stage(item, backend, "logic_segment_timing")[
                    "import"
                ]["output"]
            )
            selector, reference = publish_logic_segment_timing_store(
                path,
                output_dir,
                Path(item["fpga"]) / "logic-segment-timing.json",
            )
            source = selector["source"]
            if (
                source["design"] != physical_summary["design"]
                or source["platform"] != physical_summary["platform"]
                or source["fpga"] != item["fpga"]
            ):
                raise ValidationError(
                    "logic segment timing source disagrees with physical summary"
                )
            logic_semantic_seals.add(source["semantic_contract_sha256"])
            logic_route_seals.add(source["system_routes_sha256"])
            logic_schedule_seals.add(source["tdm_schedule_sha256"])
            physical_summary["logic_segment_timing"][item["fpga"]] = reference
        if len(logic_semantic_seals) != 1:
            raise ValidationError(
                "logic segment timing semantic seals disagree"
            )
        if len(logic_route_seals) != 1 or len(logic_schedule_seals) != 1:
            raise ValidationError(
                "logic segment timing route/schedule seals disagree"
            )
        physical_summary["logic_segment_system_routes_sha256"] = next(
            iter(logic_route_seals)
        )
        physical_summary["logic_segment_tdm_schedule_sha256"] = next(
            iter(logic_schedule_seals)
        )
        logic_semantic_seal = next(iter(logic_semantic_seals))
        if logic_semantic_seal is not None:
            physical_summary[
                "logic_segment_semantic_contract_sha256"
            ] = logic_semantic_seal
        if backend in {"rapidwright", "vivado"}:
            physical_summary["local_path_timing"] = {}
            for item in records:
                path = Path(
                    _implementation_stage(item, backend, "local_path_timing")[
                        "import"
                    ]["output"]
                )
                _selector, reference = publish_local_path_timing_store(
                    path,
                    output_dir,
                    Path(item["fpga"]) / path.name,
                )
                with read_local_path_timing_storage(
                    output_dir / reference["path"],
                    expected_design=physical_summary["design"],
                    expected_fpga=item["fpga"],
                ) as database:
                    validate_local_path_timing(database)
                physical_summary["local_path_timing"][item["fpga"]] = reference
    physical_summary["validation"] = validate_physical_summary(
        physical_summary, runtime, platform
    )
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
        "execution": dict(execution),
        "expected_fpgas": expected_fpgas,
        "fpgas": records,
    }
    report["physical_summary_ref"] = "physical-summary.json"
    report["summary"] = validate_multi_fpga_physical_report(
        report, physical_summary
    )
    write_json(output_dir / "physical-summary.json", physical_summary)
    write_json(output_dir / "multi-fpga-physical-flow-report.json", report)
    return report
