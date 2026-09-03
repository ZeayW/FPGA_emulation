"""Physical deadline qualification for static exact combinational cuts."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .combinational_cut import semantic_contract_sha256
from .errors import ValidationError
from .logic_segment_timing import validate_logic_segment_timing
from .platform import Platform


STATIC_EXACT_DEADLINE_SCHEMA = "emuflow.static-exact-segment-deadlines/v2"
def _finite_nonnegative(value: Any, context: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0.0
    ):
        raise ValidationError(f"{context} must be finite and non-negative")
    return float(value)


def _exact_contract(
    schedule: Mapping[str, Any],
    semantic_contract: Mapping[str, Any],
) -> Tuple[Mapping[str, Any], str, Mapping[str, Any]]:
    from .tdm import (
        is_sampled_virtual_wire_schedule,
        sampled_virtual_wire_timing_constraints,
    )

    if not is_sampled_virtual_wire_schedule(schedule):
        raise ValidationError("static exact deadlines require an exact schedule")
    contract = semantic_contract
    if (
        not isinstance(contract, dict)
        or contract.get("mode") != "static-exact-combinational"
    ):
        raise ValidationError("static exact schedule contract is invalid")
    digest = semantic_contract_sha256(contract)
    if (
        schedule.get("semantic_contract_schema") != contract.get("schema")
        or schedule.get("semantic_contract_sha256") != digest
    ):
        raise ValidationError("static exact schedule contract digest disagrees")
    frame_slots = schedule.get("metrics", {}).get("frame_slots")
    timing_constraints = schedule.get("timing_constraints")
    if (
        isinstance(frame_slots, bool)
        or not isinstance(frame_slots, int)
        or frame_slots <= 1
        or timing_constraints
        != sampled_virtual_wire_timing_constraints(frame_slots)
    ):
        raise ValidationError("static exact commit slot disagrees with schedule")
    return contract, digest, timing_constraints


def _slot_period_ns(platform: Platform, entries: List[Mapping[str, Any]]) -> float:
    links = {link.id: link for link in platform.links}
    periods = set()
    for entry in entries:
        link = links.get(entry.get("link"))
        if link is None:
            raise ValidationError(
                f"static exact entry {entry.get('id')!r} uses an unknown link"
            )
        periods.add(round(1000.0 / link.fabric_clock_mhz, 12))
    if len(periods) != 1:
        raise ValidationError(
            "static exact v1 requires one common fabric slot period"
        )
    if not periods:
        raise ValidationError("static exact schedule has no transport entries")
    return periods.pop()


def _physical_segment_evidence(
    physical_summary: Mapping[str, Any],
    contract_digest: str,
) -> Dict[str, List[Mapping[str, Any]]]:
    raw = physical_summary.get("logic_segment_timing")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValidationError("physical logic segment timing map is invalid")
    evidence: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    seen = set()
    for fpga, database in sorted(raw.items()):
        validation = validate_logic_segment_timing(database)
        if validation["fpga"] != fpga:
            raise ValidationError(
                "static exact logic segment FPGA identity disagrees"
            )
        if database.get("semantic_contract_sha256") != contract_digest:
            raise ValidationError(
                "static exact logic timing is bound to another contract"
            )
        for item in database["segments"]:
            physical_id = item["id"]
            if physical_id in seen:
                raise ValidationError(
                    f"duplicate physical logic segment {physical_id!r}"
                )
            seen.add(physical_id)
            exact_id = item.get("static_exact_segment_id")
            if not isinstance(exact_id, str) or not exact_id:
                raise ValidationError(
                    "static exact physical segment lacks contract identity"
                )
            evidence[exact_id].append(item)
    return evidence


def _arrival_by_net_fpga(
    entries: List[Mapping[str, Any]],
) -> Dict[Tuple[str, str], Mapping[str, Any]]:
    arrivals = {}
    for entry in entries:
        key = (entry["net"], entry["to"])
        if key in arrivals:
            raise ValidationError(
                f"static exact cut {entry['net']!r} arrives more than once "
                f"at {entry['to']!r}"
            )
        arrivals[key] = entry
    return arrivals


def _source_entries(
    entries_by_net: Mapping[str, List[Mapping[str, Any]]],
    net: str,
    fpga: str,
) -> List[Mapping[str, Any]]:
    result = [
        item for item in entries_by_net.get(net, []) if item["from"] == fpga
    ]
    if not result:
        raise ValidationError(
            f"static exact cut {net!r} has no TX from source {fpga!r}"
        )
    return result


def build_static_exact_segment_deadlines(
    schedule: Mapping[str, Any],
    semantic_contract: Mapping[str, Any],
    physical_summary: Mapping[str, Any],
    platform: Platform,
) -> Dict[str, Any]:
    """Rebuild physical settle windows for every exact semantic segment."""
    contract, digest, timing_constraints = _exact_contract(
        schedule, semantic_contract
    )
    from .tdm import sampled_logic_segment_budget_slots
    entries = list(schedule.get("entries", []))
    slot_ns = _slot_period_ns(platform, entries)
    uncertainty_ns = _finite_nonnegative(
        physical_summary.get("static_exact_clock_uncertainty_ns", 0.0),
        "static exact clock uncertainty",
    )
    evidence_by_segment = _physical_segment_evidence(
        physical_summary, digest
    )
    entries_by_net: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for entry in entries:
        entries_by_net[entry["net"]].append(entry)
    arrivals = _arrival_by_net_fpga(entries)
    commit_slot = timing_constraints["commit_slot"]
    records = []
    missing = []
    for segment in sorted(contract["logic_segments"], key=lambda item: item["id"]):
        segment_id = segment["id"]
        kind = segment["kind"]
        fpga = segment["fpga"]
        configuration_stable_constant = (
            kind == "launch_to_tx"
            and segment.get("source_semantics")
            == "configuration-stable-constant"
        )
        if kind == "launch_to_tx":
            tx_entries = _source_entries(
                entries_by_net, segment["sink_cut_net"], fpga
            )
            start_slot = 0
            deadline_slot = min(item["slot"] for item in tx_entries)
            causal_source = (
                "configuration-stable-constant@slot0"
                if configuration_stable_constant
                else "architectural-launch@slot0"
            )
            causal_sink = ",".join(sorted(item["id"] for item in tx_entries))
        elif kind == "rx_to_tx":
            arrival = arrivals.get((segment["source_cut_net"], fpga))
            if arrival is None:
                raise ValidationError(
                    f"static exact segment {segment_id!r} has no source arrival"
                )
            tx_entries = _source_entries(
                entries_by_net, segment["sink_cut_net"], fpga
            )
            start_slot = arrival["arrival_slot"]
            deadline_slot = min(item["slot"] for item in tx_entries)
            causal_source = arrival["id"]
            causal_sink = ",".join(sorted(item["id"] for item in tx_entries))
        elif kind == "rx_to_capture":
            arrival = arrivals.get((segment["source_cut_net"], fpga))
            if arrival is None:
                raise ValidationError(
                    f"static exact segment {segment_id!r} has no source arrival"
                )
            start_slot = arrival["arrival_slot"]
            deadline_slot = commit_slot
            causal_source = arrival["id"]
            causal_sink = segment["capture_requirement"]
        else:
            raise ValidationError(
                f"static exact segment {segment_id!r} kind is invalid"
            )
        available_slots = deadline_slot - start_slot
        if available_slots < 0:
            raise ValidationError(
                f"static exact segment {segment_id!r} has a negative window"
            )
        physical = evidence_by_segment.get(segment_id, [])
        record = {
            "id": segment_id,
            "kind": kind,
            "fpga": fpga,
            "start_slot": start_slot,
            "deadline_slot": deadline_slot,
            "available_slots": available_slots,
            "slot_period_ns": slot_ns,
            "deadline_budget_ns": available_slots * slot_ns,
            "clock_uncertainty_ns": uncertainty_ns,
            "causal_source": causal_source,
            "causal_sink": causal_sink,
            "schedule_settle_slots": sampled_logic_segment_budget_slots(
                segment, timing_constraints
            ),
        }
        if configuration_stable_constant:
            if sampled_logic_segment_budget_slots(
                segment, timing_constraints
            ) != 0:
                raise ValidationError(
                    f"static exact constant segment {segment_id!r} must "
                    "have a zero-slot budget"
                )
            if physical:
                raise ValidationError(
                    f"static exact constant segment {segment_id!r} has "
                    "unexpected dynamic physical evidence"
                )
            record.update(
                {
                    "evidence": "structural-configuration-stable-constant",
                    "physical_measurements": 0,
                    "physical_delay_bound_ns": 0.0,
                    "slack_ns": available_slots * slot_ns - uncertainty_ns,
                    "status": "pass",
                }
            )
        elif not physical:
            missing.append(segment_id)
            record.update(
                {
                    "evidence": "missing",
                    "physical_delay_bound_ns": None,
                    "slack_ns": None,
                    "status": "incomplete",
                }
            )
        else:
            delay = max(float(item["delay_ns"]) for item in physical)
            cone_bound = any(
                item.get("measurement", "endpoint-exact")
                == "cut-net-cone-upper-bound"
                for item in physical
            )
            slack = available_slots * slot_ns - uncertainty_ns - delay
            record.update(
                {
                    "evidence": (
                        "routed-cone-upper-bound"
                        if cone_bound
                        else "routed-endpoint-exact"
                    ),
                    "physical_measurements": len(physical),
                    "physical_delay_bound_ns": delay,
                    "slack_ns": slack,
                    "status": "pass" if slack >= -1.0e-9 else "fail",
                }
            )
        records.append(record)
    unknown = sorted(set(evidence_by_segment) - {item["id"] for item in records})
    if unknown:
        raise ValidationError(
            f"physical timing contains unknown static exact segments {unknown[:10]}"
        )
    qualified = [item for item in records if item["slack_ns"] is not None]
    source_records = [
        item for item in qualified if item["kind"] != "rx_to_capture"
    ]
    capture_records = [
        item for item in qualified if item["kind"] == "rx_to_capture"
    ]
    failed = [item["id"] for item in qualified if item["status"] == "fail"]
    status = "incomplete" if missing else "fail" if failed else "pass"
    endpoint_exact = sum(
        item["evidence"] == "routed-endpoint-exact" for item in qualified
    )
    structural_constants = sum(
        item["evidence"] == "structural-configuration-stable-constant"
        for item in qualified
    )
    physically_measured = len(qualified) - structural_constants
    return {
        "schema": STATIC_EXACT_DEADLINE_SCHEMA,
        "status": status,
        "design": schedule["design"],
        "platform": platform.name,
        "provider": "static-exact-routed-causal-deadline-reconstruction-v1",
        "qualification": (
            "incomplete-missing-segment-evidence"
            if missing
            else (
                "routed-segment-deadline-fail"
                if failed
                else (
                    "routed-endpoint-exact-deadline-pass"
                    if endpoint_exact == len(records)
                    else (
                        "routed-endpoint-exact-plus-structural-constant-"
                        "deadline-pass"
                        if endpoint_exact + structural_constants
                        == len(records)
                        else "routed-conservative-bound-deadline-pass"
                    )
                )
            )
        ),
        "semantic_contract_sha256": digest,
        "slot_edge_convention": timing_constraints["slot_edge_convention"],
        "commit_slot": commit_slot,
        "slot_period_ns": slot_ns,
        "clock_uncertainty_ns": uncertainty_ns,
        "coverage": {
            "contract_segments": len(records),
            "measured_segments": physically_measured,
            "endpoint_exact_segments": endpoint_exact,
            "structural_constant_segments": structural_constants,
            "conservative_bound_segments": physically_measured - endpoint_exact,
            "missing_segments": len(missing),
        },
        "worst_source_ready_slack_ns": (
            min(item["slack_ns"] for item in source_records)
            if source_records
            else None
        ),
        "worst_final_capture_slack_ns": (
            min(item["slack_ns"] for item in capture_records)
            if capture_records
            else None
        ),
        "failed_segments": failed,
        "missing_segments": missing,
        "segments": records,
    }


def validate_static_exact_segment_deadlines(
    database: Mapping[str, Any],
    schedule: Mapping[str, Any],
    semantic_contract: Mapping[str, Any],
    physical_summary: Mapping[str, Any],
    platform: Platform,
) -> Dict[str, Any]:
    """Independently reject any changed deadline, identity, or coverage field."""
    if database.get("schema") != STATIC_EXACT_DEADLINE_SCHEMA:
        raise ValidationError("static exact deadline schema is invalid")
    contract, digest, timing_constraints = _exact_contract(
        schedule, semantic_contract
    )
    if (
        database.get("design") != schedule.get("design")
        or database.get("platform") != platform.name
        or database.get("provider")
        != "static-exact-routed-causal-deadline-reconstruction-v1"
        or database.get("semantic_contract_sha256") != digest
        or database.get("slot_edge_convention")
        != timing_constraints["slot_edge_convention"]
        or database.get("commit_slot") != timing_constraints["commit_slot"]
    ):
        raise ValidationError("static exact deadline identity disagrees")
    entries = list(schedule["entries"])
    slot_ns = _slot_period_ns(platform, entries)
    uncertainty_ns = _finite_nonnegative(
        physical_summary.get("static_exact_clock_uncertainty_ns", 0.0),
        "static exact clock uncertainty",
    )
    if (
        not math.isclose(
            float(database.get("slot_period_ns", -1.0)),
            slot_ns,
            rel_tol=0.0,
            abs_tol=1.0e-9,
        )
        or not math.isclose(
            float(database.get("clock_uncertainty_ns", -1.0)),
            uncertainty_ns,
            rel_tol=0.0,
            abs_tol=1.0e-9,
        )
    ):
        raise ValidationError("static exact deadline clock contract disagrees")
    evidence = _physical_segment_evidence(physical_summary, digest)
    records = database.get("segments")
    if not isinstance(records, list):
        raise ValidationError("static exact deadline records are invalid")
    records_by_id = {
        item.get("id"): item for item in records if isinstance(item, dict)
    }
    semantic_by_id = {
        item["id"]: item for item in contract["logic_segments"]
    }
    if (
        len(records_by_id) != len(records)
        or set(records_by_id) != set(semantic_by_id)
        or set(evidence) - set(semantic_by_id)
    ):
        raise ValidationError("static exact deadline segment coverage disagrees")

    arrivals = {}
    tx_by_net_fpga: Dict[Tuple[str, str], List[Mapping[str, Any]]] = defaultdict(list)
    for entry in entries:
        tx_by_net_fpga[(entry["net"], entry["from"])].append(entry)
        arrival_key = (entry["net"], entry["to"])
        if arrival_key in arrivals:
            raise ValidationError(
                "static exact validator found a duplicate current-frame arrival"
            )
        arrivals[arrival_key] = entry

    missing = []
    failed = []
    source_slacks = []
    capture_slacks = []
    endpoint_exact = 0
    structural_constants = 0
    for segment_id, semantic in sorted(semantic_by_id.items()):
        record = records_by_id[segment_id]
        kind = semantic["kind"]
        fpga = semantic["fpga"]
        configuration_stable_constant = (
            kind == "launch_to_tx"
            and semantic.get("source_semantics")
            == "configuration-stable-constant"
        )
        if kind == "launch_to_tx":
            starts = 0
            txs = tx_by_net_fpga.get((semantic["sink_cut_net"], fpga), [])
            if not txs:
                raise ValidationError("static exact validator found no source TX")
            deadline = min(item["slot"] for item in txs)
            causal_source = (
                "configuration-stable-constant@slot0"
                if configuration_stable_constant
                else "architectural-launch@slot0"
            )
            causal_sink = ",".join(sorted(item["id"] for item in txs))
        elif kind == "rx_to_tx":
            arrival = arrivals.get((semantic["source_cut_net"], fpga))
            txs = tx_by_net_fpga.get((semantic["sink_cut_net"], fpga), [])
            if arrival is None or not txs:
                raise ValidationError(
                    "static exact validator found an incomplete RX-to-TX chain"
                )
            starts = arrival["arrival_slot"]
            deadline = min(item["slot"] for item in txs)
            causal_source = arrival["id"]
            causal_sink = ",".join(sorted(item["id"] for item in txs))
        else:
            arrival = arrivals.get((semantic["source_cut_net"], fpga))
            if arrival is None:
                raise ValidationError(
                    "static exact validator found an incomplete capture chain"
                )
            starts = arrival["arrival_slot"]
            deadline = timing_constraints["commit_slot"]
            causal_source = arrival["id"]
            causal_sink = semantic["capture_requirement"]
        available = deadline - starts
        if available < 0:
            raise ValidationError("static exact validator found a negative window")
        fixed = {
            "id": segment_id,
            "kind": kind,
            "fpga": fpga,
            "start_slot": starts,
            "deadline_slot": deadline,
            "available_slots": available,
            "causal_source": causal_source,
            "causal_sink": causal_sink,
            "schedule_settle_slots": (
                0
                if kind == "launch_to_tx"
                and semantic.get("source_semantics")
                == "configuration-stable-constant"
                else timing_constraints["settle_slots"]
            ),
        }
        for field, value in fixed.items():
            if record.get(field) != value:
                raise ValidationError(
                    f"static exact deadline {segment_id!r}.{field} disagrees"
                )
        for field, value in (
            ("slot_period_ns", slot_ns),
            ("deadline_budget_ns", available * slot_ns),
            ("clock_uncertainty_ns", uncertainty_ns),
        ):
            if not math.isclose(
                float(record.get(field, -1.0)),
                value,
                rel_tol=0.0,
                abs_tol=1.0e-9,
            ):
                raise ValidationError(
                    f"static exact deadline {segment_id!r}.{field} disagrees"
                )
        physical = evidence.get(segment_id, [])
        if configuration_stable_constant:
            if fixed["schedule_settle_slots"] != 0:
                raise ValidationError(
                    f"static exact constant segment {segment_id!r} must "
                    "have a zero-slot budget"
                )
            if physical:
                raise ValidationError(
                    f"static exact constant segment {segment_id!r} has "
                    "unexpected dynamic physical evidence"
                )
            slack = available * slot_ns - uncertainty_ns
            if (
                record.get("evidence")
                != "structural-configuration-stable-constant"
                or record.get("physical_measurements") != 0
                or record.get("physical_delay_bound_ns") != 0.0
                or record.get("status") != "pass"
                or not math.isclose(
                    float(record.get("slack_ns", math.inf)),
                    slack,
                    rel_tol=0.0,
                    abs_tol=1.0e-9,
                )
            ):
                raise ValidationError(
                    f"static exact constant deadline {segment_id!r} disagrees"
                )
            structural_constants += 1
            source_slacks.append(slack)
            continue
        if not physical:
            missing.append(segment_id)
            if any(
                record.get(field) != value
                for field, value in (
                    ("evidence", "missing"),
                    ("physical_delay_bound_ns", None),
                    ("slack_ns", None),
                    ("status", "incomplete"),
                )
            ):
                raise ValidationError(
                    f"static exact missing evidence {segment_id!r} disagrees"
                )
            continue
        delay = max(float(item["delay_ns"]) for item in physical)
        cone_bound = any(
            item.get("measurement", "endpoint-exact")
            == "cut-net-cone-upper-bound"
            for item in physical
        )
        evidence_name = (
            "routed-cone-upper-bound"
            if cone_bound
            else "routed-endpoint-exact"
        )
        slack = available * slot_ns - uncertainty_ns - delay
        record_status = "pass" if slack >= -1.0e-9 else "fail"
        if (
            record.get("evidence") != evidence_name
            or record.get("physical_measurements") != len(physical)
            or record.get("status") != record_status
            or not math.isclose(
                float(record.get("physical_delay_bound_ns", -1.0)),
                delay,
                rel_tol=0.0,
                abs_tol=1.0e-9,
            )
            or not math.isclose(
                float(record.get("slack_ns", math.inf)),
                slack,
                rel_tol=0.0,
                abs_tol=1.0e-9,
            )
        ):
            raise ValidationError(
                f"static exact physical deadline {segment_id!r} disagrees"
            )
        endpoint_exact += not cone_bound
        (capture_slacks if kind == "rx_to_capture" else source_slacks).append(
            slack
        )
        if record_status == "fail":
            failed.append(segment_id)
    qualified = len(records) - len(missing)
    measured = qualified - structural_constants
    status = "incomplete" if missing else "fail" if failed else "pass"
    qualification = (
        "incomplete-missing-segment-evidence"
        if missing
        else (
            "routed-segment-deadline-fail"
            if failed
            else (
                "routed-endpoint-exact-deadline-pass"
                if endpoint_exact == len(records)
                else (
                    "routed-endpoint-exact-plus-structural-constant-"
                    "deadline-pass"
                    if endpoint_exact + structural_constants == len(records)
                    else "routed-conservative-bound-deadline-pass"
                )
            )
        )
    )
    expected_coverage = {
        "contract_segments": len(records),
            "measured_segments": measured,
            "endpoint_exact_segments": endpoint_exact,
            "structural_constant_segments": structural_constants,
            "conservative_bound_segments": measured - endpoint_exact,
        "missing_segments": len(missing),
    }
    if (
        database.get("status") != status
        or database.get("qualification") != qualification
        or database.get("coverage") != expected_coverage
        or database.get("failed_segments") != failed
        or database.get("missing_segments") != missing
        or database.get("worst_source_ready_slack_ns")
        != (min(source_slacks) if source_slacks else None)
        or database.get("worst_final_capture_slack_ns")
        != (min(capture_slacks) if capture_slacks else None)
    ):
        raise ValidationError(
            "static exact deadline database disagrees with reconstruction"
        )
    return {
        "status": database["status"],
        "qualification": database["qualification"],
        **database["coverage"],
        "failed_segments": len(database["failed_segments"]),
        "worst_source_ready_slack_ns": database[
            "worst_source_ready_slack_ns"
        ],
        "worst_final_capture_slack_ns": database[
            "worst_final_capture_slack_ns"
        ],
    }
