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
    build_split_artifacts,
    transport_to_systemverilog,
    validate_split_artifacts,
)
from .platform import Platform
from .pin_planning import CHIMEW_PIN_PLAN_PROVIDER, validate_pin_plan
from .runtime import virtual_runtime_controller_to_systemverilog


PHASE6_REPORT_SCHEMA = "emuflow.phase6-report/v1"
STATIC_EXACT_SCHEDULE_PROVIDER = "deterministic-static-exact-list-schedule-v1"


def _static_exact_equivalence_evidence(
    ir: EmuIR,
    assignment: Mapping[str, Any],
    schedule: Mapping[str, Any],
    *,
    cycles: int,
    seed: int,
) -> Dict[str, Any]:
    random_traces = [
        simulate_static_exact_partition_equivalence(
            ir,
            assignment,
            schedule,
            cycles=cycles,
            seed=seed + offset,
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


def _load_artifacts(root: Path, manifest: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "manifest": dict(manifest),
        "lane_map": read_json(root / manifest["lane_map"]),
        "netlists": {
            item["fpga"]: read_json(root / item["netlist"])
            for item in manifest["fpgas"]
        },
        "transports": {
            item["fpga"]: read_json(root / item["transport"])
            for item in manifest["fpgas"]
        },
        "anchors": {
            item["fpga"]: read_json(root / item["virtual_anchors"])
            for item in manifest["fpgas"]
        },
    }


def run_phase6(
    ir_path: Path,
    assignment_path: Path,
    schedule_path: Path,
    platform_path: Path,
    output_dir: Path,
    equivalence_cycles: int = 16,
    equivalence_seed: int = 20260727,
    pin_plan_path: Optional[Path] = None,
    position_hints_path: Optional[Path] = None,
    electrical_binding_path: Optional[Path] = None,
    managed_storage: bool = False,
) -> Dict[str, Any]:
    ir = EmuIR.load(ir_path)
    assignment = read_json(assignment_path)
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
    artifacts = build_split_artifacts(
        ir, assignment, schedule, platform, pin_plan
    )
    validation = validate_split_artifacts(
        ir,
        assignment,
        schedule,
        platform,
        artifacts,
        pin_plan,
        reconstruct=False,
    )
    if schedule.get("provider") == STATIC_EXACT_SCHEDULE_PROVIDER:
        equivalence = _static_exact_equivalence_evidence(
            ir,
            assignment,
            schedule,
            cycles=equivalence_cycles,
            seed=equivalence_seed,
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
    output_dir.mkdir(parents=True, exist_ok=True)
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
    (output_dir / "virtual_runtime_controller.sv").write_text(
        virtual_runtime_controller_to_systemverilog(),
        encoding="utf-8",
    )
    for item in artifacts["manifest"]["fpgas"]:
        fpga_id = item["fpga"]
        fpga_root = output_dir / fpga_id
        write_json(
            fpga_root / "netlist.json",
            (
                pack_managed_json(artifacts["netlists"][fpga_id])
                if managed_storage
                else artifacts["netlists"][fpga_id]
            ),
            compact=managed_storage,
        )
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
        "design": ir.value["design"]["name"],
        "platform": platform.name,
        "provider": artifacts["manifest"]["provider"],
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
    replay_equivalence: bool = True,
    reconstruct_artifacts: bool = True,
) -> Dict[str, Any]:
    ir = EmuIR.load(ir_path)
    assignment = read_json(assignment_path)
    schedule = read_json(schedule_path)
    platform = Platform.load(platform_path)
    manifest = read_json(manifest_path)
    artifacts = _load_artifacts(manifest_path.parent, manifest)
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
    artifacts["manifest"].pop("electrical_binding", None)
    validation = validate_split_artifacts(
        ir,
        assignment,
        schedule,
        platform,
        artifacts,
        pin_plan,
        reconstruct=reconstruct_artifacts,
    )
    if (
        replay_equivalence
        and schedule.get("provider") == STATIC_EXACT_SCHEDULE_PROVIDER
    ):
        validation["static_exact_equivalence"] = (
            _static_exact_equivalence_evidence(
                ir,
                assignment,
                schedule,
                cycles=16,
                seed=20260727,
            )
        )
    return validation
