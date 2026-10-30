"""Bounded BoardDB routing policy and current artifact-contract envelopes."""

from __future__ import annotations

import math

from collections import defaultdict, deque

from pathlib import Path

from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from emuflow.common.errors import ValidationError

from emuflow.partition.combinational_cut import STATIC_EXACT_CANDIDATE_POLICIES, STATIC_EXACT_COMBINATIONAL_CUT_SCHEMAS, STATIC_EXACT_STRUCTURAL_CONTRACT_SCHEMA

from emuflow.common.json_io import read_json

from emuflow.partition.partition import PARTITION_ASSIGNMENT_SCHEMA

from emuflow.platform.platform import BoardLink, Platform

SYSTEM_ROUTES_SCHEMA = "emuflow.system-routes/v1"

SYSTEM_ROUTE_CONSTRAINTS_SCHEMA = "emuflow.system-route-constraints/v1"

RUNTIME_BARRIER_SLOTS = 1

ArcKey = Tuple[str, str, str]

def static_exact_contract_from_assignment(assignment):
    """Resolve only current source-owned metadata; native stages audit cuts."""
    if assignment.get("semantic_contract") is not None:
        raise ValidationError("inline assignment semantic replay is retired")
    if assignment.get("semantic_contract_indexed") is not True:
        return None
    from emuflow.partition.phase3_storage import IndexedCutNetRecords
    digest = assignment.get("semantic_contract_sha256")
    if (assignment.get("semantic_contract_schema") != STATIC_EXACT_STRUCTURAL_CONTRACT_SCHEMA
            or not isinstance(digest, str) or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
            or not isinstance(assignment.get("cut_nets"), IndexedCutNetRecords)):
        raise ValidationError("indexed assignment semantic contract identity is incomplete")
    return {"schema": STATIC_EXACT_STRUCTURAL_CONTRACT_SCHEMA,
            "mode": "static-exact-combinational",
            "qualification": "structural-partition-legality", "indexed": True}

def semantic_contract_for_routes(
    assignment: Mapping[str, Any],
    routes_artifact: Mapping[str, Any],
) -> Optional[Mapping[str, Any]]:
    """Resolve and validate the Phase-3-owned contract bound by Phase 4."""

    exact_contract = static_exact_contract_from_assignment(assignment)
    if exact_contract is None:
        if any(
            key in routes_artifact
            for key in (
                "semantic_contract",
                "semantic_contract_schema",
                "semantic_contract_sha256",
            )
        ):
            raise ValidationError(
                "safe routes may not contain an exact semantic contract"
            )
        return None
    if "semantic_contract" in routes_artifact:
        raise ValidationError(
            "routes may not duplicate the Phase 3 semantic contract"
        )
    if (
        routes_artifact.get("semantic_contract_schema")
        != exact_contract["schema"]
        or routes_artifact.get("semantic_contract_sha256")
        != assignment["semantic_contract_sha256"]
    ):
        raise ValidationError(
            "routes semantic_contract binding does not match assignment"
        )
    return exact_contract

