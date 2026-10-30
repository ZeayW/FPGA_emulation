"""Bounded schedule configuration and the current typed native validation API."""
from __future__ import annotations

from pathlib import Path

import struct

from dataclasses import dataclass

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from emuflow.common.errors import TDMScheduleInfeasibleError, ValidationError

from emuflow.platform.platform import Platform

from emuflow.routing.routing import RUNTIME_BARRIER_SLOTS, build_directed_graph, normalize_route_constraints, route_link_delay_coefficients_ns

TDM_SCHEDULE_SCHEMA = "emuflow.tdm-schedule/v1"

TDM_EXACT_DEPENDENCY_PROVIDER = "deterministic-dependency-earliest-slot-v1"

SAMPLED_VIRTUAL_WIRE_SCHEDULE_CERTIFICATE_SCHEMA = (
    "emuflow.sampled-virtual-wire-schedule-certificate/v1"
)

INDEXED_SAMPLED_VIRTUAL_WIRE_SCHEDULE_CERTIFICATE_SCHEMA = (
    "emuflow.sampled-virtual-wire-schedule-certificate/v2"
)

SAMPLED_VIRTUAL_WIRE_CONSTRAINTS_SCHEMA = (
    "emuflow.sampled-virtual-wire-timing-constraints/v1"
)

COMBINATIONAL_SETTLE_SLOTS = 1

HopKey = Tuple[str, str, str, str]

def sampled_virtual_wire_timing_constraints(
    frame_slots: int,
) -> Dict[str, Any]:
    """Create the Phase-5-owned causal timing policy.

    Phase 3 owns only structural cut/dependency identities.  Slot readiness,
    settle allowance, and the frame commit edge are scheduling policy and are
    therefore materialized here by the unified Phase 5 implementation.
    """

    if isinstance(frame_slots, bool) or not isinstance(frame_slots, int) or frame_slots < 2:
        raise ValidationError("sampled virtual-wire frame must have at least two slots")
    return {
        "schema": SAMPLED_VIRTUAL_WIRE_CONSTRAINTS_SCHEMA,
        "settle_slots": COMBINATIONAL_SETTLE_SLOTS,
        "commit_slot": frame_slots - RUNTIME_BARRIER_SLOTS,
        "slot_edge_convention": "tx-sample-before-rx-shadow-update-v1",
    }

def sampled_logic_segment_budget_slots(
    segment: Mapping[str, Any],
    timing_constraints: Mapping[str, Any],
) -> int:
    if (
        segment.get("kind") == "launch_to_tx"
        and segment.get("source_semantics") == "configuration-stable-constant"
    ):
        return 0
    value = timing_constraints.get("settle_slots")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError("sampled virtual-wire settle slots are invalid")
    return value

def is_sampled_virtual_wire_schedule(schedule: Mapping[str, Any]) -> bool:
    """Return whether transport semantics require readiness/capture checks.

    Solver/provider identity is intentionally irrelevant: any unified Phase 5
    implementation may produce the schedule as long as the semantic contract
    and its independently reconstructible certificate are present.
    """

    return (
        schedule.get("transport_semantics") == "sampled-virtual-wire"
        and isinstance(schedule.get("semantic_contract_sha256"), str)
        and isinstance(schedule.get("schedule_dependency_certificate"), dict)
    )

def is_fixed_slot_transport_schedule(schedule: Mapping[str, Any]) -> bool:
    """Return whether concrete TX events exist for physical readiness checks.

    A declared ratio-delay model owns its target transport delay separately.
    The events still constrain causal TX/RX and commit readiness; legacy
    platforms also use the projected fixed-slot event as the delay objective.
    """

    return schedule.get("transport_semantics") in {
        "registered-boundary",
        "sampled-virtual-wire",
    }

