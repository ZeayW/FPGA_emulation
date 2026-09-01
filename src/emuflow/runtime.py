from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional, Sequence

from .errors import ValidationError
from .platform import Platform
from .system_timing import build_system_timing
from .tdm import RUNTIME_BARRIER_SLOTS, TDM_SCHEDULE_SCHEMA


VIRTUAL_RUNTIME_SCHEMA = "emuflow.virtual-runtime/v1"
PHYSICAL_SUMMARY_SCHEMA = "emuflow.phase7b-physical-summary/v1"
QOR_REPORT_SCHEMA = "emuflow.qor-report/v4"


def virtual_runtime_controller_to_systemverilog() -> str:
    return """module emuflow_virtual_runtime_controller #(
  parameter integer FRAME_SLOTS = 32,
  parameter integer SLOT_BITS = 5
) (
  input  logic fabric_clk,
  input  logic reset,
  input  logic links_ready,
  output logic virtual_clock_enable,
  output logic [SLOT_BITS-1:0] slot
);
  logic started;

  always_comb begin
    virtual_clock_enable =
      !reset && started && links_ready && (slot == FRAME_SLOTS - 1);
  end

  always_ff @(posedge fabric_clk) begin
    if (reset) begin
      slot <= '0;
      started <= 1'b0;
    end else if (!started) begin
      // No TDM work may execute before the distributed startup barrier.
      // The first complete frame begins only after global readiness.
      slot <= '0;
      if (links_ready) begin
        started <= 1'b1;
      end
    end else begin
      if (slot == FRAME_SLOTS - 1) begin
        if (links_ready) begin
          slot <= '0;
        end
      end else begin
        slot <= slot + 1'b1;
      end
    end
  end
endmodule
"""


def _common_fabric_clock_mhz(platform: Platform) -> float:
    if not platform.links:
        raise ValidationError(
            "virtual runtime requires at least one BoardDB link"
        )
    frequencies = [link.fabric_clock_mhz for link in platform.links]
    reference = frequencies[0]
    if any(
        not math.isclose(value, reference, rel_tol=0.0, abs_tol=1e-9)
        for value in frequencies[1:]
    ):
        raise ValidationError(
            "virtual runtime v1 requires one common fabric clock frequency"
        )
    return reference