def normalize_route_constraints(
    value: Optional[Mapping[str, Any]],
    platform: Platform,
    frame_slots: Optional[int] = None,
    max_iterations: Optional[int] = None,
) -> Dict[str, Any]:
    raw: Mapping[str, Any] = value or {}
    if raw and raw.get("schema") != SYSTEM_ROUTE_CONSTRAINTS_SCHEMA:
        raise ValidationError(
            "route constraints.schema: expected "
            f"{SYSTEM_ROUTE_CONSTRAINTS_SCHEMA!r}, "
            f"got {raw.get('schema')!r}"
        )

    raw_frame_slots = raw.get("frame_slots", 32)
    if frame_slots is not None:
        raw_frame_slots = frame_slots
    if (
        isinstance(raw_frame_slots, bool)
        or not isinstance(raw_frame_slots, int)
        or raw_frame_slots <= 0
    ):
        raise ValidationError(
            "route constraints.frame_slots: expected a positive integer"
        )

    raw_iterations = raw.get("max_iterations", 20)
    if max_iterations is not None:
        raw_iterations = max_iterations
    if (
        isinstance(raw_iterations, bool)
        or not isinstance(raw_iterations, int)
        or raw_iterations <= 0
    ):
        raise ValidationError(
            "route constraints.max_iterations: expected a positive integer"
        )

    raw_max_route_hops = raw.get("max_route_hops")
    if raw_max_route_hops is not None and (
        isinstance(raw_max_route_hops, bool)
        or not isinstance(raw_max_route_hops, int)
        or raw_max_route_hops <= 0
    ):
        raise ValidationError(
            "route constraints.max_route_hops: expected null or a positive "
            "integer"
        )

    raw_unavailable = raw.get("unavailable_links", [])
    if not isinstance(raw_unavailable, list) or not all(
        isinstance(link_id, str) for link_id in raw_unavailable
    ):
        raise ValidationError(
            "route constraints.unavailable_links: expected an array of strings"
        )
    link_ids = {link.id for link in platform.links}
    unknown = sorted(set(raw_unavailable) - link_ids)
    if unknown:
        raise ValidationError(
            f"route constraints.unavailable_links: unknown links {unknown}"
        )

    raw_link_delays = raw.get("link_delay_ns", {})
    if not isinstance(raw_link_delays, dict):
        raise ValidationError(
            "route constraints.link_delay_ns: expected an object"
        )
    unknown_delays = sorted(set(raw_link_delays) - link_ids)
    if unknown_delays:
        raise ValidationError(
            "route constraints.link_delay_ns: unknown links "
            f"{unknown_delays}"
        )
    link_delays = {}
    for link_id, value in raw_link_delays.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0.0
        ):
            raise ValidationError(
                f"route constraints.link_delay_ns.{link_id}: "
                "expected a non-negative number"
            )
        link_delays[link_id] = float(value)

    raw_directed_delays = raw.get("directed_link_delay_ns", {})
    if not isinstance(raw_directed_delays, dict):
        raise ValidationError(
            "route constraints.directed_link_delay_ns: expected an object"
        )
    legal_arcs = set()
    for link in platform.links:
        left, right = link.endpoints
        legal_arcs.add((link.id, left, right))
        if link.direction in {"full_duplex", "half_duplex"}:
            legal_arcs.add((link.id, right, left))
    directed_delays: Dict[str, Dict[str, Dict[str, float]]] = {}
    for link_id, by_source in raw_directed_delays.items():
        if not isinstance(link_id, str) or not isinstance(by_source, dict):
            raise ValidationError(
                "route constraints.directed_link_delay_ns: invalid link map"
            )
        for source, by_sink in by_source.items():
            if not isinstance(source, str) or not isinstance(by_sink, dict):
                raise ValidationError(
                    "route constraints.directed_link_delay_ns: "
                    "invalid source map"
                )
            for sink, value in by_sink.items():
                if (link_id, source, sink) not in legal_arcs:
                    raise ValidationError(
                        "route constraints.directed_link_delay_ns: "
                        f"unknown arc {(link_id, source, sink)}"
                    )
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or float(value) < 0.0
                ):
                    raise ValidationError(
                        "route constraints.directed_link_delay_ns: "
                        f"invalid delay for {(link_id, source, sink)}"
                    )
                directed_delays.setdefault(link_id, {}).setdefault(
                    source, {}
                )[sink] = float(value)

    raw_sll_links = raw.get("sll_links", [])
    if not isinstance(raw_sll_links, list) or not all(
        isinstance(link_id, str) for link_id in raw_sll_links
    ):
        raise ValidationError(
            "route constraints.sll_links: expected an array of strings"
        )
    unknown_sll = sorted(set(raw_sll_links) - link_ids)
    if unknown_sll:
        raise ValidationError(
            f"route constraints.sll_links: unknown links {unknown_sll}"
        )

    raw_shared_links = raw.get("shared_capacity_links", [])
    if not isinstance(raw_shared_links, list) or not all(
        isinstance(link_id, str) for link_id in raw_shared_links
    ):
        raise ValidationError(
            "route constraints.shared_capacity_links: expected an array "
            "of strings"
        )
    unknown_shared = sorted(set(raw_shared_links) - link_ids)
    if unknown_shared:
        raise ValidationError(
            "route constraints.shared_capacity_links: unknown links "
            f"{unknown_shared}"
        )

    ratio_domains = {
        (
            link.tdm_delay_model.min_ratio,
            link.tdm_delay_model.max_ratio,
            link.tdm_delay_model.ratio_quantum,
        )
        for link in platform.links
        if link.tdm_delay_model is not None
    }
    if len(ratio_domains) > 1:
        raise ValidationError(
            "route constraints require one common BoardDB TDM ratio domain"
        )
    if ratio_domains:
        platform_min_ratio, platform_max_ratio, platform_ratio_quantum = next(
            iter(ratio_domains)
        )
    else:
        platform_min_ratio, platform_max_ratio, platform_ratio_quantum = (
            1,
            raw_frame_slots,
            8,
        )

    optimization_values = {}
    for key, default, integer in (
        ("reroute_rounds", 8, True),
        ("lambda_load", 2.0, False),
        ("lambda_timing", 4.0, False),
        ("lambda_history", 1.0, False),
        ("lambda_tdm", 0.1, False),
        ("tdm_ratio_quantum", platform_ratio_quantum, True),
        ("tdm_min_ratio", platform_min_ratio, True),
        ("tdm_max_ratio", min(raw_frame_slots, platform_max_ratio), True),
    ):
        value = raw.get(key, default)
        valid = (
            not isinstance(value, bool)
            and isinstance(value, int if integer else (int, float))
            and value >= 0
        )
        if not valid:
            kind = "non-negative integer" if integer else "non-negative number"
            raise ValidationError(
                f"route constraints.{key}: expected a {kind}"
            )
        optimization_values[key] = int(value) if integer else float(value)
    if optimization_values["tdm_ratio_quantum"] <= 0:
        raise ValidationError(
            "route constraints.tdm_ratio_quantum: expected a positive integer"
        )
    minimum_ratio = optimization_values["tdm_min_ratio"]
    maximum_ratio = optimization_values["tdm_max_ratio"]
    ratio_quantum = optimization_values["tdm_ratio_quantum"]
    if minimum_ratio <= 0:
        raise ValidationError(
            "route constraints.tdm_min_ratio: expected a positive integer"
        )
    if minimum_ratio % ratio_quantum and not (
        not ratio_domains and minimum_ratio == 1
    ):
        raise ValidationError(
            "route constraints.tdm_min_ratio: expected 1 for a fixed-delay "
            "platform or a multiple of tdm_ratio_quantum"
        )
    if ratio_domains and maximum_ratio % ratio_quantum:
        raise ValidationError(
            "route constraints.tdm_max_ratio: expected a multiple of "
            "tdm_ratio_quantum"
        )
    if minimum_ratio > maximum_ratio or maximum_ratio > raw_frame_slots:
        raise ValidationError(
            "route constraints TDM ratio bounds must satisfy "
            "min_ratio <= max_ratio <= frame_slots"
        )
    if ratio_domains and (
        minimum_ratio < platform_min_ratio
        or maximum_ratio > platform_max_ratio
        or ratio_quantum != platform_ratio_quantum
    ):
        raise ValidationError(
            "route constraints TDM ratio domain must be a supported subset "
            "of the BoardDB delay-model domain"
        )
    tree_edge_sum_tdm = raw.get("tree_edge_sum_tdm", False)
    if not isinstance(tree_edge_sum_tdm, bool):
        raise ValidationError(
            "route constraints.tree_edge_sum_tdm: expected a boolean"
        )
    hard_sll_capacity = raw.get("hard_sll_capacity", False)
    if not isinstance(hard_sll_capacity, bool):
        raise ValidationError(
            "route constraints.hard_sll_capacity: expected a boolean"
        )

    boarddb_shared_links = {
        link.id
        for link in platform.links
        if link.capacity_sharing == "shared_bidirectional"
    }
    for link in platform.links:
        if link.bank_count is not None and link.id in raw_sll_links:
            raise ValidationError("a shared-bank FPGA interconnect cannot be an SLL link")
        if link.tdm_delay_model is None:
            continue
        declared_reference = link.tdm_delay_model.delay_ns(
            link.tdm_delay_model.min_ratio
        )
        overrides = ([link_delays[link.id]] if link.id in link_delays else [])
        overrides += [delay for by_sink in directed_delays.get(link.id, {}).values()
                      for delay in by_sink.values()]
        if any(
            not math.isclose(v, declared_reference, abs_tol=1e-9)
            for v in overrides
        ):
            raise ValidationError(
                f"link {link.id}: fixed delay override conflicts with its TDM ratio model; "
                "remove the stale override and use the BoardDB model")
    return {
        "schema": SYSTEM_ROUTE_CONSTRAINTS_SCHEMA,
        "frame_slots": raw_frame_slots,
        "max_iterations": raw_iterations,
        "max_route_hops": raw_max_route_hops,
        "unavailable_links": sorted(set(raw_unavailable)),
        "link_delay_ns": dict(sorted(link_delays.items())),
        "directed_link_delay_ns": {
            link_id: {
                source: dict(sorted(by_sink.items()))
                for source, by_sink in sorted(by_source.items())
            }
            for link_id, by_source in sorted(directed_delays.items())
        },
        "sll_links": sorted(set(raw_sll_links)),
        "shared_capacity_links": sorted(
            set(raw_shared_links) | boarddb_shared_links
        ),
        "tree_edge_sum_tdm": tree_edge_sum_tdm,
        "hard_sll_capacity": hard_sll_capacity,
        **optimization_values,
    }