def _static_exact_contract_from_routes(routes):
    contract, digest = routes.get("semantic_contract"), routes.get("semantic_contract_sha256")
    if contract is None and digest is None:
        return None
    if (not isinstance(contract, dict) or contract.get("indexed") is not True
            or contract.get("schema") != "emuflow.static-exact-combinational-cut/v3"
            or contract.get("schema") != routes.get("semantic_contract_schema")
            or contract.get("mode") != "static-exact-combinational"
            or contract.get("qualification") != "structural-partition-legality"
            or not isinstance(digest, str) or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)):
        raise ValidationError("routes indexed exact semantic contract binding is invalid")
    raw = routes.get("routes")
    validate = getattr(raw, "validate_indexed_exact_metadata", None)
    if getattr(raw, "_user_version", None) != 3 or not callable(validate):
        raise ValidationError("exact metadata validation requires the current typed RouteStore")
    validate()
    return contract


def _link_by_id(platform: Platform):
    return {link.id: link for link in platform.links}

def _bank_direction_allocations(
    routes: Mapping[str, Any], platform: Platform,
    constraints: Mapping[str, Any],
) -> Dict[Tuple[str, str, str], Dict[str, int]]:
    """Choose integer bank budgets once for the concrete routed workload.

    The load-based split is a deterministic minimax-ratio heuristic. Native
    scheduling obeys these fixed direction budgets;
    neither is allowed to borrow individual wires from the opposite direction.
    Lane identifiers remain physical, pair-global wire identifiers.
    """
    from emuflow.transport.bank_allocation import allocate_shared_banks

    arc_counts = getattr(routes["routes"], "iter_arc_counts", None)
    if not callable(arc_counts):
        raise ValidationError("bank allocation requires the current typed RouteStore")
    counts = {(link, source, sink): count for link, source, sink, count in arc_counts()}
    result = {}
    for link in platform.links:
        if link.bank_count is None:
            continue
        left, right = sorted(link.endpoints)
        forward = (link.id, left, right)
        reverse = (link.id, right, left)
        allocation = allocate_shared_banks(
            counts.get(forward, 0), counts.get(reverse, 0), link.bank_count,
            link.wires_per_bank,
            min_ratio=int(constraints.get("tdm_min_ratio", 1)),
            ratio_quantum=int(constraints["tdm_ratio_quantum"]),
            max_ratio=int(constraints["tdm_max_ratio"]),
        )
        if allocation is None:
            raise TDMScheduleInfeasibleError(
                f"shared bank allocation is infeasible for link {link.id!r}"
            )
        for key, prefix, offset in (
            (forward, "forward", 0),
            (reverse, "reverse", allocation["forward_lanes"]),
        ):
            result[key] = {
                "bank_count": allocation[f"{prefix}_banks"],
                "wires_per_bank": link.wires_per_bank,
                "lane_offset": offset,
                "lanes": allocation[f"{prefix}_lanes"],
            }
    return result

def _bank_allocation_records(
    allocations: Mapping[Tuple[str, str, str], Mapping[str, int]],
) -> List[Dict[str, Any]]:
    return [
        {"link": key[0], "from": key[1], "to": key[2], **value}
        for key, value in sorted(allocations.items())
    ]

def _write_native_schedule_board_request(
    path: Path, generation: Sequence[int], platform: Platform,
    constraints: Mapping[str, Any], arcs: Mapping[Any, Any],
    bank_allocations: Mapping[Any, Any], fpga_ids: Sequence[str],
    link_ids: Sequence[str], capacity_keys: Sequence[str],
) -> None:
    """Bounded board/configuration request; contains no route or hop records."""
    fpga_index = {name:i for i,name in enumerate(fpga_ids)}
    link_index = {name:i for i,name in enumerate(link_ids)}
    capacity_index = {name:i for i,name in enumerate(capacity_keys)}
    links = _link_by_id(platform)
    with path.open("wb") as stream:
        stream.write(struct.pack("<QI3Q4q6I",0x3142534D44544545,1,*generation,
            constraints["frame_slots"],RUNTIME_BARRIER_SLOTS,COMBINATIONAL_SETTLE_SLOTS,
            int(constraints.get("tdm_min_ratio",1)),constraints["tdm_max_ratio"],constraints["tdm_ratio_quantum"]))
        for catalog in (fpga_ids,link_ids,capacity_keys):
            stream.write(struct.pack("<I",len(catalog)))
            for name in catalog:
                encoded = name.encode("utf-8")
                stream.write(struct.pack("<I",len(encoded))); stream.write(encoded)
        stream.write(struct.pack("<I",len(arcs)))
        for key,arc in sorted(arcs.items()):
            link = links[key[0]]
            budget = bank_allocations.get(key)
            offset = budget["lane_offset"] if budget else 0
            lanes = budget["lanes"] if budget else link.transport_bits_per_cycle_per_direction
            base,slope = route_link_delay_coefficients_ns(platform,*key,constraints)
            stream.write(struct.pack("<8I3d",link_index[key[0]],fpga_index[key[1]],fpga_index[key[2]],
                capacity_index[arc["capacity_key"]],offset,lanes,int(link.tdm_delay_model is not None),
                link.latency_cycles,base,slope,1000.0/link.fabric_clock_mhz))