def build_virtual_runtime(
    schedule: Mapping[str, Any],
    platform: Platform,
) -> Dict[str, Any]:
    if schedule.get("schema") != TDM_SCHEDULE_SCHEMA:
        raise ValidationError(
            f"schedule.schema: expected {TDM_SCHEDULE_SCHEMA!r}"
        )
    if schedule.get("platform") != platform.name:
        raise ValidationError("schedule.platform does not match BoardDB")
    metrics = schedule.get("metrics")
    if not isinstance(metrics, dict):
        raise ValidationError("schedule.metrics: expected an object")
    frame_slots = metrics.get("frame_slots")
    completion_slot = metrics.get("completion_slot")
    if (
        isinstance(frame_slots, bool)
        or not isinstance(frame_slots, int)
        or frame_slots < 2
    ):
        raise ValidationError(
            "runtime frame_slots must be an integer of at least two"
        )
    if (
        isinstance(completion_slot, bool)
        or not isinstance(completion_slot, int)
        or completion_slot < 0
        or completion_slot >= frame_slots - RUNTIME_BARRIER_SLOTS
    ):
        raise ValidationError(
            "schedule must complete before the runtime barrier release slot"
        )
    expected_completion = max(
        (
            entry.get("arrival_slot", -1)
            for entry in schedule.get("entries", [])
        ),
        default=0,
    )
    if completion_slot != expected_completion:
        raise ValidationError(
            "schedule completion_slot does not match scheduled arrivals"
        )

    fabric_clock_mhz = _common_fabric_clock_mhz(platform)
    fabric_period_ns = 1000.0 / fabric_clock_mhz
    release_slot = frame_slots - RUNTIME_BARRIER_SLOTS
    shadow_settle_slots = release_slot - completion_slot
    nominal_virtual_period_ns = frame_slots * fabric_period_ns
    return {
        "schema": VIRTUAL_RUNTIME_SCHEMA,
        "design": schedule.get("design"),
        "platform": platform.name,
        "provider": "lockstep-pausible-clock-barrier-v1",
        "semantic_envelope": {
            "virtual_dut_clocks": 1,
            "reset_model": "synchronous-fabric-controller",
            "clock_progression": "one-dut-edge-after-each-complete-frame",
        },
        "fabric_clock": {
            "frequency_mhz": fabric_clock_mhz,
            "period_ns": fabric_period_ns,
            "distribution": "common-frequency-lockstep",
        },
        "frame": {
            "slots": frame_slots,
            "completion_slot": completion_slot,
            "barrier_release_slot": release_slot,
            "shadow_settle_slots": shadow_settle_slots,
            "shadow_settle_ns": shadow_settle_slots * fabric_period_ns,
        },
        "virtual_dut_clock": {
            "nominal_frequency_mhz": 1000.0 / nominal_virtual_period_ns,
            "nominal_period_ns": nominal_virtual_period_ns,
            "enable_pulse_fabric_cycles": 1,
            "enable_assertion_phase": (
                "during-release-slot-before-dut-edge"
            ),
            "may_stall": True,
            "binding": "dedicated-clock-buffer-enable",
            "unsafe_binding": "fabric-routed-combinational-clock-gate",
        },
        "barrier": {
            "ready_signal": "links_ready",
            "ready_scope": "global-consensus-across-all-fpgas",
            "stall_slot": release_slot,
            "stall_behavior": "hold-slot-and-suppress-dut-clock-enable",
            "startup_behavior": "hold-slot-zero-until-global-ready",
            "lockstep_requirements": [
                "common fabric-clock frequency",
                "phase-aligned fabric clocks or trained slot alignment",
                "synchronous controller reset release",
                "same synchronized global links_ready value",
            ],
        },
        "timing_model": {
            "dut_clock_port": "clk",
            "fabric_clock_port": "fabric_clk",
            "fabric_to_dut_max_delay_ns": (
                shadow_settle_slots * fabric_period_ns
            ),
            "physical_link_io_delay_status": "requires-hardware-bsp",
            "cdc_protocol": "pausible-clock-stable-data-window",
        },
        "board_binding": {
            "status": "virtual",
            "requires_package_pins": True,
            "requires_clock_buffer_binding": True,
            "requires_link_training": True,
        },
    }


def validate_virtual_runtime(
    runtime: Mapping[str, Any],
    schedule: Mapping[str, Any],
    platform: Platform,
) -> Dict[str, Any]:
    expected = build_virtual_runtime(schedule, platform)
    if runtime != expected:
        raise ValidationError(
            "virtual runtime contract does not match schedule and BoardDB"
        )
    frame = expected["frame"]
    return {
        "status": "pass",
        "fpgas": len(platform.fpgas),
        "frame_slots": frame["slots"],
        "completion_slot": frame["completion_slot"],
        "barrier_release_slot": frame["barrier_release_slot"],
        "shadow_settle_slots": frame["shadow_settle_slots"],
        "shadow_settle_ns": frame["shadow_settle_ns"],
        "nominal_virtual_frequency_mhz": expected["virtual_dut_clock"][
            "nominal_frequency_mhz"
        ],
    }


