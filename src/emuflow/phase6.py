from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .equivalence import (
    exhaustively_verify_static_exact_partition_equivalence,
    simulate_partition_equivalence,
    simulate_static_exact_partition_equivalence,
)
from .errors import ValidationError
from .io import read_json, write_json
from .managed_json_storage import pack_managed_json
from .ir import EmuIR
from .netlist import (
    anchors_to_xdc_template,
    transport_to_systemverilog,
)
from .platform import Platform
from .pin_planning import CHIMEW_PIN_PLAN_PROVIDER, validate_pin_plan
from .ppro_calibrated_platform import (
    predict_transport_resources,
    validate_transport_cost_database,
)
from .runtime import (
    build_virtual_runtime,
    virtual_runtime_controller_to_systemverilog,
)
from .split_netlist_storage import (
    build_streaming_split_metadata,
    read_compact_emuir_control_metadata,
    validate_split_netlists_sqlite,
    write_split_netlists_sqlite,
)


PHASE6_REPORT_SCHEMA = "emuflow.phase6-report/v1"


def _static_exact_equivalence_evidence(
    ir: EmuIR,
    assignment: Mapping[str, Any],
    schedule: Mapping[str, Any],
    *,
    cycles: int,
    seed: int,
    clock_periods: Optional[Mapping[str, float]],
) -> Dict[str, Any]:
    random_traces = [
        simulate_static_exact_partition_equivalence(
            ir,
            assignment,
            schedule,
            cycles=cycles,
            seed=seed + offset,
            clock_periods=clock_periods,
        )
        for offset in range(3)
    ]
    exhaustive = None
    exhaustive_skip = None
    try:
        exhaustive = exhaustively_verify_static_exact_partition_equivalence(
            ir,
            assignment,
            schedule,
            max_variables=12,
            clock_periods=clock_periods,
        )
    except ValidationError as error:
        message = str(error)
        if (
            "variable limit exceeded" not in message
            and "does not support memory state" not in message
        ):
            raise
        exhaustive_skip = {
            "status": "not-run",
            "reason": message,
            "qualification": "large-model-boundary-not-a-proof",
        }
    evidence = {
        "status": "pass",
        "provider": "static-exact-event-driven-macro-cycle-equivalence-v1",
        "semantic_contract_sha256": schedule[
            "semantic_contract_sha256"
        ],
        "random_traces": random_traces,
        "random_trace_count": len(random_traces),
        "random_macro_cycles": sum(item["cycles"] for item in random_traces),
        "cycles": sum(item["cycles"] for item in random_traces),
        "compared_state_bits": sum(
            item["compared_state_bits"] for item in random_traces
        ),
        "compared_output_bits": sum(
            item["compared_output_bits"] for item in random_traces
        ),
        "source_full_evaluations": sum(
            item["source_full_evaluations"] for item in random_traces
        ),
        "reference_full_evaluations": sum(
            item["reference_full_evaluations"] for item in random_traces
        ),
        "initialization_full_evaluations": sum(
            item["initialization_full_evaluations"]
            for item in random_traces
        ),
        "partition_full_evaluations": sum(
            item["partition_full_evaluations"] for item in random_traces
        ),
        "incremental_combinational_cell_evaluations": sum(
            item["incremental_combinational_cell_evaluations"]
            for item in random_traces
        ),
        "shadow_pin_updates": sum(
            item["shadow_pin_updates"] for item in random_traces
        ),
        "mismatches": 0,
        "trace_sha256": hashlib.sha256(
            json.dumps(
                [item["trace_sha256"] for item in random_traces],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "assumptions": [
            "one-commit macro-step semantics",
            "reset is deasserted during each checked macro-step",
            "transport shadows begin unavailable and must be produced in-frame",
        ],
    }
    if exhaustive is not None:
        evidence.update(
            {
                "qualification": (
                    "exhaustive-small-model-proof-plus-random-traces"
                ),
                "exhaustive_macro_step": exhaustive,
            }
        )
    else:
        evidence.update(
            {
                "qualification": (
                    "multi-seed-random-trace-validation-not-proof"
                ),
                "exhaustive_macro_step": exhaustive_skip,
            }
        )
    return evidence


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
    schedule = read_json(schedule_path)
    platform = Platform.load(platform_path)
    pin_plan = read_json(pin_plan_path) if pin_plan_path is not None else None
    position_hints = (
        read_json(position_hints_path)
        if position_hints_path is not None
        else None
    )
    pin_validation = None
    electrical_validation = None
    electrical_binding = None
    if pin_plan is not None:
        if position_hints is None:
            raise ValueError(
                "position_hints_path is required with pin_plan_path"
            )
        pin_validation = validate_pin_plan(
            schedule,
            platform,
            position_hints,
            pin_plan,
        )
        if pin_plan.get("provider") == CHIMEW_PIN_PLAN_PROVIDER:
            if electrical_binding_path is None:
                raise ValueError(
                    "electrical_binding_path is required with a Chimew pin plan"
                )
            from .chimew_phase6 import validate_chimew_phase6_binding

            electrical_binding = read_json(electrical_binding_path)
            electrical_validation = validate_chimew_phase6_binding(
                schedule,
                platform,
                pin_plan,
                electrical_binding,
            )
        elif electrical_binding_path is not None:
            raise ValueError(
                "electrical_binding_path is supported only with a Chimew pin plan"
            )
    elif position_hints is not None:
        raise ValueError(
            "pin_plan_path is required with position_hints_path"
        )
    elif electrical_binding_path is not None:
        raise ValueError("pin_plan_path is required with electrical_binding_path")
    artifacts = build_streaming_split_metadata(
        assignment_index_path,
        schedule,
        platform,
        design=source["design"],
        clocks=source["clocks"],
        pin_plan=pin_plan,
    )
    runtime = build_virtual_runtime(schedule, platform, clock_periods)
    artifacts["manifest"]["clock_periods"] = {
        clock: float(record["target_period_ns"])
        for clock, record in runtime["virtual_dut_clocks"].items()
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    validation = write_split_netlists_sqlite(
        output_dir / "split-netlists.sqlite",
        ir_path,
        assignment_index_path,
        schedule,
        expected_design=design_name,
        expected_platform=platform.name,
    )
    validation.update({
        "scheduled_hops": len(schedule["entries"]),
        "transport_endpoints": sum(
            len(value["endpoints"])
            for value in artifacts["transports"].values()
        ),
        "lane_entries": len(artifacts["lane_map"]["entries"]),
    })
    if validation["transport_endpoints"] != 2 * validation["scheduled_hops"]:
        raise ValidationError("transport endpoint coverage disagrees")
    if validation["lane_entries"] != validation["scheduled_hops"]:
        raise ValidationError("lane-map coverage disagrees")
    transport_cost = None
    if transport_cost_path is not None:
        transport_cost = read_json(transport_cost_path.resolve())
        validate_transport_cost_database(
            transport_cost, expected_platform=platform.name
        )
        validation["transport_cost_prediction"] = predict_transport_resources(
            transport_cost, artifacts["transports"]
        )
        artifacts["manifest"]["transport_cost"] = "transport-cost.json"
    equivalence = {
        "status": "pass",
        "provider": "constructive-sqlite-split-certificate-v1",
        "qualification": "lossless-source-record-and-cut-boundary-proof",
        "mismatches": 0,
        "cycles": 0,
        "compared_state_bits": 0,
        "compared_output_bits": 0,
        "trace_sha256": hashlib.sha256(
            json.dumps(
                {
                    "provider": validation["provider"],
                    "instances": validation["instances"],
                    "net_segments": validation["net_segments"],
                    "endpoints": validation["endpoints"],
                    "schedule_entries": len(schedule["entries"]),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "source_instances": validation["instances"],
        "net_segments": validation["net_segments"],
        "endpoints": validation["endpoints"],
        "synthetic_shadow_drivers": validation[
            "synthetic_shadow_drivers"
        ],
        **(
            {
                "semantic_contract_sha256": schedule[
                    "semantic_contract_sha256"
                ]
            }
            if "semantic_contract_sha256" in schedule
            else {}
        ),
        "scope": (
            "constructive Phase 6 transformation proof; exhaustive or trace "
            "behavioral replay remains an explicit small-design qualification"
        ),
    }
    record_counts = source.get("record_counts", {})
    small_qualification = (
        int(record_counts.get("cell_records", 0)) <= 10_000
        and int(record_counts.get("endpoint_records", 0)) <= 100_000
    )
    if small_qualification:
        from .tdm import is_sampled_virtual_wire_schedule

        ir = EmuIR.load(ir_path)
        assignment_json_path = (
            assignment_path.with_name("assignment.json")
            if assignment_path.suffix == ".sqlite"
            else assignment_path
        )
        assignment = read_json(assignment_json_path)
        if is_sampled_virtual_wire_schedule(schedule):
            equivalence = _static_exact_equivalence_evidence(
                ir,
                assignment,
                schedule,
                cycles=equivalence_cycles,
                seed=equivalence_seed,
                clock_periods=artifacts["manifest"]["clock_periods"],
            )
        else:
            equivalence = simulate_partition_equivalence(
                ir,
                assignment,
                schedule,
                cycles=equivalence_cycles,
                seed=equivalence_seed,
            )
    if electrical_binding is not None:
        artifacts["manifest"]["electrical_binding"] = "electrical_binding.json"
    write_json(
        output_dir / "manifest.json",
        (
            pack_managed_json(artifacts["manifest"])
            if managed_storage
            else artifacts["manifest"]
        ),
        compact=managed_storage,
    )
    write_json(
        output_dir / "lane_map.json",
        (
            pack_managed_json(artifacts["lane_map"])
            if managed_storage
            else artifacts["lane_map"]
        ),
        compact=managed_storage,
    )
    if pin_plan is not None:
        write_json(output_dir / "pin_plan.json", pin_plan)
        write_json(output_dir / "position_hints.json", position_hints)
        if electrical_binding_path is not None:
            write_json(
                output_dir / "electrical_binding.json",
                electrical_binding,
            )
    if transport_cost is not None:
        write_json(output_dir / "transport-cost.json", transport_cost, compact=True)
    (output_dir / "virtual_runtime_controller.sv").write_text(
        virtual_runtime_controller_to_systemverilog(runtime),
        encoding="utf-8",
    )
    for item in artifacts["manifest"]["fpgas"]:
        fpga_id = item["fpga"]
        fpga_root = output_dir / fpga_id
        write_json(
            fpga_root / "transport.json",
            (
                pack_managed_json(artifacts["transports"][fpga_id])
                if managed_storage
                else artifacts["transports"][fpga_id]
            ),
            compact=managed_storage,
        )
        write_json(
            fpga_root / "virtual_anchors.json",
            (
                pack_managed_json(artifacts["anchors"][fpga_id])
                if managed_storage
                else artifacts["anchors"][fpga_id]
            ),
            compact=managed_storage,
        )
        (fpga_root / "transport_schedule.sv").write_text(
            transport_to_systemverilog(
                artifacts["transports"][fpga_id], platform
            ),
            encoding="utf-8",
        )
        (fpga_root / "virtual_anchors.xdc.template").write_text(
            anchors_to_xdc_template(artifacts["anchors"][fpga_id]),
            encoding="utf-8",
        )
    report = {
        "schema": PHASE6_REPORT_SCHEMA,
        "phase": 6,
        "increment": "board-independent-netlist-and-lane-planning",
        "status": "pass",
        "design": design_name,
        "platform": platform.name,
        "provider": artifacts["manifest"]["provider"],
        "virtual_runtime_provider": runtime["provider"],
        "virtual_dut_clocks": sorted(runtime["virtual_dut_clocks"]),
        "clock_contract_ref": "manifest.json#/clock_periods",
        "validation": validation,
        "equivalence": equivalence,
        "board_binding": artifacts["manifest"]["board_binding"],
        **(
            {"pin_plan_validation": pin_validation}
            if pin_validation is not None
            else {}
        ),
        **(
            {"electrical_binding_validation": electrical_validation}
            if electrical_validation is not None
            else {}
        ),
        "artifacts": {
            "manifest": "manifest.json",
            "lane_map": "lane_map.json",
            "split_netlists": "split-netlists.sqlite",
            "runtime_controller_rtl": "virtual_runtime_controller.sv",
            "report": "phase6_report.json",
            **(
                {"pin_plan": "pin_plan.json"}
                if pin_plan is not None
                else {}
            ),
            **(
                {"position_hints": "position_hints.json"}
                if pin_plan is not None
                else {}
            ),
            **(
                {"electrical_binding": "electrical_binding.json"}
                if electrical_validation is not None
                else {}
            ),
            **(
                {"transport_cost": "transport-cost.json"}
                if transport_cost is not None
                else {}
            ),
        },
    }
    write_json(output_dir / "phase6_report.json", report)
    return report


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
        read_json(schedule_path)
        if schedule_value is None
        else schedule_value
    )
    platform = Platform.load(platform_path)
    manifest = read_json(manifest_path)
    transports = {
        item["fpga"]: read_json(manifest_path.parent / item["transport"])
        for item in manifest["fpgas"]
    }
    pin_plan = (
        read_json(pin_plan_path)
        if pin_plan_path is not None
        else (
            read_json(manifest_path.parent / manifest["pin_plan"])
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
            read_json(resolved_positions),
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
            from .chimew_phase6 import validate_chimew_phase6_binding

            validate_chimew_phase6_binding(
                schedule, platform, pin_plan, read_json(resolved_binding)
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
            read_json(manifest_path.parent / manifest["lane_map"])["entries"]
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
    from .tdm import is_sampled_virtual_wire_schedule

    if replay_equivalence and is_sampled_virtual_wire_schedule(schedule):
        assignment_json_path = (
            assignment_path.with_name("assignment.json")
            if assignment_path.suffix == ".sqlite"
            else assignment_path
        )
        ir = EmuIR.load(ir_path)
        assignment = read_json(assignment_json_path)
        validation["static_exact_equivalence"] = (
            _static_exact_equivalence_evidence(
                ir,
                assignment,
                schedule,
                cycles=16,
                seed=20260727,
                clock_periods=clock_periods,
            )
        )
    return validation
