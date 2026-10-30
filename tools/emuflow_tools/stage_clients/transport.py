from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from emuflow.validation.equivalence import simulate_partition_equivalence
from emuflow.transport.phase6_oracle import _static_exact_equivalence_evidence
from emuflow.common.errors import ValidationError
from emuflow_tools.document_io import read_json
from emuflow_tools.transport.chimew.chimew_binding_storage import read_chimew_binding
from emuflow.ir.ir import EmuIR
from emuflow.platform.platform import Platform
from emuflow_tools.transport.pin_plan_storage import read_pin_plan
from emuflow.common.native_tools import require_native_schema
from emuflow_tools.transport.position_hints_storage import read_position_hints
from emuflow_tools.transport.pin_planning import CHIMEW_PIN_PLAN_PROVIDER, validate_pin_plan
from emuflow_tools.transport.transport_cost import predict_transport_resources, validate_transport_cost_database
from emuflow.transport.runtime import build_virtual_runtime
from emuflow.transport.split_netlist_storage import read_compact_emuir_control_metadata, read_split_resource_reservations, validate_split_netlists_sqlite
from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
from emuflow_tools.transport.transport_storage import read_lane_map, read_transport, read_virtual_binding


PHASE6_REPORT_SCHEMA = "emuflow.phase6-report/v1"




def _virtual_board_artifact_paths(platform: Platform) -> Dict[str, Any]:
    return {
        "wiring": "virtual_board_wiring.json",
        "binding": "virtual_board_binding.json",
        "xdc": {
            fpga.id: f"{fpga.id}/virtual_data_pins.xdc"
            for fpga in platform.fpgas
        },
        "differential_io": {
            fpga.id: f"{fpga.id}/virtual_differential_io.sv"
            for fpga in platform.fpgas
        },
    }