def estimate_runtime_timing(
    runtime: Mapping[str, Any],
    phase5_report: Mapping[str, Any],
) -> Dict[str, Any]:
    """Compare concrete scheduled path delay with the virtual clock period."""
    timing = phase5_report.get("timing_validation")
    if not isinstance(timing, dict):
        return {
            "status": "unavailable",
            "qualification": "no-scheduled-path-timing",
        }
    if timing.get("status") != "pass":
        raise ValidationError("Phase 5 scheduled timing did not pass")
    worst_delay = timing.get("worst_delay_ns")
    original_slack = timing.get("worst_slack_ns")
    virtual_period = runtime["virtual_dut_clock"]["nominal_period_ns"]
    for name, value in (
        ("worst_delay_ns", worst_delay),
        ("worst_slack_ns", original_slack),
        ("nominal_period_ns", virtual_period),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValidationError(f"runtime timing {name} must be finite")
    runtime_slack = float(virtual_period) - float(worst_delay)
    return {
        "status": "pass" if runtime_slack >= 0.0 else "fail",
        "qualification": (
            "academic-preplacement-fixed-delay-plus-concrete-tdm-schedule"
        ),
        "original_clock_reference": {
            "worst_path": timing.get("worst_path"),
            "worst_delay_ns": float(worst_delay),
            "worst_slack_ns": float(original_slack),
            "negative_slack_paths": timing.get("negative_slack_paths"),
            "closure_gate": False,
        },
        "virtual_clock": {
            "period_ns": float(virtual_period),
            "frequency_mhz": runtime["virtual_dut_clock"][
                "nominal_frequency_mhz"
            ],
            "estimated_worst_slack_ns": runtime_slack,
            "estimated_closed": runtime_slack >= 0.0,
        },
    }


def runtime_timing_xdc(runtime: Mapping[str, Any]) -> str:
    fabric = runtime["fabric_clock"]
    dut = runtime["virtual_dut_clock"]
    timing = runtime["timing_model"]
    return "\n".join(
        [
            "# EmuFlow board-independent pausible-clock timing contract.",
            "# The DUT clock is constrained separately by the implementation",
            "# harness. This XDC adds the fabric clock and the protocol-backed",
            "# fabric-shadow to virtual-DUT stable-data window.",
            f"create_clock -name emuflow_fabric_clk "
            f"-period {fabric['period_ns']:.9f} "
            f"[get_ports {{{timing['fabric_clock_port']}}}]",
            f"set_max_delay -datapath_only "
            f"{timing['fabric_to_dut_max_delay_ns']:.9f} "
            "-from [get_clocks {emuflow_fabric_clk}] "
            "-to [get_clocks {emuflow_dut_clk}]",
            "",
            "# Board-specific input/output delays, package pins, IOSTANDARD,",
            "# source-synchronous forwarding, and link training are intentionally",
            "# absent until a hardware BSP is selected.",
            "# physical_link_io_delay_status=requires-hardware-bsp",
            f"# nominal_virtual_dut_period_ns={dut['nominal_period_ns']:.9f}",
            "",
        ]
    )


def runtime_controller_testbench(
    runtime: Mapping[str, Any],
    fpga_ids: Sequence[str],
    frames: int = 12,
) -> str:
    if frames < 3:
        raise ValueError("runtime controller testbench requires at least 3 frames")
    frame_slots = runtime["frame"]["slots"]
    slot_bits = max(1, (frame_slots - 1).bit_length())
    lines = [
        "`timescale 1ns/1ps",
        "module virtual_runtime_controller_tb;",
        f"  localparam integer FRAME_SLOTS = {frame_slots};",
        f"  localparam integer SLOT_BITS = {slot_bits};",
        f"  localparam integer TARGET_FRAMES = {frames};",
        "  logic fabric_clk = 1'b0;",
        "  logic reset = 1'b1;",
        "  logic links_ready = 1'b0;",
    ]
    for index, fpga_id in enumerate(fpga_ids):
        lines.extend(
            [
                f"  logic virtual_clock_enable_{index};",
                f"  logic [SLOT_BITS-1:0] slot_{index};",
                "  emuflow_virtual_runtime_controller #(",
                "    .FRAME_SLOTS(FRAME_SLOTS), .SLOT_BITS(SLOT_BITS)",
                f"  ) dut_{index} (",
                "    .fabric_clk(fabric_clk), .reset(reset),",
                "    .links_ready(links_ready),",
                f"    .virtual_clock_enable(virtual_clock_enable_{index}),",
                f"    .slot(slot_{index})",
                "  );",
                f"  // FPGA instance {fpga_id}",
            ]
        )
    lines.extend(
        [
            "  always #2 fabric_clk = ~fabric_clk;",
            "  integer pulses = 0;",
            "  integer stalled_cycles = 0;",
            "  integer previous_slot;",
            "  initial begin",
            "    repeat (3) @(posedge fabric_clk);",
            "    #1 reset = 1'b0;",
            "    repeat (3) begin",
            "      @(posedge fabric_clk); #1;",
            "      if (slot_0 !== 0 || virtual_clock_enable_0)",
            '        $fatal(1, "controller advanced before global ready");',
            "    end",
            "    @(negedge fabric_clk); links_ready = 1'b1;",
            "    @(posedge fabric_clk); #1;",
            "    if (slot_0 !== 0 || virtual_clock_enable_0)",
            '      $fatal(1, "startup barrier did not begin at slot zero");',
            "    while (pulses < TARGET_FRAMES) begin",
            "      @(negedge fabric_clk);",
            "      previous_slot = slot_0;",
            "      if (pulses == 2 && previous_slot == FRAME_SLOTS - 1 &&",
            "          stalled_cycles < 3) begin",
            "        links_ready = 1'b0;",
            "      end else begin",
            "        links_ready = 1'b1;",
            "      end",
            "      #1;",
            "      if (previous_slot == FRAME_SLOTS - 1 && links_ready) begin",
            "        if (!virtual_clock_enable_0)",
            '          $fatal(1, "clock enable was not asserted before release");',
            "      end else if (virtual_clock_enable_0) begin",
            '        $fatal(1, "clock enable asserted outside release window");',
            "      end",
            "      @(posedge fabric_clk); #1;",
        ]
    )
    for index in range(1, len(fpga_ids)):
        lines.extend(
            [
                f"      if (slot_{index} !== slot_0 ||",
                f"          virtual_clock_enable_{index} !== "
                "virtual_clock_enable_0)",
                f'        $fatal(1, "controller {index} lost lockstep");',
            ]
        )
    lines.extend(
        [
            "      if (!links_ready && previous_slot == FRAME_SLOTS - 1) begin",
            "        stalled_cycles = stalled_cycles + 1;",
            "        if (slot_0 !== FRAME_SLOTS - 1 || virtual_clock_enable_0)",
            '          $fatal(1, "barrier did not hold at release slot");',
            "      end else if (previous_slot == FRAME_SLOTS - 1) begin",
            "        if (slot_0 !== 0 || virtual_clock_enable_0)",
            '          $fatal(1, "illegal state after frame release");',
            "        pulses = pulses + 1;",
            "      end else begin",
            "        if (slot_0 !== previous_slot + 1)",
            '          $fatal(1, "illegal slot progression");',
            "      end",
            "      if (virtual_clock_enable_0 !==",
            "          (!reset && links_ready && slot_0 == FRAME_SLOTS - 1))",
            '        $fatal(1, "clock enable does not match release window");',
            "    end",
            "    if (stalled_cycles != 3)",
            '      $fatal(1, "stall coverage mismatch");',
            '    $display("EMUFLOW_RUNTIME_TB status=pass frames=%0d '
            'stalled_cycles=%0d controllers=%0d", pulses, stalled_cycles, '
            f"{len(fpga_ids)});",
            "    $finish;",
            "  end",
            "endmodule",
            "",
        ]
    )
    return "\n".join(lines)


def validate_physical_summary(
    summary: Mapping[str, Any],
    runtime: Mapping[str, Any],
    platform: Platform,
) -> Dict[str, Any]:
    if summary.get("schema") != PHYSICAL_SUMMARY_SCHEMA:
        raise ValidationError(
            f"physical summary schema must be {PHYSICAL_SUMMARY_SCHEMA!r}"
        )
    if summary.get("status") != "pass":
        raise ValidationError("physical summary did not pass")
    if summary.get("design") != runtime.get("design"):
        raise ValidationError("physical summary design does not match runtime")
    if summary.get("platform") != platform.name:
        raise ValidationError("physical summary platform does not match BoardDB")
    raw_fpgas = summary.get("fpgas")
    if not isinstance(raw_fpgas, list):
        raise ValidationError("physical summary fpgas must be an array")
    by_id = {
        item.get("fpga"): item for item in raw_fpgas if isinstance(item, dict)
    }
    expected_ids = {fpga.id for fpga in platform.fpgas}
    if set(by_id) != expected_ids:
        raise ValidationError(
            "physical summary must contain exactly one record per FPGA"
        )
    expected_fabric = runtime["fabric_clock"]["period_ns"]
    expected_dut = runtime["virtual_dut_clock"]["nominal_period_ns"]
    total_cells = 0
    total_physical_cells = 0
    total_infrastructure_cells = 0
    total_optimization_cells = 0
    total_original = 0
    total_transport = 0
    worst_slack = None
    worst_dut_slack = None
    worst_fabric_slack = None
    worst_cross_slack = None
    total_tns = 0.0
    total_failing_endpoints = 0
    total_failing_endpoint_constraints = 0
    for fpga_id in sorted(by_id):
        item = by_id[fpga_id]
        for field in (
            "original_cells",
            "transport_cells",
            "routed_cells",
            "physical_cells",
            "infrastructure_cells",
            "unrouted_nets",
            "drc_violations",
        ):
            value = item.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValidationError(
                    f"physical summary {fpga_id}.{field} must be non-negative"
                )
        if item["routed_cells"] != (
            item["original_cells"] + item["transport_cells"]
        ):
            raise ValidationError(
                f"physical summary {fpga_id} cell accounting is inconsistent"
            )
        optimization_cells = item.get("optimization_cells", 0)
        if (
            isinstance(optimization_cells, bool)
            or not isinstance(optimization_cells, int)
            or optimization_cells < 0
        ):
            raise ValidationError(
                f"physical summary {fpga_id}.optimization_cells must be "
                "non-negative"
            )
        if item["physical_cells"] != (
            item["routed_cells"]
            + item["infrastructure_cells"]
            + optimization_cells
        ):
            raise ValidationError(
                f"physical summary {fpga_id} physical cell accounting "
                "is inconsistent"
            )
        if item["unrouted_nets"] or item["drc_violations"]:
            raise ValidationError(
                f"physical summary {fpga_id} did not close route/DRC"
            )
        clocks = item.get("clocks")
        if not isinstance(clocks, dict):
            raise ValidationError(
                f"physical summary {fpga_id}.clocks must be an object"
            )
        if not math.isclose(
            float(clocks.get("fabric_period_ns", -1)),
            expected_fabric,
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            raise ValidationError(
                f"physical summary {fpga_id} fabric period mismatch"
            )
        physical_dut_period = float(clocks.get("dut_period_ns", -1))
        # A backend may intentionally close DUT logic at a faster clock than
        # the virtual frame rate. That is conservative: the runtime still
        # releases exactly one DUT edge per complete frame. Reject only a
        # physical constraint slower than the nominal runtime period.
        if (
            physical_dut_period <= 0.0
            or physical_dut_period - expected_dut > 1e-6
        ):
            raise ValidationError(
                f"physical summary {fpga_id} DUT period is slower than "
                "the nominal runtime"
            )
        slack = item.get("wns_ns")
        if isinstance(slack, bool) or not isinstance(slack, (int, float)):
            raise ValidationError(
                f"physical summary {fpga_id}.wns_ns must be numeric"
            )
        timing = item.get("timing")
        if not isinstance(timing, dict):
            raise ValidationError(
                f"physical summary {fpga_id}.timing must be an object"
            )
        timing_met = timing.get("timing_met")
        if timing_met is not None and timing_met is not (float(slack) >= 0):
            raise ValidationError(
                f"physical summary {fpga_id}.timing.timing_met disagrees"
            )
        if "tns_ns" in timing:
            tns = timing.get("tns_ns")
            failing_endpoints = timing.get("failing_endpoints")
            failing_constraints = timing.get("failing_endpoint_constraints")
            if (
                isinstance(tns, bool)
                or not isinstance(tns, (int, float))
                or not math.isfinite(float(tns))
                or float(tns) > 0
            ):
                raise ValidationError(
                    f"physical summary {fpga_id}.timing.tns_ns is invalid"
                )
            for field, value in (
                ("failing_endpoints", failing_endpoints),
                ("failing_endpoint_constraints", failing_constraints),
            ):
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValidationError(
                        f"physical summary {fpga_id}.timing.{field} is invalid"
                    )
            total_tns += float(tns)
            total_failing_endpoints += failing_endpoints
            total_failing_endpoint_constraints += failing_constraints
        timing_values = {}
        for field in (
            "dut_wns_ns",
            "fabric_wns_ns",
            "fabric_to_dut_wns_ns",
        ):
            value = timing.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValidationError(
                    f"physical summary {fpga_id}.timing.{field} "
                    "must be finite"
                )
            timing_values[field] = float(value)
        presence = item.get("clock_domain_presence")
        if presence is None:
            dut_present = True
        elif (
            not isinstance(presence, dict)
            or set(presence) != {"fabric", "dut", "cross"}
            or any(not isinstance(value, bool) for value in presence.values())
            or not presence["fabric"]
            or presence["cross"] != presence["dut"]
        ):
            raise ValidationError(
                f"physical summary {fpga_id}.clock_domain_presence is invalid"
            )
        else:
            dut_present = presence["dut"]
        total_cells += item["routed_cells"]
        total_physical_cells += item["physical_cells"]
        total_infrastructure_cells += item["infrastructure_cells"]
        total_optimization_cells += optimization_cells
        total_original += item["original_cells"]
        total_transport += item["transport_cells"]
        worst_slack = float(slack) if worst_slack is None else min(
            worst_slack, float(slack)
        )
        if dut_present:
            worst_dut_slack = (
                timing_values["dut_wns_ns"]
                if worst_dut_slack is None
                else min(worst_dut_slack, timing_values["dut_wns_ns"])
            )
        worst_fabric_slack = (
            timing_values["fabric_wns_ns"]
            if worst_fabric_slack is None
            else min(worst_fabric_slack, timing_values["fabric_wns_ns"])
        )
        if dut_present:
            worst_cross_slack = (
                timing_values["fabric_to_dut_wns_ns"]
                if worst_cross_slack is None
                else min(
                    worst_cross_slack,
                    timing_values["fabric_to_dut_wns_ns"],
                )
            )
    return {
        "status": "pass",
        "scope": "per-fpga-local-physical-closure",
        "fpgas": len(by_id),
        "original_cells": total_original,
        "transport_cells": total_transport,
        "routed_cells": total_cells,
        "physical_cells": total_physical_cells,
        "infrastructure_cells": total_infrastructure_cells,
        "optimization_cells": total_optimization_cells,
        "unrouted_nets": 0,
        "drc_violations": 0,
        "worst_wns_ns": worst_slack,
        "worst_local_backend_wns_ns": worst_slack,
        "worst_dut_wns_ns": worst_dut_slack,
        "worst_fabric_wns_ns": worst_fabric_slack,
        "worst_fabric_to_dut_wns_ns": worst_cross_slack,
        "total_tns_ns": total_tns,
        "failing_endpoints": total_failing_endpoints,
        "failing_endpoint_constraints": total_failing_endpoint_constraints,
        "timing_met": worst_slack >= 0,
    }


def aggregate_qor(
    runtime: Mapping[str, Any],
    phase3_report: Mapping[str, Any],
    phase4_report: Mapping[str, Any],
    phase5_report: Mapping[str, Any],
    phase6_report: Mapping[str, Any],
    physical_summary: Optional[Mapping[str, Any]],
    platform: Platform,
    routes: Optional[Mapping[str, Any]] = None,
    schedule: Optional[Mapping[str, Any]] = None,
    routes_artifact_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    for name, report in (
        ("phase3", phase3_report),
        ("phase4", phase4_report),
        ("phase5", phase5_report),
        ("phase6", phase6_report),
    ):
        if report.get("status") != "pass":
            raise ValidationError(f"{name} report did not pass")
        if report.get("design") != runtime.get("design"):
            raise ValidationError(f"{name} design does not match runtime")
        if report.get("platform") != platform.name:
            raise ValidationError(f"{name} platform does not match BoardDB")
    partition = phase3_report["validation"]
    routing = phase4_report["validation"]
    scheduling = phase5_report["validation"]
    equivalence = phase6_report["equivalence"]
    physical = (
        {"status": "pending"}
        if physical_summary is None
        else validate_physical_summary(physical_summary, runtime, platform)
    )
    if physical_summary is None:
        runtime_timing = estimate_runtime_timing(runtime, phase5_report)
    else:
        if routes is None or schedule is None:
            raise ValidationError(
                "physical Phase 7C closure requires routes and schedule for "
                "unified system timing"
            )
        runtime_timing = build_system_timing(
            runtime,
            routes,
            schedule,
            phase5_report,
            physical_summary,
            platform,
            routes_artifact_sha256=routes_artifact_sha256,
        )
    physical_closed = (
        physical["status"] == "pass"
        and runtime_timing.get("status") == "pass"
    )
    whole_design_timing_complete = (
        physical_summary is None
        or runtime_timing.get("timing_scope") == "whole-original-design"
    )
    closed = physical_closed and whole_design_timing_complete
    return {
        "schema": QOR_REPORT_SCHEMA,
        "status": (
            "pass"
            if closed
            else "incomplete"
            if (
                (physical_closed and not whole_design_timing_complete)
                or runtime_timing.get("status") == "incomplete"
            )
            else "pending"
            if physical["status"] == "pending"
            else "fail"
        ),
        "design": runtime["design"],
        "platform": platform.name,
        "whole_design_timing_complete": whole_design_timing_complete,
        "partition": {
            "instances": partition["instances"],
            "used_fpgas": partition["used_fpgas"],
            "cut_nets": partition["cut_nets"],
            "cut_sink_endpoints": partition["cut_sink_endpoints"],
        },
        "system_routing": {
            "demands": routing["demands"],
            "routed_sinks": routing["routed_sinks"],
            "bit_hops": routing["total_link_bit_hops"],
            "max_link_utilization": routing["max_link_utilization"],
        },
        "tdm": {
            "scheduled_bit_hops": scheduling["scheduled_bit_hops"],
            "frame_slots": scheduling["frame_slots"],
            "completion_slot": scheduling["completion_slot"],
            "max_domain_utilization": scheduling[
                "max_domain_utilization"
            ],
            "collisions": scheduling["collisions"],
        },
        "runtime": {
            "fabric_frequency_mhz": runtime["fabric_clock"][
                "frequency_mhz"
            ],
            "nominal_virtual_frequency_mhz": runtime["virtual_dut_clock"][
                "nominal_frequency_mhz"
            ],
            "nominal_virtual_period_ns": runtime["virtual_dut_clock"][
                "nominal_period_ns"
            ],
            "shadow_settle_ns": runtime["frame"]["shadow_settle_ns"],
            "barrier_release_slot": runtime["frame"][
                "barrier_release_slot"
            ],
        },
        "timing": runtime_timing,
        "equivalence": {
            "cycles": equivalence["cycles"],
            "compared_state_bits": equivalence["compared_state_bits"],
            "compared_output_bits": equivalence["compared_output_bits"],
            "mismatches": equivalence["mismatches"],
            "trace_sha256": equivalence["trace_sha256"],
        },
        "physical": physical,
        "board_binding": runtime["board_binding"],
    }