def route_link_delay_ns(
    platform: Platform,
    link_id: str,
    source: str,
    sink: str,
    constraints: Mapping[str, Any],
) -> float:
    """Return the affine R=1 reference, or the fixed propagation delay."""
    link = next(link for link in platform.links if link.id == link_id)
    if link.tdm_delay_model is not None:
        return (
            link.tdm_delay_model.intercept_ns
            + link.tdm_delay_model.ratio_coefficient_ns
        )
    directed = constraints.get("directed_link_delay_ns", {})
    override = directed.get(link_id, {}).get(source, {}).get(sink)
    if override is not None:
        return float(override)
    link_override = constraints.get("link_delay_ns", {}).get(link_id)
    if link_override is not None:
        return float(link_override)
    link = next(link for link in platform.links if link.id == link_id)
    return link.latency_cycles * 1000.0 / link.fabric_clock_mhz

def route_link_delay_coefficients_ns(
    platform: Platform, link_id: str, source: str, sink: str,
    constraints: Mapping[str, Any],
) -> Tuple[float, float]:
    """Coefficients for d(R)=base_at_R1+slope*(R-1), independent of slot time."""
    link = next(link for link in platform.links if link.id == link_id)
    base = route_link_delay_ns(platform, link_id, source, sink, constraints)
    slope = (link.tdm_delay_model.ratio_coefficient_ns
             if link.tdm_delay_model is not None else 1000.0 / link.fabric_clock_mhz)
    return base, slope

