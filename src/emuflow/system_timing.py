"""Unified timing closure across placed/routed FPGA partitions and links."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional

from .board_link_timing import validate_board_link_timing
from .boundary_timing import validate_boundary_timing_database
from .errors import ValidationError
from .logic_segment_timing import validate_logic_segment_timing
from .local_path_timing import (
    path_id_set_sha256,
    validate_local_path_timing,
)
from .platform import Platform
from .static_exact_timing import (
    build_static_exact_segment_deadlines,
    validate_static_exact_segment_deadlines,
)
from .tdm import reconstruct_tdm_schedule_timing_paths


SYSTEM_TIMING_SCHEMA = "emuflow.system-timing/v2"


def _finite_number(value: Any, context: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValidationError(f"{context} must be a finite number")
    return float(value)


def _physical_delay_database(
    physical_summary: Mapping[str, Any],
) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for item in physical_summary["fpgas"]:
        fpga = item["fpga"]
        raw_delays = item.get("clock_domain_delays_ns", {})
        if not isinstance(raw_delays, dict):
            raise ValidationError(
                f"physical summary {fpga}.clock_domain_delays_ns "
                "must be an object"
            )

        def delay(domain: str) -> float:
            if domain not in raw_delays:
                raise ValidationError(
                    f"physical summary {fpga} lacks the {domain} physical "
                    "delay required for unified system timing"
                )
            value = _finite_number(
                raw_delays[domain],
                f"physical summary {fpga}.{domain} delay",
            )
            if value < 0.0:
                raise ValidationError(
                    f"physical summary {fpga}.{domain} delay must be "
                    "non-negative"
                )
            return value

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
                f"physical summary {fpga}.clock_domain_presence is invalid"
            )
        else:
            dut_present = presence["dut"]
        dut_delay = delay("dut")
        cross_delay = delay("cross")
        if not dut_present and (dut_delay != 0.0 or cross_delay != 0.0):
            raise ValidationError(
                f"physical summary {fpga} assigns delay to an absent DUT clock"
            )
        result[fpga] = {
            "dut": dut_delay,
            # The physical result exposes the maximum constrained crossing
            # delay. Use it for both launch and capture interfaces; this is a
            # conservative bound until endpoint-specific timing is exported.
            "cross": cross_delay,
            "dut_present": dut_present,
        }
    return result


def _logic_partition_sequence(
    transitions: List[Mapping[str, Any]],
) -> tuple[List[str], int]:
    if not transitions:
        raise ValidationError(
            "system timing path has no logical cut transitions"
        )
    sequence: List[str] = []
    discontinuities = 0
    for index, transition in enumerate(transitions):
        source = transition.get("from")
        sink = transition.get("to")
        if not isinstance(source, str) or not isinstance(sink, str):
            raise ValidationError(
                f"system timing transition {index} is invalid"
            )
        if not sequence:
            sequence.extend((source, sink))
        elif sequence[-1] == source:
            sequence.append(sink)
        else:
            # Compressed STA records can combine conservative representatives
            # whose selected multicast sinks do not form one exact endpoint
            # chain. Retain both physical segments and record the loss of path
            # exactness rather than silently omitting either delay.
            discontinuities += 1
            sequence.extend((source, sink))
    return sequence, discontinuities


def _endpoint_delay_database(
    physical_summary: Mapping[str, Any],
) -> Optional[Dict[str, float]]:
    raw_timing = physical_summary.get("boundary_timing")
    if raw_timing is None:
        return None
    identities = physical_summary.get("boundary_identities")
    if not isinstance(raw_timing, dict) or not isinstance(identities, dict):
        raise ValidationError("physical boundary timing/identity maps are invalid")
    if set(raw_timing) != set(identities):
        raise ValidationError("physical boundary timing FPGA coverage disagrees")
    result: Dict[str, float] = {}
    for fpga, database in raw_timing.items():
        validate_boundary_timing_database(database, identities[fpga])
        for endpoint in database["endpoints"]:
            endpoint_id = endpoint["id"]
            if endpoint_id in result:
                raise ValidationError(
                    f"duplicate physical boundary timing endpoint {endpoint_id!r}"
                )
            result[endpoint_id] = _finite_number(
                endpoint["delay_ns"],
                f"physical boundary endpoint {endpoint_id}",
            )
    return result


def _logic_segment_database(
    physical_summary: Mapping[str, Any],
) -> Optional[Dict[str, Dict[str, List[Mapping[str, Any]]]]]:
    raw = physical_summary.get("logic_segment_timing")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValidationError("physical logic segment timing map is invalid")
    result: Dict[str, Dict[str, List[Mapping[str, Any]]]] = {}
    segment_ids = set()
    for fpga, database in raw.items():
        validation = validate_logic_segment_timing(database)
        if validation["fpga"] != fpga:
            raise ValidationError(
                "physical logic segment timing FPGA identity disagrees"
            )
        for segment in database["segments"]:
            segment_id = segment["id"]
            if segment_id in segment_ids:
                raise ValidationError(
                    f"duplicate physical logic segment {segment_id!r}"
                )
            segment_ids.add(segment_id)
            result.setdefault(segment["system_path"], {}).setdefault(
                segment["member_path"], []
            ).append(segment)
    return result


def _board_link_delay_database(
    physical_summary: Mapping[str, Any], platform: Platform
) -> Optional[Dict[tuple[str, str, str], float]]:
    raw = physical_summary.get("board_link_timing")
    if raw is None:
        return None
    validate_board_link_timing(raw, platform)
    return {
        (item["link"], item["from"], item["to"]): float(
            item["delay_bound_ns"]
        )
        for item in raw["links"]
    }


def _sampled_virtual_wire_path_delay(
    record: Mapping[str, Any],
    member: str,
    segments: List[Mapping[str, Any]],
    endpoint_delays: Mapping[str, float],
    board_link_delays: Optional[Mapping[tuple[str, str, str], float]],
    schedule_entries: Mapping[str, Mapping[str, Any]],
    routes_by_net: Mapping[str, Mapping[str, Any]],
    *,
    commit_slot: int,
    uncertainty_ns: float,
) -> Dict[str, Any]:
    """Propagate one sampled path through its fixed physical schedule.

    A sampled virtual wire is not an additive ``logic + TDM`` path.  Each
    first-hop TX samples at the concrete Phase-5 slot, so logic that becomes
    ready earlier waits until that slot and logic that becomes ready later is
    a physical timing failure.  Relay TX endpoints obey the same rule.  This
    evaluator deliberately consumes the fixed schedule; it never reschedules
    a path during Phase 7C.
    """

    ordered_segments = sorted(segments, key=lambda item: item["cut_index"])
    cut_nets = list(record["cut_nets"])
    if len(ordered_segments) != len(cut_nets) + 1:
        raise ValidationError(
            f"sampled path {record['path']!r}/{member!r} has incomplete "
            "physical logic-segment coverage"
        )
    scheduled_hops = list(record["scheduled_hops"])
    hops_by_cut: List[List[Mapping[str, Any]]] = []
    used_entries = set()
    for net in cut_nets:
        route = routes_by_net.get(net)
        if route is None:
            raise ValidationError(
                f"sampled path {record['path']!r} has no route for cut {net!r}"
            )
        demand = route.get("id")
        hops = [hop for hop in scheduled_hops if hop["demand"] == demand]
        if not hops:
            raise ValidationError(
                f"sampled path {record['path']!r} has no scheduled hops for "
                f"cut {net!r}"
            )
        for hop in hops:
            entry_id = hop["schedule_entry"]
            if entry_id in used_entries:
                raise ValidationError(
                    f"sampled path {record['path']!r} reuses schedule entry "
                    f"{entry_id!r}"
                )
            used_entries.add(entry_id)
        hops_by_cut.append(hops)
    if len(used_entries) != len(scheduled_hops):
        raise ValidationError(
            f"sampled path {record['path']!r} scheduled-hop binding is not exact"
        )

    arrival_ns = 0.0
    physical_logic_ns = 0.0
    physical_interface_ns = 0.0
    board_ns = 0.0
    minimum_tx_slack_ns = math.inf
    for cut_index, hops in enumerate(hops_by_cut):
        segment = ordered_segments[cut_index]
        segment_delay = float(segment["delay_ns"])
        physical_logic_ns += segment_delay
        ready_ns = arrival_ns + segment_delay
        expected_tx = hops[0]["tx_endpoint"]
        if segment.get("replace_tx_endpoint") != expected_tx:
            raise ValidationError(
                f"sampled path {record['path']!r}/{member!r} logic segment "
                f"{cut_index} does not terminate at its first TX"
            )
        for hop_index, hop in enumerate(hops):
            entry_id = hop["schedule_entry"]
            entry = schedule_entries.get(entry_id)
            if entry is None:
                raise ValidationError(
                    f"sampled path {record['path']!r} references missing "
                    f"schedule entry {entry_id!r}"
                )
            slot_ns = float(hop["tdm_slot_ns"])
            tx_time_ns = float(entry["slot"]) * slot_ns
            if hop_index:
                relay_tx_delay = endpoint_delays[hop["tx_endpoint"]]
                ready_ns = arrival_ns + relay_tx_delay
                physical_interface_ns += relay_tx_delay
            tx_slack_ns = tx_time_ns - uncertainty_ns - ready_ns
            minimum_tx_slack_ns = min(minimum_tx_slack_ns, tx_slack_ns)
            key = (hop["link"], hop["from"], hop["to"])
            link_delay_ns = (
                float(board_link_delays[key])
                if board_link_delays is not None
                else float(hop["base_link_delay_ns"])
            )
            rx_delay_ns = endpoint_delays[hop["rx_endpoint"]]
            board_ns += link_delay_ns
            physical_interface_ns += rx_delay_ns
            arrival_ns = tx_time_ns + link_delay_ns + rx_delay_ns
            ready_ns = arrival_ns

    capture = ordered_segments[-1]
    if (
        capture.get("kind") != "capture"
        or capture.get("replace_tx_endpoint") is not None
    ):
        raise ValidationError(
            f"sampled path {record['path']!r}/{member!r} lacks one final "
            "physical capture segment"
        )
    capture_delay_ns = float(capture["delay_ns"])
    physical_logic_ns += capture_delay_ns
    capture_ready_ns = arrival_ns + capture_delay_ns
    slot_periods = {float(hop["tdm_slot_ns"]) for hop in scheduled_hops}
    if len(slot_periods) != 1:
        raise ValidationError(
            "sampled virtual-wire global timing requires one slot period"
        )
    slot_period_ns = slot_periods.pop()
    commit_deadline_ns = float(commit_slot) * slot_period_ns
    capture_slack_ns = commit_deadline_ns - uncertainty_ns - capture_ready_ns
    causal_slack_ns = min(minimum_tx_slack_ns, capture_slack_ns)
    total_delay_ns = capture_ready_ns + uncertainty_ns
    physical_stage_ns = physical_logic_ns + physical_interface_ns
    return {
        "system_delay_bound_ns": total_delay_ns,
        "physical_logic_delay_bound_ns": physical_logic_ns,
        "physical_interface_delay_bound_ns": physical_interface_ns,
        "physical_routed_stage_delay_bound_ns": physical_stage_ns,
        "scheduled_link_tdm_delay_ns": max(
            0.0, capture_ready_ns - physical_stage_ns
        ),
        "sampled_event_timing_status": (
            "pass" if causal_slack_ns >= -1.0e-9 else "fail"
        ),
        "minimum_tx_readiness_slack_ns": minimum_tx_slack_ns,
        "capture_commit_slack_ns": capture_slack_ns,
        "clock_uncertainty_ns": uncertainty_ns,
        "board_delay_bound_ns": board_ns,
    }


def _local_path_database(
    physical_summary: Mapping[str, Any],
    virtual_period: float,
    routes_artifact_sha256: str | None,
) -> tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    raw = physical_summary.get("local_path_timing")
    if raw is None:
        return [], None
    if not isinstance(raw, dict) or not raw:
        raise ValidationError("physical local path timing map is invalid")
    records: List[Dict[str, Any]] = []
    source = None
    ids = set()
    for fpga, database in sorted(raw.items()):
        validation = validate_local_path_timing(database)
        if validation["fpga"] != fpga:
            raise ValidationError("physical local path FPGA identity disagrees")
        if source is None:
            source = dict(database["source"])
        elif database["source"] != source:
            raise ValidationError("physical local path source seals disagree")
        for item in database["paths"]:
            path_id = item["id"]
            if path_id in ids:
                raise ValidationError(
                    f"duplicate physical local timing path {path_id!r}"
                )
            ids.add(path_id)
            delay = float(item["delay_ns"])
            period = float(item["clock_period_ns"])
            required_time = float(item.get("required_time_ns", period))
            measurement = item.get(
                "measurement", "endpoint-longest-path-fallback"
            )
            if measurement == "explicit-routed-path-chain":
                physical_logic_model = "routed-selected-path-chain-exact"
                physical_logic_exact = True
                physical_logic_cone_bound = False
            elif measurement == "endpoint-longest-path-fallback":
                physical_logic_model = (
                    "routed-endpoint-longest-path-conservative"
                )
                physical_logic_exact = False
                physical_logic_cone_bound = True
            else:
                raise ValidationError(
                    "physical local path measurement is invalid"
                )
            records.append(
                {
                    "path": path_id,
                    "representative_path": path_id,
                    "path_scope": "same-fpga-local",
                    "clock_domain": item["clock_domain"],
                    "target_period_ns": period,
                    "target_required_time_ns": required_time,
                    "source_required_time_adjustment_ns": (
                        period - required_time
                    ),
                    "virtual_period_ns": virtual_period,
                    "runtime_required_time_ns": (
                        virtual_period - (period - required_time)
                    ),
                    "logical_fpga_sequence": [fpga],
                    "logical_cut_transitions": [],
                    "routed_hops": [],
                    "preplacement_fixed_delay_ns": None,
                    "physical_logic_delay_bound_ns": delay,
                    "physical_interface_delay_bound_ns": 0.0,
                    "physical_interface_model": "not-applicable-local-path",
                    "physical_logic_model": physical_logic_model,
                    "physical_logic_member_path": path_id,
                    "physical_routed_stage_delay_bound_ns": delay,
                    "scheduled_link_tdm_delay_ns": 0.0,
                    "scheduled_link_tdm_model_delay_ns": 0.0,
                    "scheduled_link_tdm_model": "not-applicable-local-path",
                    "system_delay_bound_ns": delay,
                    "target_clock_slack_bound_ns": required_time - delay,
                    "runtime_clock_slack_bound_ns": (
                        virtual_period - (period - required_time) - delay
                    ),
                    "partition_chain_exact": True,
                    "physical_logic_segments_exact": physical_logic_exact,
                    "physical_logic_segments_cone_bound": (
                        physical_logic_cone_bound
                    ),
                }
            )
    records.sort(key=lambda item: item["path"])
    if source is not None:
        # The certificate binds the concrete routes artifact byte-for-byte.
        # A Mapping cannot recover that identity: managed nodes may choose
        # compact JSON while direct runs use pretty JSON for the same value.
        # Require the digest captured while reading the actual input file.
        if routes_artifact_sha256 is None:
            raise ValidationError(
                "physical local path timing requires the concrete route "
                "artifact digest"
            )
        if routes_artifact_sha256 != source["routes_sha256"]:
            raise ValidationError(
                "physical local path timing is bound to another route artifact"
            )
    return records, source


def build_system_timing(
    runtime: Mapping[str, Any],
    routes: Mapping[str, Any],
    schedule: Mapping[str, Any],
    phase5_report: Mapping[str, Any],
    physical_summary: Mapping[str, Any],
    platform: Platform,
    *,
    routes_artifact_sha256: str | None = None,
) -> Dict[str, Any]:
    """Compose P&R delay and concrete TDM/link delay on every STA path.

    The schedule/link component is path-exact for the Phase-4 routed sink
    selected by the timing model. Routed endpoint measurements are used when
    the physical backend provides complete BoundaryTimingDB coverage. Exact
    logic segments replace the TX endpoint measurements they subsume; the
    remaining RX/interface stages and the measured logic-stage chain are then
    composed without assuming that either component is a separable pure-logic
    delay. Backends without complete segment coverage retain conservative
    per-partition post-route maxima.
    """
    records = reconstruct_tdm_schedule_timing_paths(
        routes, platform, schedule
    )
    if not records:
        raise ValidationError("system timing has no cross-FPGA timing paths")
    reported = phase5_report.get("timing_validation")
    if not isinstance(reported, dict) or reported.get("status") != "pass":
        raise ValidationError("Phase 5 has no passing scheduled timing")
    reconstructed_worst = min(
        records, key=lambda record: (record["normalized_slack"], record["path"])
    )
    for field, expected in (
        ("worst_delay_ns", reconstructed_worst["delay_ns"]),
        ("worst_slack_ns", reconstructed_worst["slack_ns"]),
    ):
        actual = _finite_number(reported.get(field), f"Phase 5 {field}")
        if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1.0e-8):
            raise ValidationError(
                f"Phase 5 {field} does not match schedule reconstruction"
            )

    delays = _physical_delay_database(physical_summary)
    endpoint_delays = _endpoint_delay_database(physical_summary)
    logic_segments = _logic_segment_database(physical_summary)
    board_link_delays = _board_link_delay_database(
        physical_summary, platform
    )
    exact_deadlines = None
    from .tdm import is_sampled_virtual_wire_schedule

    if is_sampled_virtual_wire_schedule(schedule):
        semantic_contract = routes.get("semantic_contract")
        if not isinstance(semantic_contract, dict):
            raise ValidationError(
                "sampled virtual-wire timing requires the routed semantic "
                "contract"
            )
        exact_deadlines = build_static_exact_segment_deadlines(
            schedule, semantic_contract, physical_summary, platform
        )
        validate_static_exact_segment_deadlines(
            exact_deadlines,
            schedule,
            semantic_contract,
            physical_summary,
            platform,
        )
    sampled_schedule = exact_deadlines is not None
    sampled_uncertainty_ns = _finite_number(
        physical_summary.get("static_exact_clock_uncertainty_ns", 0.0),
        "sampled virtual-wire clock uncertainty",
    )
    if sampled_uncertainty_ns < 0.0:
        raise ValidationError(
            "sampled virtual-wire clock uncertainty must be non-negative"
        )
    schedule_entries = {
        item["id"]: item for item in schedule.get("entries", [])
    }
    routes_by_net = {
        item["net"]: item for item in routes.get("routes", [])
    }
    expected_fpgas = {fpga.id for fpga in platform.fpgas}
    if set(delays) != expected_fpgas:
        raise ValidationError(
            "system timing physical delay database does not cover BoardDB"
        )
    virtual_period = _finite_number(
        runtime["virtual_dut_clock"]["nominal_period_ns"],
        "runtime virtual period",
    )
    cross_paths = []
    discontinuous_paths = 0
    exact_logic_paths = 0
    cone_bound_logic_paths = 0
    measured_logic_paths = 0
    sampled_event_paths = 0
    sampled_event_failures = 0
    for record in records:
        transitions = record["cut_transitions"]
        partitions, discontinuities = _logic_partition_sequence(transitions)
        unknown = sorted(set(partitions) - set(delays))
        if unknown:
            raise ValidationError(
                f"system timing path {record['path']} uses unknown FPGAs "
                f"{unknown}"
            )
        local_delay = sum(delays[fpga]["dut"] for fpga in partitions)
        scheduled_hops = record["scheduled_hops"]
        if endpoint_delays is None:
            interface_delay = sum(
                delays[hop["from"]]["cross"]
                + delays[hop["to"]]["cross"]
                for hop in scheduled_hops
            )
            interface_model = "per-partition-interface-maxima-upper-bound"
        else:
            endpoint_ids = [
                endpoint_id
                for hop in scheduled_hops
                for endpoint_id in (
                    hop["tx_endpoint"],
                    hop["rx_endpoint"],
                )
            ]
            missing = sorted(set(endpoint_ids) - set(endpoint_delays))
            if missing:
                raise ValidationError(
                    f"system timing path {record['path']} lacks endpoint "
                    f"timing for {missing[:10]}"
                )
            interface_delay = sum(
                endpoint_delays[endpoint_id] for endpoint_id in endpoint_ids
            )
            interface_model = "routed-endpoint-exact"
        member_ids = record.get(
            "compressed_path_ids", [record["path"]]
        )
        logic_model = "per-partition-maximum-upper-bound"
        member_physical = None
        if logic_segments is not None and endpoint_delays is not None:
            member_segments = logic_segments.get(record["path"], {})
            candidates = []
            for member in member_ids:
                segments = member_segments.get(member, [])
                roles = [segment["kind"] for segment in segments]
                cut_indices = [segment["cut_index"] for segment in segments]
                if (
                    len(segments) != len(record["cut_nets"]) + 1
                    or roles.count("launch") != 1
                    or roles.count("capture") != 1
                    or roles.count("transition")
                    != len(record["cut_nets"]) - 1
                    or sorted(cut_indices)
                    != list(range(len(record["cut_nets"]) + 1))
                ):
                    candidates = []
                    break
                replacements = [
                    segment["replace_tx_endpoint"]
                    for segment in segments
                    if segment["replace_tx_endpoint"] is not None
                ]
                if (
                    len(replacements) != len(record["cut_nets"])
                    or len(replacements) != len(set(replacements))
                    or any(item not in endpoint_delays for item in replacements)
                ):
                    raise ValidationError(
                        f"system timing path {record['path']} logic segment "
                        "replacement coverage is invalid"
                    )
                replaced_interface = sum(
                    endpoint_delays[item] for item in replacements
                )
                unreplaced_interface = interface_delay - replaced_interface
                if unreplaced_interface < -1.0e-8:
                    raise ValidationError(
                        f"system timing path {record['path']} has invalid "
                        "endpoint replacement accounting"
                    )
                unreplaced_interface = max(0.0, unreplaced_interface)
                segment_delay = sum(
                    float(segment["delay_ns"]) for segment in segments
                )
                cone_bound_segments = sum(
                    segment.get("measurement", "endpoint-exact")
                    == "cut-net-cone-upper-bound"
                    for segment in segments
                )
                composite = unreplaced_interface + segment_delay
                candidates.append(
                    (
                        composite,
                        member,
                        segment_delay,
                        unreplaced_interface,
                        cone_bound_segments,
                    )
                )
            if candidates and len(candidates) == len(member_ids):
                member_physical = {
                    member: {
                        "physical_delay": composite,
                        "local_delay": segment_delay,
                        "interface_delay": unreplaced_interface,
                        "cone_bound_segments": cone_bound_segments,
                        "segments": sorted(
                            member_segments[member],
                            key=lambda item: item["cut_index"],
                        ),
                    }
                    for (
                        composite,
                        member,
                        segment_delay,
                        unreplaced_interface,
                        cone_bound_segments,
                    ) in candidates
                }
        if member_physical is None:
            clockless = [
                fpga for fpga in partitions if not delays[fpga]["dut_present"]
            ]
            if clockless:
                raise ValidationError(
                    f"system timing path {record['path']} crosses DUT-clockless "
                    f"partitions without complete routed logic segments: {clockless}"
                )
        transport_delay = record["transport_delay_ns"]
        transport_model = "phase5-boarddb-model"
        if board_link_delays is not None:
            transport_delay = 0.0
            for hop in scheduled_hops:
                key = (hop["link"], hop["from"], hop["to"])
                transport_delay += (
                    board_link_delays[key]
                    + hop["tdm_wait_slots"] * hop["tdm_slot_ns"]
                )
            transport_model = "board-link-timing-db"
        target_period = record["clock_period_ns"]
        target_required_time = record.get("required_time_ns", target_period)
        required_time_adjustment = target_period - target_required_time
        runtime_required_time = virtual_period - required_time_adjustment
        for member in member_ids:
            member_local_delay = local_delay
            member_interface_delay = interface_delay
            member_physical_delay = local_delay + interface_delay
            member_logic_model = logic_model
            member_interface_model = interface_model
            logic_exact = False
            cone_bound = False
            if member_physical is not None:
                measurement = member_physical[member]
                member_local_delay = measurement["local_delay"]
                member_interface_delay = measurement["interface_delay"]
                member_physical_delay = measurement["physical_delay"]
                cone_bound = measurement["cone_bound_segments"] > 0
                member_interface_model = (
                    "routed-endpoint-exact-unreplaced-stages"
                )
                measured_logic_paths += 1
                if cone_bound:
                    member_logic_model = (
                        "routed-staging-chain-cone-upper-bound"
                    )
                    cone_bound_logic_paths += 1
                else:
                    member_logic_model = "routed-staging-chain-exact"
                    logic_exact = True
                    exact_logic_paths += 1
            sampled_event = None
            if sampled_schedule and member_physical is not None:
                assert endpoint_delays is not None
                sampled_event = _sampled_virtual_wire_path_delay(
                    record,
                    member,
                    measurement["segments"],
                    endpoint_delays,
                    board_link_delays,
                    schedule_entries,
                    routes_by_net,
                    commit_slot=exact_deadlines["commit_slot"],
                    uncertainty_ns=sampled_uncertainty_ns,
                )
                member_local_delay = sampled_event[
                    "physical_logic_delay_bound_ns"
                ]
                member_interface_delay = sampled_event[
                    "physical_interface_delay_bound_ns"
                ]
                member_physical_delay = sampled_event[
                    "physical_routed_stage_delay_bound_ns"
                ]
                transport_delay = sampled_event[
                    "scheduled_link_tdm_delay_ns"
                ]
                transport_model = "sampled-virtual-wire-event-propagation"
                total_delay = sampled_event["system_delay_bound_ns"]
                sampled_event_paths += 1
                sampled_event_failures += int(
                    sampled_event["sampled_event_timing_status"] == "fail"
                )
            else:
                total_delay = member_physical_delay + transport_delay
            path_record = {
                "path": member,
                "representative_path": record["path"],
                "path_scope": "cross-fpga",
                "clock_domain": record["clock_domain"],
                "target_period_ns": target_period,
                "target_required_time_ns": target_required_time,
                "source_required_time_adjustment_ns": required_time_adjustment,
                "virtual_period_ns": virtual_period,
                "runtime_required_time_ns": runtime_required_time,
                "logical_fpga_sequence": partitions,
                "logical_cut_transitions": transitions,
                "routed_hops": record["routed_hops"],
                "preplacement_fixed_delay_ns": record[
                    "preplacement_fixed_delay_ns"
                ],
                "physical_logic_delay_bound_ns": member_local_delay,
                "physical_interface_delay_bound_ns": member_interface_delay,
                "physical_interface_model": member_interface_model,
                "physical_logic_model": member_logic_model,
                "physical_logic_member_path": (
                    member if member_physical is not None else None
                ),
                "physical_routed_stage_delay_bound_ns": member_physical_delay,
                "scheduled_link_tdm_delay_ns": transport_delay,
                "scheduled_link_tdm_model_delay_ns": record[
                    "transport_delay_ns"
                ],
                "scheduled_link_tdm_model": transport_model,
                "system_delay_bound_ns": total_delay,
                "target_clock_slack_bound_ns": (
                    target_required_time - total_delay
                ),
                "runtime_clock_slack_bound_ns": (
                    runtime_required_time - total_delay
                ),
                "partition_chain_exact": discontinuities == 0,
                "physical_logic_segments_exact": logic_exact,
                "physical_logic_segments_cone_bound": cone_bound,
            }
            if sampled_event is not None:
                path_record.update(
                    {
                        "sampled_event_timing_status": sampled_event[
                            "sampled_event_timing_status"
                        ],
                        "minimum_tx_readiness_slack_ns": sampled_event[
                            "minimum_tx_readiness_slack_ns"
                        ],
                        "capture_commit_slack_ns": sampled_event[
                            "capture_commit_slack_ns"
                        ],
                        "clock_uncertainty_ns": sampled_event[
                            "clock_uncertainty_ns"
                        ],
                        "board_delay_bound_ns": sampled_event[
                            "board_delay_bound_ns"
                        ],
                    }
                )
            cross_paths.append(path_record)
        discontinuous_paths += (discontinuities > 0) * len(member_ids)

    local_paths, original_source = _local_path_database(
        physical_summary, virtual_period, routes_artifact_sha256
    )
    system_paths = sorted(
        [*local_paths, *cross_paths], key=lambda item: item["path"]
    )
    local_exact_logic_paths = sum(
        bool(path["physical_logic_segments_exact"]) for path in local_paths
    )
    local_cone_bound_logic_paths = sum(
        bool(path["physical_logic_segments_cone_bound"])
        for path in local_paths
    )
    whole_exact_logic_paths = exact_logic_paths + local_exact_logic_paths
    whole_measured_logic_paths = measured_logic_paths + len(local_paths)
    whole_cone_bound_logic_paths = (
        cone_bound_logic_paths + local_cone_bound_logic_paths
    )
    whole_logic_paths = len(system_paths)
    system_ids = [path["path"] for path in system_paths]
    if len(system_ids) != len(set(system_ids)):
        raise ValidationError(
            "same-FPGA and cross-FPGA timing path populations overlap"
        )
    if original_source is not None:
        if (
            len(system_ids) != original_source["original_paths"]
            or path_id_set_sha256(system_ids)
            != original_source["original_path_ids_sha256"]
        ):
            raise ValidationError(
                "whole-design timing does not exactly cover the original "
                "TimingPathDB path population"
            )
        timing_scope = "whole-original-design"
        coverage = 1.0
    else:
        timing_scope = "cross-fpga-path-subset"
        coverage = None

    target_worst = min(
        system_paths,
        key=lambda path: (path["target_clock_slack_bound_ns"], path["path"]),
    )
    runtime_worst = min(
        system_paths,
        key=lambda path: (path["runtime_clock_slack_bound_ns"], path["path"]),
    )
    target_tns = sum(
        min(0.0, path["target_clock_slack_bound_ns"])
        for path in system_paths
    )
    runtime_tns = sum(
        min(0.0, path["runtime_clock_slack_bound_ns"])
        for path in system_paths
    )
    maximum_delay = max(
        path["system_delay_bound_ns"] for path in system_paths
    )
    runtime_wns = runtime_worst["runtime_clock_slack_bound_ns"]
    status = "pass" if runtime_wns >= 0.0 else "fail"
    if exact_deadlines is not None:
        if exact_deadlines["status"] == "incomplete":
            status = "incomplete"
        elif exact_deadlines["status"] == "fail":
            status = "fail"
        elif sampled_event_paths != len(cross_paths):
            status = "incomplete"
        elif sampled_event_failures:
            status = "fail"
    if (
        sampled_schedule
        and sampled_event_paths == len(cross_paths)
        and whole_exact_logic_paths == whole_logic_paths
    ):
        qualification = "sampled-virtual-wire-event-propagated-physical"
    elif (
        sampled_schedule
        and sampled_event_paths == len(cross_paths)
        and whole_measured_logic_paths == whole_logic_paths
    ):
        qualification = (
            "sampled-virtual-wire-event-propagated-physical-bounds"
        )
    elif whole_exact_logic_paths == whole_logic_paths:
        qualification = "staging-aware-physical-plus-concrete-link-tdm"
    elif whole_measured_logic_paths == whole_logic_paths:
        qualification = (
            "staging-aware-routed-physical-bounds-plus-concrete-link-tdm"
        )
    elif whole_measured_logic_paths > 0:
        qualification = (
            "hybrid-staging-aware-and-partition-maxima-plus-concrete-link-tdm"
        )
    elif endpoint_delays is not None:
        qualification = (
            "partition-logic-maxima-plus-endpoint-exact-interface-plus-"
            "concrete-link-tdm"
        )
    else:
        qualification = (
            "conservative-partition-physical-maxima-plus-concrete-link-tdm"
        )
    return {
        "schema": SYSTEM_TIMING_SCHEMA,
        "status": status,
        "design": runtime["design"],
        "platform": platform.name,
        "qualification": qualification,
        "timing_scope": timing_scope,
        "path_exactness": {
            "scheduled_link_tdm": True,
            "sampled_virtual_wire_event_propagation": (
                sampled_event_paths == len(cross_paths)
                if sampled_schedule
                else None
            ),
            "physical_boundary_endpoints": endpoint_delays is not None,
            "physical_logic_segments": (
                whole_exact_logic_paths == whole_logic_paths
            ),
            "physical_logic_segment_bounds": (
                whole_measured_logic_paths == whole_logic_paths
            ),
            "physical_logic_segments_endpoint_exact": (
                whole_exact_logic_paths == whole_logic_paths
            ),
            "physical_model": (
                "routed-staging-chain-exact"
                if whole_exact_logic_paths == whole_logic_paths
                else (
                    "routed-staging-chain-upper-bounds"
                    if whole_measured_logic_paths == whole_logic_paths
                    else (
                        "hybrid-routed-staging-chain-and-partition-maxima"
                        if whole_measured_logic_paths > 0
                        else "per-partition-and-interface-maxima-upper-bound"
                    )
                )
            ),
            "endpoint_exact_logic_paths": whole_exact_logic_paths,
            "cone_bound_logic_paths": whole_cone_bound_logic_paths,
            "fallback_logic_paths": (
                whole_logic_paths - whole_measured_logic_paths
            ),
            "local_original_paths_endpoint_exact": (
                local_exact_logic_paths
            ),
            "local_original_paths_cone_bound": (
                local_cone_bound_logic_paths
            ),
            "discontinuous_compressed_paths": discontinuous_paths,
            "sampled_event_paths": sampled_event_paths,
            "sampled_event_failures": sampled_event_failures,
        },
        "physical_source": {
            "provider": physical_summary.get("provider"),
            "qualification": physical_summary.get("qualification"),
        },
        **(
            {"static_exact_segment_deadlines": exact_deadlines}
            if exact_deadlines is not None
            else {}
        ),
        # Preserve the byte-level identity of the original whole-design
        # timing population and every artifact used to classify it as local
        # or cross-FPGA.  Final A/B qualification cross-checks this binding
        # against the concrete artifacts in each flow arm.
        "source_binding": original_source,
        "target_clock": {
            "closure_gate": False,
            "worst_path": target_worst["path"],
            "worst_slack_bound_ns": target_worst[
                "target_clock_slack_bound_ns"
            ],
            "negative_slack_paths": sum(
                path["target_clock_slack_bound_ns"] < 0.0
                for path in system_paths
            ),
            "total_negative_slack_bound_ns": target_tns,
            "tns_bound_ns": target_tns,
        },
        "runtime_clock": {
            "closure_gate": True,
            "period_ns": virtual_period,
            "frequency_mhz": 1000.0 / virtual_period,
            "worst_path": runtime_worst["path"],
            "worst_slack_bound_ns": runtime_wns,
            "negative_slack_paths": sum(
                path["runtime_clock_slack_bound_ns"] < 0.0
                for path in system_paths
            ),
            "total_negative_slack_bound_ns": runtime_tns,
            "tns_bound_ns": runtime_tns,
            "minimum_safe_period_bound_ns": maximum_delay,
            "maximum_safe_frequency_bound_mhz": 1000.0 / maximum_delay,
        },
        "summary": {
            "timing_paths": len(system_paths),
            "original_paths": (
                original_source["original_paths"]
                if original_source is not None
                else None
            ),
            "original_local_paths": len(local_paths),
            "original_cross_fpga_paths": len(cross_paths),
            "compressed_representative_paths": len(records),
            "original_path_coverage": coverage,
            "original_path_ids_sha256": (
                original_source["original_path_ids_sha256"]
                if original_source is not None
                else None
            ),
            "maximum_system_delay_bound_ns": maximum_delay,
        },
        "paths": system_paths,
    }