@dataclass(frozen=True)
class NativeExactScheduleRequest:
    """Bounded producer request, deliberately not a logical schedule view."""

    route_store: Path
    route_generation: tuple[int, ...]
    header: Mapping[str, Any]
    domain_templates: tuple[Mapping[str, Any], ...]
    platform: Platform
    constraints: Mapping[str, Any]
    arcs: Mapping[Any, Any]
    bank_allocations: Mapping[Any, Any]
    catalogs: tuple[tuple[str, ...], ...]
    source_bindings: tuple

    def write_board_request(self, path: Path) -> None:
        _write_native_schedule_board_request(
            path, self.route_generation, self.platform, self.constraints,
            self.arcs, self.bank_allocations, *self.catalogs,
        )

def build_native_exact_schedule_request(
    routes: Mapping[str, Any],
    platform: Platform,
    *,
    source_bindings=None,
) -> Optional[NativeExactScheduleRequest]:
    """Select the complete producer without importing routes or hop results.

    Indexed production uses one shared-core stage; failure is fatal, with no
    alternate native replay or materialized-result retry.
    """

    raw = routes.get("routes")
    if getattr(raw, "_user_version", None) != 3:
        return None
    contract = _static_exact_contract_from_routes(routes)
    if contract is None or contract.get("indexed") is not True:
        return None
    constraints = normalize_route_constraints(routes.get("constraints"), platform)
    _, arcs, capacities = build_directed_graph(platform, constraints)
    allocations = _bank_direction_allocations(routes, platform, constraints)
    frame = constraints["frame_slots"]
    links = _link_by_id(platform)
    templates = []
    for key in sorted(capacities):
        capacity = capacities[key]
        lanes = links[capacity["link"]].transport_bits_per_cycle_per_direction
        templates.append({"key": key, "link": capacity["link"], "direction": capacity["direction"],
                          "lanes": lanes, "frame_slots": frame, "capacity_bit_hops": lanes * frame})
    header = {
        "schema": TDM_SCHEDULE_SCHEMA, "design": routes.get("design"), "platform": platform.name,
        "provider": TDM_EXACT_DEPENDENCY_PROVIDER, "transport_semantics": "sampled-virtual-wire",
        "route_constraints": constraints, "qualification": "dependency-schedule-readiness-pass",
        "timing_constraints": sampled_virtual_wire_timing_constraints(frame),
        "semantic_contract_schema": contract["schema"],
        "semantic_contract_sha256": routes["semantic_contract_sha256"],
    }
    if allocations:
        header["bank_allocations"] = _bank_allocation_records(allocations)
    from emuflow.storage.store_publication import require_store_sources_unchanged

    store = Path(raw.store_path).resolve()
    if source_bindings is None:
        bind_sources = getattr(routes, "bound_store_sources", None)
        if not callable(bind_sources):
            raise ValidationError(
                "indexed Phase 5 input lacks original source bindings"
            )
        source_bindings = bind_sources()
    sources = tuple(source_bindings)
    route_source = next(
        (
            source
            for source in sources
            if source.path == store and source.schema == "system-route-store-v3"
        ),
        None,
    )
    if route_source is None:
        raise ValidationError("indexed Phase 5 route ownership is missing")
    require_store_sources_unchanged(sources)
    return NativeExactScheduleRequest(
        store,
        route_source.generation,
        header,
        tuple(templates),
        platform,
        constraints,
        arcs,
        allocations,
        (
            tuple(sorted(fpga.id for fpga in platform.fpgas)),
            tuple(sorted(links)),
            tuple(sorted(capacities)),
        ),
        sources,
    )