def _arc_key(link_id: str, source: str, sink: str) -> ArcKey:
    return (link_id, source, sink)

def maximum_schedulable_tdm_ratio(
    constraints: Mapping[str, Any],
    latency_cycles: int,
    *,
    ratio_quantum: Optional[int] = None,
    minimum_ratio: Optional[int] = None,
) -> int:
    """Return the largest discrete ratio that arrives before commit.

    Phase 4 and Phase 5 must use the same discrete ratio domain. Counting
    every physical launch slot is insufficient when the TDM provider only
    admits ratio-quantum multiples: a route can pass aggregate capacity and
    still be impossible to legalize.
    """

    usable_slots = (
        int(constraints["frame_slots"])
        - RUNTIME_BARRIER_SLOTS
        - int(latency_cycles)
    )
    if usable_slots <= 0:
        raise ValidationError(
            "TDM schedule has no transmit slot whose data arrives before "
            "the frame commit"
        )
    quantum = int(
        constraints["tdm_ratio_quantum"]
        if ratio_quantum is None
        else ratio_quantum
    )
    minimum = int(
        constraints.get("tdm_min_ratio", 1)
        if minimum_ratio is None
        else minimum_ratio
    )
    discrete_maximum = (
        (usable_slots // quantum) * quantum
        if usable_slots >= quantum
        else (1 if minimum == 1 else 0)
    )
    maximum = min(
        int(constraints["tdm_max_ratio"]),
        discrete_maximum,
    )
    if maximum < minimum:
        raise ValidationError(
            "TDM schedule has no discrete ratio between the configured "
            "minimum and the frame-arrival deadline"
        )
    return maximum

def _capacity_key(
    link: BoardLink,
    source: str,
    sink: str,
    shared_capacity_links: Set[str],
) -> str:
    if link.bank_count is None and (
        link.direction == "half_duplex" or link.id in shared_capacity_links
    ):
        return f"{link.id}:shared"
    return f"{link.id}:{source}->{sink}"

def build_directed_graph(
    platform: Platform,
    constraints: Mapping[str, Any],
    *,
    reserve_arrival_and_commit: bool = False,
) -> Tuple[
    Dict[str, List[Dict[str, Any]]],
    Dict[ArcKey, Dict[str, Any]],
    Dict[str, Dict[str, Any]],
]:
    unavailable = set(constraints["unavailable_links"])
    shared_capacity_links = set(constraints.get("shared_capacity_links", []))
    adjacency: Dict[str, List[Dict[str, Any]]] = {
        fpga.id: [] for fpga in platform.fpgas
    }
    arcs: Dict[ArcKey, Dict[str, Any]] = {}
    capacity_records: Dict[str, Dict[str, Any]] = {}

    for link in platform.links:
        if link.id in unavailable:
            continue
        left, right = link.endpoints
        directions = [(left, right)]
        if link.direction in {"full_duplex", "half_duplex"}:
            directions.append((right, left))
        for source, sink in directions:
            capacity_key = _capacity_key(
                link, source, sink, shared_capacity_links
            )
            arc = {
                "link": link.id,
                "from": source,
                "to": sink,
                "latency_cycles": link.latency_cycles,
                "capacity_key": capacity_key,
            }
            bank_metadata = ({
                "bank_group": link.id,
                "bank_count": link.bank_count,
                "wires_per_bank": link.wires_per_bank,
                "opposite_capacity_key": _capacity_key(link, sink, source, shared_capacity_links),
            } if link.bank_count is not None else {})
            arc.update(bank_metadata)
            arcs[_arc_key(link.id, source, sink)] = arc
            adjacency[source].append(arc)
            if capacity_key not in capacity_records:
                usable_slots = (
                    1
                    if link.id in constraints["sll_links"]
                    else (
                        constraints["tdm_max_ratio"]
                        if link.tdm_delay_model is not None
                        else constraints["frame_slots"]
                    )
                )
                if (
                    reserve_arrival_and_commit
                    and link.id not in constraints["sll_links"]
                ):
                    usable_slots = maximum_schedulable_tdm_ratio(
                        constraints,
                        link.latency_cycles,
                    )
                if usable_slots <= 0:
                    raise ValidationError(
                        f"link {link.id!r} has no transmit slot whose data "
                        "arrives before the frame commit"
                    )
                capacity_records[capacity_key] = {
                    "key": capacity_key,
                    "link": link.id,
                    "direction": (
                        "shared"
                        if link.bank_count is None and (
                            link.direction == "half_duplex"
                            or link.id in shared_capacity_links
                        )
                        else f"{source}->{sink}"
                    ),
                    "capacity_bits": (
                        link.transport_bits_per_cycle_per_direction
                        * usable_slots
                    ),
                    **bank_metadata,
                }

    for source in adjacency:
        adjacency[source].sort(
            key=lambda arc: (arc["to"], arc["link"], arc["from"])
        )
    return adjacency, arcs, capacity_records
