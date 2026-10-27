"""Small, in-memory deadline arithmetic oracle, excluded from production.

This code only evaluates fixture events and measured bounds. It has no storage
adapter, engine runner, publication path or large-design mode. Original-path
OpenSTA events remain the production timing authority.
"""
from __future__ import annotations

from collections import defaultdict
import math

from emuflow.partition.combinational_cut import semantic_contract_sha256
from emuflow.common.errors import ValidationError
from emuflow.scheduling.tdm import sampled_virtual_wire_timing_constraints

_LIMIT = 4096
_SCHEMA = "emuflow.static-exact-segment-deadlines/v2"


def _fixture_records(value):
    if not isinstance(value, list) or len(value) > _LIMIT:
        raise ValidationError("deadline oracle requires a small in-memory fixture")
    return value


def _nonnegative(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValidationError("deadline uncertainty must be finite and non-negative")
    return float(value)


def build_static_exact_segment_deadlines(schedule, contract, physical_summary, platform):
    """Evaluate arrival→TX/commit windows independently on at most 4096 events."""
    entries = _fixture_records(schedule.get("entries"))
    segments = _fixture_records(contract.get("logic_segments"))
    if (schedule.get("transport_semantics") != "sampled-virtual-wire" or
            contract.get("mode") != "static-exact-combinational"):
        raise ValidationError("deadline oracle requires exact combinational semantics")
    digest = semantic_contract_sha256(contract)
    if (schedule.get("semantic_contract_schema") != contract.get("schema") or
            schedule.get("semantic_contract_sha256") != digest):
        raise ValidationError("static exact schedule contract digest disagrees")
    timing = schedule.get("timing_constraints")
    frame = schedule.get("metrics", {}).get("frame_slots")
    if (type(frame) is not int or frame <= 1 or
            timing != sampled_virtual_wire_timing_constraints(frame)):
        raise ValidationError("static exact commit slot disagrees with schedule")
    uncertainty = _nonnegative(physical_summary.get("static_exact_clock_uncertainty_ns", 0.0))
    sources, arrivals, links = defaultdict(list), {}, set()
    for entry in entries:
        sources[entry["net"], entry["from"]].append(entry)
        key = entry["net"], entry["to"]
        if key in arrivals:
            raise ValidationError("static exact schedule has a duplicate current-frame arrival")
        arrivals[key] = entry
        links.add(entry["link"])
    periods = {round(1000.0/link.fabric_clock_mhz, 12) for link in platform.links if link.id in links}
    if len(periods) != 1 or links - {link.id for link in platform.links}:
        raise ValidationError("deadline oracle requires one common fabric slot period")
    slot = periods.pop()
    evidence, identities, measurements = defaultdict(list), set(), 0
    for fpga, database in sorted(physical_summary.get("logic_segment_timing", {}).items()):
        records = _fixture_records(database["segments"])
        measurements += len(records)
        if measurements > _LIMIT:
            raise ValidationError("deadline oracle requires a small in-memory fixture")
        if database.get("status") != "pass" or database.get("schema") != "emuflow.logic-segment-timing/v1":
            raise ValidationError("deadline oracle requires passing logic timing facts")
        if any(record.get("fpga") != fpga or _nonnegative(record["delay_ns"]) < 0 for record in records):
            raise ValidationError("deadline oracle physical facts disagree")
        if database["fpga"] != fpga:
            raise ValidationError("static exact logic segment FPGA identity disagrees")
        if database.get("semantic_contract_sha256") != digest:
            raise ValidationError("static exact logic timing is bound to another contract")
        for record in records:
            exact = record.get("static_exact_segment_id")
            if record["id"] in identities or not isinstance(exact, str) or not exact:
                raise ValidationError("duplicate or unresolved physical logic segment")
            identities.add(record["id"])
            evidence[exact].append(record)

    def tx(net, fpga):
        records = sources[net, fpga]
        if not records:
            raise ValidationError("static exact cut has no TX from source")
        return min(row["slot"] for row in records), ",".join(sorted(row["id"] for row in records))

    result, missing, failed = [], [], []
    for segment in sorted(segments, key=lambda row: row["id"]):
        identity, kind, fpga = segment["id"], segment["kind"], segment["fpga"]
        constant = (kind == "launch_to_tx" and
                    segment.get("source_semantics") == "configuration-stable-constant")
        if kind == "launch_to_tx":
            start, source = 0, "configuration-stable-constant@slot0" if constant else "architectural-launch@slot0"
        else:
            arrival = arrivals.get((segment["source_cut_net"], fpga))
            if arrival is None:
                raise ValidationError("static exact segment has no source arrival")
            start, source = arrival["arrival_slot"], arrival["id"]
        if kind in {"launch_to_tx", "rx_to_tx"}:
            deadline, sink = tx(segment["sink_cut_net"], fpga)
        elif kind == "rx_to_capture":
            deadline, sink = timing["commit_slot"], segment["capture_requirement"]
        else:
            raise ValidationError("static exact segment kind is invalid")
        available = deadline-start
        if available < 0:
            raise ValidationError("static exact segment has a negative window")
        physical = evidence.get(identity, [])
        record = dict(id=identity, kind=kind, fpga=fpga, start_slot=start, deadline_slot=deadline,
            available_slots=available, slot_period_ns=slot, deadline_budget_ns=available*slot,
            clock_uncertainty_ns=uncertainty, causal_source=source, causal_sink=sink,
            schedule_settle_slots=0 if constant else timing["settle_slots"])
        if constant:
            if physical or segment.get("budget_slots") != 0:
                raise ValidationError("static exact constant must have zero budget and no dynamic evidence")
            record.update(evidence="structural-configuration-stable-constant", physical_measurements=0,
                          physical_delay_bound_ns=0.0, slack_ns=available*slot-uncertainty, status="pass")
        elif not physical:
            missing.append(identity)
            record.update(evidence="missing", physical_delay_bound_ns=None, slack_ns=None, status="incomplete")
        else:
            delay = max(float(row["delay_ns"]) for row in physical)
            bound = any(row.get("measurement", "endpoint-exact") in {
                "cut-net-cone-upper-bound", "optimized-endpoint-collection-upper-bound"} for row in physical)
            slack = available*slot-uncertainty-delay
            record.update(evidence="routed-cone-upper-bound" if bound else "routed-endpoint-exact",
                physical_measurements=len(physical), physical_delay_bound_ns=delay,
                slack_ns=slack, status="pass" if slack >= -1.e-9 else "fail")
            if record["status"] == "fail":
                failed.append(identity)
        result.append(record)
    if len({row["id"] for row in result}) != len(result) or set(evidence)-{row["id"] for row in result}:
        raise ValidationError("physical timing contains unknown or duplicate static exact segments")
    qualified = [row for row in result if row["slack_ns"] is not None]
    exact = sum(row["evidence"] == "routed-endpoint-exact" for row in qualified)
    constants = sum(row["evidence"] == "structural-configuration-stable-constant" for row in qualified)
    measured = len(qualified)-constants
    qualification = ("incomplete-missing-segment-evidence" if missing else "routed-segment-deadline-fail" if failed
        else "routed-endpoint-exact-deadline-pass" if exact == len(result)
        else "routed-endpoint-exact-plus-structural-constant-deadline-pass" if exact+constants == len(result)
        else "routed-conservative-bound-deadline-pass")
    def worst(capture):
        slacks = [row["slack_ns"] for row in qualified if (row["kind"] == "rx_to_capture") == capture]
        return min(slacks) if slacks else None
    return dict(schema=_SCHEMA, status="incomplete" if missing else "fail" if failed else "pass",
        design=schedule["design"], platform=platform.name,
        provider="static-exact-routed-causal-deadline-reconstruction-v1", qualification=qualification,
        semantic_contract_sha256=digest, slot_edge_convention=timing["slot_edge_convention"],
        commit_slot=timing["commit_slot"], slot_period_ns=slot, clock_uncertainty_ns=uncertainty,
        coverage=dict(contract_segments=len(result), measured_segments=measured, endpoint_exact_segments=exact,
            structural_constant_segments=constants, conservative_bound_segments=measured-exact, missing_segments=len(missing)),
        worst_source_ready_slack_ns=worst(False), worst_final_capture_slack_ns=worst(True),
        failed_segments=failed, missing_segments=missing, segments=result)


def validate_static_exact_segment_deadlines(database, schedule, contract, physical_summary, platform):
    """Compare a small result to the arithmetic oracle; no second flow engine."""
    expected = build_static_exact_segment_deadlines(schedule, contract, physical_summary, platform)
    def same(actual, wanted):
        if type(wanted) is float:
            return type(actual) in (int, float) and math.isclose(actual, wanted, rel_tol=0., abs_tol=1.e-9)
        if isinstance(wanted, dict):
            return isinstance(actual, dict) and actual.keys() == wanted.keys() and all(same(actual[key], value) for key,value in wanted.items())
        if isinstance(wanted, list):
            return isinstance(actual, list) and len(actual) == len(wanted) and all(same(a,b) for a,b in zip(actual,wanted))
        return type(actual) is type(wanted) and actual == wanted
    if not same(database, expected):
        raise ValidationError("static exact constant deadline or routed deadline reconstruction disagrees")
    return dict(status=expected["status"], qualification=expected["qualification"], **expected["coverage"],
        failed_segments=len(expected["failed_segments"]), worst_source_ready_slack_ns=expected["worst_source_ready_slack_ns"],
        worst_final_capture_slack_ns=expected["worst_final_capture_slack_ns"])