def _validate_virtual_board_artifacts(
    platform: Platform,
    schedule: Mapping[str, Any],
    artifacts: Mapping[str, Any],
    board: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> Dict[str, Any]:
    from emuflow_tools.transport.virtual_board import validate_virtual_binding, validate_virtual_board

    wiring_validation = validate_virtual_board(platform, board)
    binding_validation = validate_virtual_binding(
        platform, board, schedule, artifacts, binding
    )
    return {
        "status": "pass",
        "qualification": binding_validation["qualification"],
        "physical_backend_status": binding_validation["physical_backend_status"],
        "wiring_validation": wiring_validation,
        "binding_validation": binding_validation,
    }


def run_phase6(
    ir_path: Path,
    assignment_path: Path,
    schedule_path: Path,
    platform_path: Path,
    output_dir: Path,
    equivalence_cycles: int = 1,
    equivalence_seed: int = 20260727,
    pin_plan_path: Optional[Path] = None,
    position_hints_path: Optional[Path] = None,
    electrical_binding_path: Optional[Path] = None,
    transport_cost_path: Optional[Path] = None,
    managed_storage: bool = False,
    clock_periods: Optional[Mapping[str, float]] = None,
) -> Dict[str, Any]:
    source = read_compact_emuir_control_metadata(ir_path)
    require_native_schema("sqlite-publication-request-v1")
    require_native_schema("split-netlists-v3")
    require_native_schema("transport-stage-request-v1")
    design_name = source["design"]["name"]
    declared_clocks = [item["id"] for item in source.get("clocks", [])]
    if clock_periods is None:
        if len(declared_clocks) != 1:
            raise ValidationError(
                "multi-clock Phase 6 requires an explicit period for every "
                "declared DUT clock"
            )
        # A single domain has no relative-rate ambiguity. Its absolute target
        # period remains owned by TimingPathDB; the runtime calendar needs only
        # one tick, so a unit period is an exact standalone Phase 6 contract.
        clock_periods = {declared_clocks[0]: 1.0}
    elif set(clock_periods) != set(declared_clocks):
        raise ValidationError(
            "Phase 6 clock periods must exactly cover declared EmuIR clocks"
        )
    assignment_index_path = (
        assignment_path
        if assignment_path.suffix == ".sqlite"
        else assignment_path.with_name("assignment.sqlite")
    )
    if not assignment_index_path.is_file():
        raise ValidationError(
            "Phase 6 requires the Phase 3 assignment.sqlite data-plane "
            "artifact; rerun Phase 3 with this EmuFlow version"
        )
    schedule = read_tdm_schedule(schedule_path)
    if getattr(schedule, "store_path", None) is None:
        raise ValidationError(
            "Phase 6 requires the current typed ScheduleStore; rerun Phase 5"
        )
    platform = Platform.load(platform_path)
    pin_plan = read_pin_plan(pin_plan_path) if pin_plan_path is not None else None
    position_hints = (
        read_position_hints(position_hints_path)
        if position_hints_path is not None
        else None
    )
    if pin_plan is None and position_hints is not None:
        raise ValueError("pin_plan_path is required with position_hints_path")
    if pin_plan is None and electrical_binding_path is not None:
        raise ValueError("pin_plan_path is required with electrical_binding_path")
    if pin_plan is not None and position_hints is None:
        raise ValueError("position_hints_path is required with pin_plan_path")
    if pin_plan is not None and electrical_binding_path is None:
        raise ValueError("electrical_binding_path is required with a Chimew pin plan")
    electrical_binding = read_chimew_binding(electrical_binding_path) if electrical_binding_path is not None else None
    from emuflow_tools.stage_clients.transport_native import run_native_phase6
    from emuflow.scheduling.tdm import is_sampled_virtual_wire_schedule

    runtime = build_virtual_runtime(schedule, platform, clock_periods)
    virtual_board = None
    if platform.virtual_board_model is not None:
        from emuflow_tools.transport.virtual_board import build_virtual_board
        virtual_board = build_virtual_board(platform)
    transport_cost = None
    if transport_cost_path is not None:
        transport_cost = read_json(transport_cost_path.resolve())
        validate_transport_cost_database(transport_cost, expected_platform=platform.name)
    # Independent behavioral replay is bounded to the existing small-model
    # qualification scope. The native stage always checks constructive
    # source/split/transport coverage, including for large production input.
    counts = source.get("record_counts", {})
    equivalence = None
    if int(counts.get("cell_records", 0)) <= 10_000 and int(counts.get("endpoint_records", 0)) <= 100_000:
        qualification_schedule = schedule.materialize()
        assignment_json = assignment_path.with_name("assignment.json") if assignment_path.suffix == ".sqlite" else assignment_path
        from emuflow.transport.phase6_oracle import read_bounded_assignment
        assignment = read_bounded_assignment(ir_path, assignment_json)
        ir = EmuIR.load(ir_path)
        if is_sampled_virtual_wire_schedule(qualification_schedule):
            equivalence = _static_exact_equivalence_evidence(ir, assignment,
                qualification_schedule, cycles=equivalence_cycles,
                seed=equivalence_seed, clock_periods=clock_periods)
        else:
            equivalence = simulate_partition_equivalence(ir, assignment,
                qualification_schedule, cycles=equivalence_cycles, seed=equivalence_seed)
    return run_native_phase6(ir_path, assignment_index_path, schedule,
        platform, platform_path, output_dir, design=source["design"],
        clocks=source["clocks"], virtual_board=virtual_board, runtime=runtime,
        equivalence=equivalence, managed_storage=managed_storage,
        transport_cost=transport_cost, transport_cost_path=transport_cost_path,
        pin_plan=pin_plan, position_hints=position_hints, electrical_binding=electrical_binding)


def validate_phase6(
    ir_path: Path,
    assignment_path: Path,
    schedule_path: Path,
    platform_path: Path,
    manifest_path: Path,
    pin_plan_path: Optional[Path] = None,
    position_hints_path: Optional[Path] = None,
    electrical_binding_path: Optional[Path] = None,
    *,
    replay_equivalence: bool = False,
    schedule_value: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    source = read_compact_emuir_control_metadata(ir_path)
    design_name = source["design"]["name"]
    assignment_index_path = (
        assignment_path
        if assignment_path.suffix == ".sqlite"
        else assignment_path.with_name("assignment.sqlite")
    )
    if not assignment_index_path.is_file():
        raise ValidationError("Phase 6 assignment.sqlite is missing")
    # Checkpoint orchestration may already have parsed a large schedule to
    # validate the cross-layer contract.  Reuse that exact object instead of
    # scanning and decoding the artifact again.
    schedule = (
        read_tdm_schedule(schedule_path)
        if schedule_value is None
        else schedule_value
    )
    platform = Platform.load(platform_path)
    manifest = read_json(manifest_path)
    if manifest.get("resource_reservations_by_fpga", {}) != (
        read_split_resource_reservations(assignment_index_path)
    ):
        raise ValidationError(
            "split manifest resource reservations disagree with Phase 3"
        )
    transports = {
        item["fpga"]: read_transport(
            manifest_path.parent / item["transport"]
        )
        for item in manifest["fpgas"]
    }
    pin_plan = (
        read_pin_plan(pin_plan_path)
        if pin_plan_path is not None
        else (
            read_pin_plan(manifest_path.parent / manifest["pin_plan"])
            if "pin_plan" in manifest
            else None
        )
    )
    if pin_plan is not None:
        resolved_positions = (
            position_hints_path
            if position_hints_path is not None
            else manifest_path.parent / manifest["position_hints"]
        )
        validate_pin_plan(
            schedule,
            platform,
            read_position_hints(resolved_positions),
            pin_plan,
        )
        if pin_plan.get("provider") == CHIMEW_PIN_PLAN_PROVIDER:
            if electrical_binding_path is None and "electrical_binding" not in manifest:
                raise ValidationError(
                    "Chimew Phase 6 manifest has no electrical binding"
                )
            resolved_binding = (
                electrical_binding_path
                if electrical_binding_path is not None
                else manifest_path.parent / manifest["electrical_binding"]
            )
            from emuflow_tools.transport.chimew.chimew_phase6 import validate_chimew_phase6_binding

            validate_chimew_phase6_binding(
                schedule,
                platform,
                pin_plan,
                read_chimew_binding(resolved_binding),
            )
        elif electrical_binding_path is not None:
            raise ValueError(
                "electrical_binding_path is supported only with a Chimew pin plan"
            )
    elif position_hints_path is not None:
        raise ValueError(
            "pin_plan_path or manifest pin plan is required with "
            "position_hints_path"
        )
    elif electrical_binding_path is not None:
        raise ValueError(
            "pin_plan_path or manifest pin plan is required with "
            "electrical_binding_path"
        )
    clock_periods = manifest.get("clock_periods")
    if not isinstance(clock_periods, dict) or not clock_periods:
        raise ValidationError("Phase 6 manifest lacks DUT clock periods")
    build_virtual_runtime(schedule, platform, clock_periods)
    split_relative = manifest.get("netlist_storage")
    if split_relative != "split-netlists.sqlite":
        raise ValidationError("Phase 6 manifest lacks split-netlists SQLite")
    transport_storage = manifest.get("transport_storage")
    if (
        transport_storage is not None
        and transport_storage != "transport.sqlite"
    ):
        raise ValidationError("Phase 6 transport SQLite path is invalid")
    validation = validate_split_netlists_sqlite(
        manifest_path.parent / split_relative,
        ir_path,
        assignment_index_path,
        expected_design=design_name,
        expected_platform=platform.name,
    )
    validation.update({
        "scheduled_hops": len(schedule["entries"]),
        "transport_endpoints": sum(
            len(value["endpoints"]) for value in transports.values()
        ),
        "lane_entries": len(
            read_lane_map(
                manifest_path.parent / manifest["lane_map"]
            )["entries"]
        ),
    })
    if validation["transport_endpoints"] != 2 * validation["scheduled_hops"]:
        raise ValidationError("transport endpoint coverage disagrees")
    if validation["lane_entries"] != validation["scheduled_hops"]:
        raise ValidationError("lane-map coverage disagrees")
    transport_cost_relative = manifest.get("transport_cost")
    if transport_cost_relative is not None:
        if transport_cost_relative != "transport-cost.json":
            raise ValidationError("Phase 6 transport cost path is invalid")
        transport_cost = read_json(manifest_path.parent / transport_cost_relative)
        validate_transport_cost_database(
            transport_cost, expected_platform=platform.name
        )
        validation["transport_cost_prediction"] = predict_transport_resources(
            transport_cost, transports
        )
    virtual_metadata = manifest.get("virtual_board_binding")
    if platform.virtual_board_model is not None:
        if virtual_metadata != _virtual_board_artifact_paths(platform):
            raise ValidationError("Phase 6 declared virtual board requires wiring, binding and pin constraints")
        from emuflow_tools.physical.fixed_io import prepare_fixed_io
        root = manifest_path.parent
        artifacts = {
            "manifest": manifest,
            "transports": transports,
            "lane_map": read_lane_map(root / manifest["lane_map"]),
        }
        try:
            board = read_json(root / virtual_metadata["wiring"])
            binding = read_virtual_binding(root / virtual_metadata["binding"])
            validation["virtual_board_binding"] = _validate_virtual_board_artifacts(
                platform, schedule, artifacts, board, binding
            )
            with tempfile.TemporaryDirectory(prefix="physical-io-validation-") as temporary:
                prepare_fixed_io(
                    root / virtual_metadata["binding"], root / manifest["lane_map"],
                    root / virtual_metadata["wiring"], schedule,
                    {fpga: root / relative for fpga, relative in virtual_metadata["xdc"].items()},
                    {fpga: root / relative for fpga, relative in virtual_metadata["differential_io"].items()},
                    Path(temporary) / "accepted",
                )
        except (OSError, ValueError) as error:
            raise ValidationError(f"Phase 6 virtual board artifact is unreadable: {error}") from error
    elif virtual_metadata is not None:
        raise ValidationError("Phase 6 virtual binding requires a declared platform model")

    from emuflow.scheduling.tdm import is_sampled_virtual_wire_schedule

    if replay_equivalence and is_sampled_virtual_wire_schedule(schedule):
        assignment_json_path = (
            assignment_path.with_name("assignment.json")
            if assignment_path.suffix == ".sqlite"
            else assignment_path
        )
        from emuflow.transport.phase6_oracle import read_bounded_assignment
        assignment = read_bounded_assignment(ir_path, assignment_json_path)
        ir = EmuIR.load(ir_path)
        materialize_schedule = getattr(schedule, "materialize", None)
        qualification_schedule = (
            materialize_schedule()
            if materialize_schedule is not None
            else schedule
        )
        validation["static_exact_equivalence"] = (
            _static_exact_equivalence_evidence(
                ir,
                assignment,
                qualification_schedule,
                cycles=16,
                seed=20260727,
                clock_periods=clock_periods,
            )
        )
    return validation
