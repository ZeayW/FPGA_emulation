"""Native timing-path-guided refinement of a concrete TDM slot schedule."""

from __future__ import annotations

import copy
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .errors import EmuFlowError, ValidationError
from .native_tools import resolve_native_executable
from .platform import Platform


TDM_SLOT_OPTIMIZER_PROVIDER = "timing-path-guided-lns-v2"
HopKey = Tuple[str, str, str, str]


def _hop_key(
    demand: str, link: str, source: str, sink: str
) -> HopKey:
    return demand, link, source, sink


def _write_native_input(
    path: Path,
    routes: Mapping[str, Any],
    platform: Platform,
    ratio_plan: Mapping[str, Any],
    schedule: Mapping[str, Any],
    *,
    max_iterations: int,
) -> None:
    from .tdm import (
        COMBINATIONAL_SETTLE_SLOTS,
        RUNTIME_BARRIER_SLOTS,
        _route_hops,
        sampled_logic_segment_budget_slots,
    )

    entries = {
        _hop_key(
            entry["demand"],
            entry["link"],
            entry["from"],
            entry["to"],
        ): entry
        for entry in schedule["entries"]
    }
    exact_contract = routes.get("semantic_contract")
    timing_constraints = schedule.get("timing_constraints")
    if exact_contract is not None and not isinstance(timing_constraints, dict):
        raise ValidationError(
            "sampled virtual-wire schedule lacks Phase-5 timing constraints"
        )
    plan_by_key = {
        _hop_key(
            hop["demand"], hop["link"], hop["from"], hop["to"]
        ): hop
        for hop in ratio_plan["hops"]
    }
    priority_keys = sorted(
        entries,
        key=lambda key: (
            plan_by_key[key]["transport_round"],
            entries[key]["slot"],
            entries[key]["id"],
        ),
    )
    priority = {key: index for index, key in enumerate(priority_keys)}
    dependencies = []
    release_by_index: Dict[int, int] = {
        hop["index"]: 0 for hop in ratio_plan["hops"]
    }
    sink_records = []
    incoming_by_net_fpga: Dict[Tuple[str, str], int] = {}
    first_hops_by_net: Dict[str, list[int]] = {}
    for route_index, route in enumerate(
        sorted(routes["routes"], key=lambda item: item["id"])
    ):
        incoming = {}
        first_hops = []
        for _depth, edge in _route_hops(route):
            key = _hop_key(
                route["id"], edge["link"], edge["from"], edge["to"]
            )
            hop = plan_by_key[key]
            parent = incoming.get(edge["from"])
            if parent is None:
                first_hops.append(hop["index"])
            else:
                dependencies.append(
                    (parent, hop["index"], COMBINATIONAL_SETTLE_SLOTS)
                )
            incoming[edge["to"]] = hop["index"]
            incoming_by_net_fpga[(route["net"], edge["to"])] = hop["index"]
        first_hops_by_net[route["net"]] = first_hops
        for sink in route["sinks"]:
            sink_records.append(
                (
                    route_index,
                    0
                    if exact_contract is not None
                    else route.get("transport_round", 0),
                    incoming[sink],
                )
            )

    if exact_contract is not None:
        nodes = {
            item["net"]: item for item in exact_contract["cut_nodes"]
        }
        segments = {
            item["id"]: item for item in exact_contract["logic_segments"]
        }
        for net, node in sorted(nodes.items()):
            roots = first_hops_by_net.get(net, [])
            if not roots:
                raise ValidationError(
                    f"cross-layer timing cut {net!r} has no source hop"
                )
            for segment_id in node["source_segment_ids"]:
                segment = segments[segment_id]
                budget = sampled_logic_segment_budget_slots(
                    segment, timing_constraints
                )
                if segment["kind"] == "launch_to_tx":
                    for child in roots:
                        release_by_index[child] = max(
                            release_by_index[child], budget
                        )
                elif segment["kind"] == "rx_to_tx":
                    predecessor = incoming_by_net_fpga.get(
                        (
                            segment["source_cut_net"],
                            node["source_fpgas"][0],
                        )
                    )
                    if predecessor is None:
                        raise ValidationError(
                            "cross-layer timing predecessor has no routed "
                            "arrival hop"
                        )
                    for child in roots:
                        dependencies.append((predecessor, child, budget))
                else:
                    raise ValidationError(
                        "cross-layer source segment has unsupported semantics"
                    )

    normalization = ratio_plan["normalization"]
    frame_slots = schedule["route_constraints"]["frame_slots"]
    realization = schedule.get("round_barrier_realization")
    if not isinstance(realization, dict):
        raise ValidationError(
            "academic TDM schedule has no round-barrier realization"
        )
    planned_ready = realization.get("source_ready_slot")
    lines = [
        "EMUFLOW_TDM_SLOT_INPUT_V3",
        (
            f"PARAM {frame_slots} {RUNTIME_BARRIER_SLOTS} "
            f"{COMBINATIONAL_SETTLE_SLOTS} {max_iterations} "
            f"{planned_ready if planned_ready is not None else -1} "
            f"{normalization['positive_slack_scale_ns']:.17g} "
            f"{normalization['negative_slack_scale_ns']:.17g} "
            f"{normalization['max_clock_period_ns']:.17g}"
        ),
    ]
    link_by_id = {link.id: link for link in platform.links}
    for hop in ratio_plan["hops"]:
        key = _hop_key(
            hop["demand"], hop["link"], hop["from"], hop["to"]
        )
        lines.append(
            f"HOP {hop['index']} "
            f"{0 if exact_contract is not None else hop['transport_round']} "
            f"{hop['domain']} {hop['lane']} {hop['discrete_ratio']} "
            f"{link_by_id[hop['link']].latency_cycles} "
            f"{release_by_index[hop['index']]} {priority[key]} "
            f"{hop['base_delay_ns']:.17g} {hop['beta_ns']:.17g}"
        )
    dependency_delays = {}
    for parent, child, delay in dependencies:
        dependency_delays[(parent, child)] = max(
            dependency_delays.get((parent, child), 0), delay
        )
    for (parent, child), delay in sorted(dependency_delays.items()):
        lines.append(f"DEP {parent} {child} {delay}")
    for route, transport_round, hop in sink_records:
        lines.append(f"SINK {route} {transport_round} {hop}")
    for timing_path in ratio_plan["timing_paths"]:
        hops = ",".join(str(hop) for hop in timing_path["hops"])
        lines.append(
            f"PATH {timing_path['index']} "
            f"{timing_path['clock_period_ns']:.17g} "
            f"{timing_path.get('required_time_ns', timing_path['clock_period_ns']):.17g} "
            f"{timing_path['fixed_delay_ns']:.17g} {hops or '-'}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_native_output(
    path: Path, expected_hops: int
) -> Dict[str, Any]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "EMUFLOW_TDM_SLOT_OUTPUT_V1":
        raise EmuFlowError("TDM slot optimizer returned an invalid header")
    hops = {}
    metrics: Dict[str, Any] = {}
    integer_metrics = {
        "iterations",
        "accepted_moves",
        "evaluated_moves",
        "lns_neighborhoods",
        "lns_evaluated_orders",
        "completion_slot",
        "total_wait_slots",
    }
    for line in lines[1:]:
        fields = line.split()
        if len(fields) == 4 and fields[0] == "HOP":
            try:
                index, slot, ready = map(int, fields[1:])
            except ValueError as error:
                raise EmuFlowError(
                    "TDM slot optimizer returned an invalid HOP"
                ) from error
            if index in hops:
                raise EmuFlowError(
                    "TDM slot optimizer returned a duplicate HOP"
                )
            hops[index] = {"slot": slot, "ready_slot": ready}
        elif len(fields) == 3 and fields[0] == "METRIC":
            key = fields[1]
            if key in metrics:
                raise EmuFlowError(
                    f"TDM slot optimizer returned duplicate metric {key!r}"
                )
            try:
                metrics[key] = (
                    int(fields[2])
                    if key in integer_metrics
                    else float(fields[2])
                )
            except ValueError as error:
                raise EmuFlowError(
                    f"TDM slot optimizer metric {key!r} is invalid"
                ) from error
        else:
            raise EmuFlowError(
                f"TDM slot optimizer returned an invalid record: {line}"
            )
    if set(hops) != set(range(expected_hops)):
        raise EmuFlowError("TDM slot optimizer HOP coverage is not exact")
    expected_metrics = {
        "iterations",
        "accepted_moves",
        "evaluated_moves",
        "lns_neighborhoods",
        "lns_evaluated_orders",
        "worst_normalized_slack",
        "completion_slot",
        "total_wait_slots",
    }
    if set(metrics) != expected_metrics:
        raise EmuFlowError("TDM slot optimizer metric coverage is not exact")
    return {"hops": hops, "metrics": metrics}