def _validate_indexed_exact_tdm_schedule(
    routes,
    platform,
    schedule,
    exact_contract,
    constraints,
    sampled_timing_constraints,
    arcs,
    links,
    bank_allocations,
) -> Dict[str, Any]:
    """Current indexed validation has one independent native owner."""
    if getattr(routes.get("routes"), "_user_version", None) != 3:
        raise ValidationError(
            "indexed schedule validation requires the current typed RouteStore owner"
        )
    if (
        schedule.get("qualification") != "dependency-schedule-readiness-pass"
        or schedule.get("transport_semantics") != "sampled-virtual-wire"
        or schedule.get("semantic_contract_schema") != exact_contract.get("schema")
        or schedule.get("semantic_contract_sha256")
        != routes.get("semantic_contract_sha256")
    ):
        raise ValidationError("cross-layer timing schedule binding is invalid")
    from emuflow.scheduling.tdm_schedule_validation import validate_native_exact_schedule

    return validate_native_exact_schedule(
        routes, platform, schedule, constraints, arcs, bank_allocations
    )

def validate_tdm_schedule(
    routes: Mapping[str, Any],
    platform: Platform,
    schedule: Mapping[str, Any],
) -> Dict[str, Any]:
    from emuflow.scheduling.tdm_schedule_storage import TDMScheduleView
    if not isinstance(schedule, TDMScheduleView):
        raise ValidationError("schedule validation requires the current typed ScheduleStore; inline schedule replay is retired")
    if schedule.get("provider") != TDM_EXACT_DEPENDENCY_PROVIDER:
        raise ValidationError("unsupported TDM schedule artifact provider")
    exact_contract = _static_exact_contract_from_routes(routes)
    if exact_contract is None:
        raise ValidationError("Static Exact schedule requires an exact dependency contract")
    if schedule.get("schema") != TDM_SCHEDULE_SCHEMA:
        raise ValidationError(
            f"schedule.schema: expected {TDM_SCHEDULE_SCHEMA!r}, "
            f"got {schedule.get('schema')!r}"
        )
    expected_transport_semantics = "sampled-virtual-wire"
    if schedule.get("transport_semantics") != expected_transport_semantics:
        raise ValidationError(
            "schedule.transport_semantics does not match the Phase 3 "
            "timing contract"
        )
    constraints = normalize_route_constraints(
        schedule.get("route_constraints"),
        platform,
    )
    if constraints != normalize_route_constraints(
        routes.get("constraints"), platform
    ):
        raise ValidationError(
            "schedule.route_constraints does not match routes"
        )
    frame_slots = constraints["frame_slots"]
    sampled_timing_constraints = sampled_virtual_wire_timing_constraints(
        frame_slots
    )
    if schedule.get("timing_constraints") != sampled_timing_constraints:
        raise ValidationError(
            "sampled virtual-wire timing constraints do not match Phase 5 policy"
        )
    _, arcs, _ = build_directed_graph(platform, constraints)
    links = _link_by_id(platform)
    bank_allocations = _bank_direction_allocations(routes, platform, constraints)
    if bank_allocations and schedule.get("bank_allocations") != _bank_allocation_records(bank_allocations):
        raise ValidationError("schedule.bank_allocations does not match routed bank allocation")
    return _validate_indexed_exact_tdm_schedule(
        routes, platform, schedule, exact_contract, constraints,
        sampled_timing_constraints, arcs, links, bank_allocations,
    )