def _apply_native_schedule(
    routes: Mapping[str, Any],
    platform: Platform,
    ratio_plan: Mapping[str, Any],
    schedule: Mapping[str, Any],
    native: Mapping[str, Any],
    *,
    prepared_ratio_model: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    from .tdm import (
        COMBINATIONAL_SETTLE_SLOTS,
        SAMPLED_VIRTUAL_WIRE_SCHEDULE_CERTIFICATE_SCHEMA,
        TDM_ACADEMIC_SCHEDULE_PROVIDER,
        _exact_capture_certificate,
        _exact_contract_indexes,
        _exact_source_readiness,
        _domain_schedule_records,
        _round_barrier_realization,
        _round_order,
        _route_hops,
        reconstruct_tdm_schedule_timing,
        validate_tdm_schedule,
    )

    refined = copy.deepcopy(schedule)
    link_by_id = {link.id: link for link in platform.links}
    entries_by_hop = {}
    for entry in refined["entries"]:
        index = entry["ratio_plan_hop"]
        optimized = native["hops"][index]
        entry["slot"] = optimized["slot"]
        entry["ready_slot"] = optimized["ready_slot"]
        entry["arrival_slot"] = (
            optimized["slot"] + link_by_id[entry["link"]].latency_cycles
        )
        entry["ratio_wait_slots"] = (
            optimized["slot"] - optimized["ready_slot"]
        )
        entries_by_hop[
            _hop_key(
                entry["demand"],
                entry["link"],
                entry["from"],
                entry["to"],
            )
        ] = entry

    completion_by_round: Dict[int, int] = {}
    completions = []
    exact_contract = routes.get("semantic_contract")
    exact_arrivals = {}
    exact_readiness = []
    if exact_contract is None:
        ordered_routes, _active_rounds = _round_order(routes["routes"])
        exact_nodes = exact_segments = exact_captures = None
    else:
        exact_nodes, exact_segments, exact_captures = (
            _exact_contract_indexes(exact_contract)
        )
        route_by_net = {item["net"]: item for item in routes["routes"]}
        ordered_routes = [
            route_by_net[node["net"]]
            for node in sorted(
                exact_nodes.values(),
                key=lambda item: (item["dependency_level"], item["net"]),
            )
        ]
        _active_rounds = [0]
    for route in ordered_routes:
        transport_round = route.get("transport_round", 0)
        if exact_contract is None:
            source_ready = max(
                (
                    completion + COMBINATIONAL_SETTLE_SLOTS
                    for prior_round, completion in completion_by_round.items()
                    if prior_round < transport_round
                ),
                default=0,
            )
        else:
            source_ready, evidence = _exact_source_readiness(
                exact_nodes[route["net"]],
                exact_segments,
                exact_arrivals,
                refined["timing_constraints"],
            )
            exact_readiness.append(
                {
                    "demand": route["id"],
                    "net": route["net"],
                    "source": route["source"],
                    "source_ready_slot": source_ready,
                    "evidence": evidence,
                }
            )
        arrival_by_node = {route["source"]: source_ready - 1}
        for _depth, edge in _route_hops(route):
            entry = entries_by_hop[
                _hop_key(
                    route["id"],
                    edge["link"],
                    edge["from"],
                    edge["to"],
                )
            ]
            arrival_by_node[edge["to"]] = entry["arrival_slot"]
        completion = max(arrival_by_node[sink] for sink in route["sinks"])
        completion_by_round[transport_round] = max(
            completion,
            completion_by_round.get(transport_round, completion),
        )
        completion_record = {
            "demand": route["id"],
            "net": route["net"],
            "transport_round": transport_round,
            "source_ready_slot": source_ready,
            "completion_slot": completion,
        }
        if exact_contract is not None:
            sink_arrivals = {
                sink: arrival_by_node[sink] for sink in sorted(route["sinks"])
            }
            completion_record["sink_arrival_slots"] = sink_arrivals
            for sink, arrival in sink_arrivals.items():
                exact_arrivals[(route["net"], sink)] = arrival
        completions.append(completion_record)

    refined["entries"].sort(
        key=lambda entry: (
            entry["slot"],
            entry["capacity_key"],
            entry["lane"],
            entry["demand"],
        )
    )
    refined["demand_completions"] = sorted(
        completions, key=lambda item: item["demand"]
    )
    refined["round_barrier_realization"] = _round_barrier_realization(
        _active_rounds,
        completion_by_round,
        ratio_plan["round_barrier_legalization"].get(
            "source_ready_slot"
        ),
    )
    refined["domain_schedules"] = _domain_schedule_records(
        platform,
        refined["route_constraints"],
        refined["entries"],
    )
    refined["metrics"]["completion_slot"] = max(
        completion_by_round.values()
    )
    refined["metrics"]["maximum_ratio_wait_slots"] = max(
        entry["ratio_wait_slots"] for entry in refined["entries"]
    )
    if exact_contract is not None:
        captures, minimum_capture_slack = _exact_capture_certificate(
            exact_segments,
            exact_captures,
            exact_arrivals,
            refined["timing_constraints"],
        )
        refined["schedule_dependency_certificate"] = {
            "schema": SAMPLED_VIRTUAL_WIRE_SCHEDULE_CERTIFICATE_SCHEMA,
            "provider": "independent-readiness-certificate-v1",
            "topological_cut_order": [
                route["net"] for route in ordered_routes
            ],
            "demand_readiness": exact_readiness,
            "capture_readiness": captures,
            "minimum_capture_slack_slots": minimum_capture_slack,
        }
        refined["metrics"].update(
            {
                "commit_slot": refined["timing_constraints"]["commit_slot"],
                "dependency_edges": len(exact_contract["dependency_edges"]),
                "maximum_combinational_dependency_depth": exact_contract[
                    "metrics"
                ]["maximum_combinational_dependency_depth"],
                "capture_requirements": len(captures),
                "minimum_capture_slack_slots": minimum_capture_slack,
            }
        )
    baseline_timing = reconstruct_tdm_schedule_timing(
        routes, platform, schedule, model=prepared_ratio_model
    )
    refined["provider"] = TDM_ACADEMIC_SCHEDULE_PROVIDER
    refined["slot_optimization"] = {
        "provider": TDM_SLOT_OPTIMIZER_PROVIDER,
        "configuration": {
            "max_iterations": native["configuration"]["max_iterations"]
        },
        "metrics": {
            **native["metrics"],
            "baseline_worst_normalized_slack": baseline_timing[
                "worst_normalized_slack"
            ],
        },
    }
    validate_tdm_schedule(
        routes,
        platform,
        refined,
        ratio_plan,
        prepared_ratio_model=prepared_ratio_model,
    )
    timing = reconstruct_tdm_schedule_timing(
        routes, platform, refined, model=prepared_ratio_model
    )
    metrics = native["metrics"]
    if (
        not math.isclose(
            metrics["worst_normalized_slack"],
            timing["worst_normalized_slack"],
            rel_tol=1.0e-10,
            abs_tol=1.0e-10,
        )
        or metrics["completion_slot"]
        != refined["metrics"]["completion_slot"]
        or metrics["total_wait_slots"]
        != sum(entry["ratio_wait_slots"] for entry in refined["entries"])
    ):
        raise ValidationError(
            "TDM slot optimizer metrics do not match independent "
            "schedule reconstruction"
        )
    return refined


def refine_tdm_schedule_native(
    routes: Mapping[str, Any],
    platform: Platform,
    ratio_plan: Mapping[str, Any],
    schedule: Mapping[str, Any],
    *,
    executable: Optional[str] = None,
    max_iterations: int = 200,
    prepared_ratio_model: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Run the in-tree C++ path-guided slot local-search engine."""
    if (
        isinstance(max_iterations, bool)
        or not isinstance(max_iterations, int)
        or max_iterations < 0
    ):
        raise ValidationError(
            "TDM slot max_iterations must be a non-negative integer"
        )
    from .tdm import validate_tdm_schedule

    validate_tdm_schedule(
        routes,
        platform,
        schedule,
        ratio_plan,
        prepared_ratio_model=prepared_ratio_model,
    )
    resolved = resolve_native_executable(
        "emuflow_tdm_slot_optimizer", executable
    )
    with tempfile.TemporaryDirectory(prefix="emuflow-tdm-slot-") as temporary:
        root = Path(temporary)
        native_input = root / "tdm-slot.in"
        native_output = root / "tdm-slot.out"
        _write_native_input(
            native_input,
            routes,
            platform,
            ratio_plan,
            schedule,
            max_iterations=max_iterations,
        )
        completed = subprocess.run(
            [resolved, str(native_input), str(native_output)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise EmuFlowError(
                "in-tree TDM slot optimizer failed with exit code "
                f"{completed.returncode}: {detail}"
            )
        native = _parse_native_output(
            native_output, len(ratio_plan["hops"])
        )
    native["configuration"] = {"max_iterations": max_iterations}
    return _apply_native_schedule(
        routes,
        platform,
        ratio_plan,
        schedule,
        native,
        prepared_ratio_model=prepared_ratio_model,
    )
