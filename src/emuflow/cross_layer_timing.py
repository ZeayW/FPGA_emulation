"""Provider-neutral identities joining timing, routing, and TDM artifacts.

The contract deliberately stores only relationships that have no canonical
owner elsewhere.  Route trees remain owned by ``system-routes`` and slot/lane
assignments remain owned by ``tdm-schedule``; this module binds their stable
identities without copying either large artifact into another hot-path JSON.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections import defaultdict, deque
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .errors import ValidationError
from .board_link_timing import (
    build_board_link_timing_model,
    validate_board_link_timing,
)
from .boundary_timing import validate_boundary_timing_database
from .logic_segment_timing import validate_logic_segment_timing
from .phase3_assignment_index import read_phase3_exact_semantic_partition
from .platform import Platform
from .routing import SYSTEM_ROUTES_SCHEMA
from .tdm import TDM_SCHEDULE_SCHEMA
from .tdm_schedule_storage import (
    TDMScheduleView,
    pretty_tdm_schedule_sha256,
)
from .system_route_storage import (
    SystemRoutesView,
    pretty_system_routes_sha256,
)


LEGACY_CROSS_LAYER_TIMING_CONTRACT_SCHEMA = "emuflow.cross-layer-timing/v1"
LEGACY_CROSS_LAYER_TIMING_PROVIDER = (
    "provider-neutral-route-schedule-binding-v1"
)
CROSS_LAYER_TIMING_CONTRACT_SCHEMA = "emuflow.cross-layer-timing/v2"
CROSS_LAYER_TIMING_PROVIDER = "provider-neutral-route-schedule-binding-v2"
CROSS_LAYER_HOP_BINDING_MODE = "canonical-route-schedule-merge-v1"
CROSS_LAYER_PHYSICAL_BINDING_SCHEMA = (
    "emuflow.cross-layer-physical-binding/v1"
)
CROSS_LAYER_PHYSICAL_BINDING_PROVIDER = (
    "provider-neutral-routed-physical-evidence-v1"
)
REGISTERED_BOUNDARY = "registered-boundary"
SAMPLED_VIRTUAL_WIRE = "sampled-virtual-wire"


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    if isinstance(value, TDMScheduleView):
        return pretty_tdm_schedule_sha256(value)
    if isinstance(value, SystemRoutesView):
        return pretty_system_routes_sha256(value)
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(indent=2, sort_keys=True)
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode("utf-8"))
    digest.update(b"\n")
    return digest.hexdigest()


def _finite_time(value: Any, context: str, *, positive: bool) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or (float(value) <= 0.0 if positive else float(value) < 0.0)
    ):
        qualifier = "positive" if positive else "non-negative"
        raise ValidationError(f"{context} must be a finite {qualifier} time")
    return float(value)


def _route_path_to_sink(
    route: Mapping[str, Any], sink: str
) -> List[Tuple[str, str, str]]:
    source = route.get("source")
    raw_edges = route.get("tree_edges")
    if not isinstance(source, str) or not isinstance(raw_edges, list):
        raise ValidationError("cross-layer route metadata is malformed")
    children: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for edge in raw_edges:
        if (
            not isinstance(edge, dict)
            or not isinstance(edge.get("link"), str)
            or not isinstance(edge.get("from"), str)
            or not isinstance(edge.get("to"), str)
        ):
            raise ValidationError("cross-layer route edge is malformed")
        children[edge["from"]].append((edge["link"], edge["to"]))
    parents: Dict[str, Tuple[str, str]] = {}
    queue = deque([source])
    while queue:
        node = queue.popleft()
        for link, child in sorted(children.get(node, [])):
            if child in parents or child == source:
                raise ValidationError("cross-layer route is not a tree")
            parents[child] = (node, link)
            queue.append(child)
    if sink != source and sink not in parents:
        raise ValidationError(
            f"cross-layer route does not reach sink {sink!r}"
        )
    reversed_hops: List[Tuple[str, str, str]] = []
    node = sink
    while node != source:
        parent, link = parents[node]
        reversed_hops.append((link, parent, node))
        node = parent
    return list(reversed(reversed_hops))


def _ordered_route_hop_keys(
    route_by_demand: Mapping[str, Mapping[str, Any]],
):
    """Yield unique routed hops in cross-layer contract order."""

    for demand_id, route in sorted(route_by_demand.items()):
        route_hops = set()
        sinks = route.get("sinks")
        if not isinstance(sinks, list):
            raise ValidationError("cross-layer route metadata is malformed")
        for sink in sorted(sinks):
            for link, source, target in _route_path_to_sink(route, sink):
                route_hops.add((link, source, target))
        for link, source, target in sorted(route_hops):
            yield demand_id, link, source, target


def _ordered_schedule_hop_records(schedule: Mapping[str, Any]):
    """Yield canonical hop keys plus entry IDs without decoding v4 entries.

    The set used by the compatibility branch is intentionally limited to
    legacy in-memory schedules. Production ScheduleStore readers expose a
    typed, ordered cursor whose uniqueness and coverage are validated by the
    store itself.
    """

    indexed_hops = getattr(schedule, "iter_hop_identity_records", None)
    if callable(indexed_hops):
        yield from indexed_hops()
        return
    entries = schedule.get("entries")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise ValidationError("cross-layer schedule entries must be an array")
    entry_by_hop = {}
    entry_ids = set()
    for entry in entries:
        entry_id = entry.get("id") if isinstance(entry, Mapping) else None
        if (
            not isinstance(entry, Mapping)
            or not isinstance(entry_id, str)
            or not entry_id
            or entry_id in entry_ids
        ):
            raise ValidationError("cross-layer schedule entry is malformed")
        key = (
            entry.get("demand"),
            entry.get("link"),
            entry.get("from"),
            entry.get("to"),
        )
        if not all(isinstance(item, str) and item for item in key):
            raise ValidationError("cross-layer schedule entry is malformed")
        if key in entry_by_hop:
            raise ValidationError("cross-layer schedule hop is duplicated")
        entry_ids.add(entry_id)
        entry_by_hop[key] = entry_id
    for key in sorted(entry_by_hop):
        yield *key, entry_by_hop[key]


def _match_route_schedule_hops(route_hops, schedule_hops, *, materialize: bool):
    """Validate the exact ordered relation and optionally build legacy rows."""

    bindings = [] if materialize else None
    count = 0
    missing = object()
    for expected, actual in itertools.zip_longest(
        route_hops,
        schedule_hops,
        fillvalue=missing,
    ):
        if actual is missing or (
            expected is not missing and expected < actual[:4]
        ):
            raise ValidationError(
                "cross-layer schedule does not cover routed hop"
            )
        if expected is missing or actual[:4] != expected:
            raise ValidationError("cross-layer schedule coverage is not exact")
        count += 1
        if bindings is not None:
            demand_id, link, source, target = expected
            bindings.append(
                {
                    "demand": demand_id,
                    "link": link,
                    "from": source,
                    "to": target,
                    "entry": actual[4],
                }
            )
    return count, bindings


def _build_contract(
    routes: Mapping[str, Any],
    schedule: Optional[Mapping[str, Any]],
    semantic_contract: Optional[Mapping[str, Any]],
    *,
    legacy_v1: bool = False,
) -> Dict[str, Any]:
    if routes.get("schema") != SYSTEM_ROUTES_SCHEMA:
        raise ValidationError("cross-layer contract requires system routes")
    raw_routes = routes.get("routes")
    if not isinstance(raw_routes, list):
        raise ValidationError("cross-layer routes must be an array")
    route_by_net: Dict[str, Mapping[str, Any]] = {}
    route_by_demand: Dict[str, Mapping[str, Any]] = {}
    compact_metadata = getattr(
        raw_routes, "iter_cross_layer_route_metadata", None
    )
    indexed_hop_keys = getattr(raw_routes, "iter_cross_layer_hop_keys", None)
    if callable(compact_metadata) and callable(indexed_hop_keys):
        for net, demand, source, sinks in compact_metadata():
            if net in route_by_net:
                raise ValidationError("cross-layer route identities are invalid")
            route_by_net[net] = {
                "id": demand,
                "net": net,
                "source": source,
                "sinks": list(sinks),
            }
        if len(route_by_net) != len(raw_routes):
            raise ValidationError("cross-layer route coverage is incomplete")
    else:
        for route in raw_routes:
            if (
                not isinstance(route, dict)
                or not isinstance(route.get("id"), str)
                or not isinstance(route.get("net"), str)
                or route["id"] in route_by_demand
                or route["net"] in route_by_net
            ):
                raise ValidationError("cross-layer route identities are invalid")
            route_by_demand[route["id"]] = route
            route_by_net[route["net"]] = route

    semantic_sha = routes.get("semantic_contract_sha256")
    semantic_schema = routes.get("semantic_contract_schema")
    if "semantic_contract" in routes:
        raise ValidationError(
            "cross-layer routes may not duplicate the Phase 3 semantic contract"
        )
    logic_segment_bindings = []
    indexed_logic_segments = False
    if semantic_contract is None:
        if semantic_sha is not None or semantic_schema is not None:
            raise ValidationError("cross-layer semantic binding is incomplete")
        transport_semantics = REGISTERED_BOUNDARY
        semantic_binding = None
    else:
        if (
            not isinstance(semantic_contract, dict)
            or not isinstance(semantic_sha, str)
            or semantic_schema != semantic_contract.get("schema")
        ):
            raise ValidationError("cross-layer semantic binding is incomplete")
        transport_semantics = SAMPLED_VIRTUAL_WIRE
        semantic_binding = {
            "schema": semantic_contract.get("schema"),
            "sha256": semantic_sha,
        }
        indexed_logic_segments = semantic_contract.get("indexed") is True
        if indexed_logic_segments:
            semantic_binding["storage"] = "phase3-assignment-index"
        else:
            raw_logic_segments = semantic_contract.get("logic_segments")
            if not isinstance(raw_logic_segments, list):
                raise ValidationError(
                    "cross-layer semantic logic segments must be an array"
                )
            seen_logic_segments = set()
            for segment in raw_logic_segments:
                segment_id = (
                    segment.get("id") if isinstance(segment, dict) else None
                )
                kind = (
                    segment.get("kind") if isinstance(segment, dict) else None
                )
                fpga = (
                    segment.get("fpga") if isinstance(segment, dict) else None
                )
                source_semantics = (
                    segment.get("source_semantics")
                    if isinstance(segment, dict)
                    else None
                )
                if (
                    not isinstance(segment_id, str)
                    or not segment_id
                    or segment_id in seen_logic_segments
                    or kind
                    not in {"launch_to_tx", "rx_to_tx", "rx_to_capture"}
                    or not isinstance(fpga, str)
                    or not fpga
                    or source_semantics
                    not in {None, "configuration-stable-constant"}
                    or (
                        source_semantics == "configuration-stable-constant"
                        and kind != "launch_to_tx"
                    )
                ):
                    raise ValidationError(
                        "cross-layer semantic logic-segment identity is invalid"
                    )
                seen_logic_segments.add(segment_id)
                binding = {
                    "segment": segment_id,
                    "kind": kind,
                    "fpga": fpga,
                }
                if source_semantics is not None:
                    binding["source_semantics"] = source_semantics
                logic_segment_bindings.append(binding)

    path_bindings = []
    timing = routes.get("timing")
    if timing is not None:
        raw_paths = timing.get("paths") if isinstance(timing, dict) else None
        if not isinstance(raw_paths, list):
            raise ValidationError("cross-layer timing paths must be an array")
        seen_paths = set()
        for path in raw_paths:
            path_id = path.get("path") if isinstance(path, dict) else None
            clock_domain = (
                path.get("clock_domain") if isinstance(path, dict) else None
            )
            cut_nets = path.get("cut_nets") if isinstance(path, dict) else None
            transitions = (
                path.get("cut_transitions") if isinstance(path, dict) else None
            )
            if (
                not isinstance(path_id, str)
                or path_id in seen_paths
                or not isinstance(clock_domain, str)
                or not clock_domain
                or not isinstance(cut_nets, list)
                or not all(isinstance(item, str) for item in cut_nets)
            ):
                raise ValidationError("cross-layer timing path is malformed")
            required_time_ns = _finite_time(
                path.get("required_time_ns", path.get("clock_period_ns")),
                f"cross-layer timing path {path_id!r}.required_time_ns",
                positive=True,
            )
            estimated_logic_delay_ns = _finite_time(
                path.get("fixed_delay_ns"),
                f"cross-layer timing path {path_id!r}.fixed_delay_ns",
                positive=False,
            )
            members = path.get("compressed_path_ids", [path_id])
            if (
                not isinstance(members, list)
                or not members
                or not all(isinstance(item, str) and item for item in members)
                or len(set(members)) != len(members)
            ):
                raise ValidationError(
                    "cross-layer timing path member identities are invalid"
                )
            seen_paths.add(path_id)
            if transitions is None:
                transition_records = []
                for net in cut_nets:
                    route = route_by_net.get(net)
                    sinks = route.get("sinks") if route is not None else None
                    if not isinstance(sinks, list) or len(sinks) != 1:
                        raise ValidationError(
                            "multicast timing paths require explicit transitions"
                        )
                    transition_records.append(
                        {"net": net, "from": route["source"], "to": sinks[0]}
                    )
            else:
                if not isinstance(transitions, list) or len(transitions) != len(
                    cut_nets
                ):
                    raise ValidationError(
                        "cross-layer timing transition coverage is invalid"
                    )
                transition_records = transitions
            cuts = []
            for cut_index, (net, transition) in enumerate(
                zip(cut_nets, transition_records)
            ):
                route = route_by_net.get(net)
                if (
                    route is None
                    or not isinstance(transition, dict)
                    or transition.get("net") != net
                    or transition.get("from") != route.get("source")
                    or transition.get("to") not in route.get("sinks", [])
                ):
                    raise ValidationError(
                        "cross-layer timing transition does not match route"
                    )
                cuts.append(
                    {
                        "cut": f"{path_id}:cut:{cut_index}",
                        "index": cut_index,
                        "logical_net": net,
                        "demand": route["id"],
                        "from": transition["from"],
                        "to": transition["to"],
                    }
                )
            path_bindings.append(
                {
                    "path": path_id,
                    "members": list(members),
                    "clock_domain": clock_domain,
                    "required_time_ns": required_time_ns,
                    "estimated_logic_delay_ns": estimated_logic_delay_ns,
                    "cuts": cuts,
                }
            )

    hop_bindings = [] if legacy_v1 else None
    scheduled_hops = 0
    schedule_binding = None
    if schedule is not None:
        if (
            schedule.get("schema") != TDM_SCHEDULE_SCHEMA
            or schedule.get("design") != routes.get("design")
            or schedule.get("platform") != routes.get("platform")
        ):
            raise ValidationError("cross-layer schedule identity is invalid")
        if schedule.get("transport_semantics") != transport_semantics:
            raise ValidationError(
                "cross-layer schedule transport semantics disagree with "
                "the Phase 3 contract"
            )
        route_hop_keys = (
            indexed_hop_keys()
            if callable(indexed_hop_keys)
            else _ordered_route_hop_keys(route_by_demand)
        )
        scheduled_hops, hop_bindings = _match_route_schedule_hops(
            route_hop_keys,
            _ordered_schedule_hop_records(schedule),
            materialize=legacy_v1,
        )
        schedule_binding = {
            "provider": schedule.get("provider"),
            "sha256": _canonical_sha256(schedule),
        }

    result: Dict[str, Any] = {
        "schema": (
            LEGACY_CROSS_LAYER_TIMING_CONTRACT_SCHEMA
            if legacy_v1
            else CROSS_LAYER_TIMING_CONTRACT_SCHEMA
        ),
        "provider": (
            LEGACY_CROSS_LAYER_TIMING_PROVIDER
            if legacy_v1
            else CROSS_LAYER_TIMING_PROVIDER
        ),
        "design": routes.get("design"),
        "platform": routes.get("platform"),
        "transport_semantics": transport_semantics,
        "route_binding": {
            "provider": routes.get("provider"),
            "sha256": _canonical_sha256(routes),
        },
        "path_bindings": sorted(path_bindings, key=lambda item: item["path"]),
        "logic_segment_bindings": sorted(
            logic_segment_bindings, key=lambda item: item["segment"]
        ),
        "metrics": {
            "paths": len(path_bindings),
            "path_cut_bindings": sum(
                len(item["cuts"]) for item in path_bindings
            ),
            "logic_segments": len(logic_segment_bindings),
            "scheduled_hops": scheduled_hops,
        },
    }
    if legacy_v1:
        result["hop_bindings"] = hop_bindings
    elif schedule is not None:
        result["hop_binding"] = {
            "mode": CROSS_LAYER_HOP_BINDING_MODE,
            "scheduled_hops": scheduled_hops,
        }
    if semantic_binding is not None:
        result["semantic_binding"] = semantic_binding
    if indexed_logic_segments:
        result["logic_segment_binding"] = {
            "mode": "phase3-assignment-index",
            "semantic_contract_sha256": semantic_sha,
        }
    if schedule_binding is not None:
        result["schedule_binding"] = schedule_binding
    return result


def build_cross_layer_timing_contract(
    routes: Mapping[str, Any],
    schedule: Optional[Mapping[str, Any]] = None,
    semantic_contract: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a compact, provider-neutral route/schedule identity contract."""

    return _build_contract(routes, schedule, semantic_contract)


def validate_cross_layer_timing_contract(
    routes: Mapping[str, Any],
    contract: Mapping[str, Any],
    schedule: Optional[Mapping[str, Any]] = None,
    semantic_contract: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Independently rebuild and exactly compare a cross-layer contract."""

    schema = contract.get("schema")
    if schema not in {
        CROSS_LAYER_TIMING_CONTRACT_SCHEMA,
        LEGACY_CROSS_LAYER_TIMING_CONTRACT_SCHEMA,
    }:
        raise ValidationError("cross-layer timing contract schema is invalid")
    expected = _build_contract(
        routes,
        schedule,
        semantic_contract,
        legacy_v1=schema == LEGACY_CROSS_LAYER_TIMING_CONTRACT_SCHEMA,
    )
    if contract != expected:
        raise ValidationError(
            "cross-layer timing contract does not match canonical inputs"
        )
    return {"status": "pass", **expected["metrics"]}


def _non_negative_delay(value: Any, context: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0.0
    ):
        raise ValidationError(f"{context} must be a finite non-negative delay")
    return float(value)


def _build_physical_binding(
    contract: Mapping[str, Any],
    schedule: Mapping[str, Any],
    physical_summary: Mapping[str, Any],
    platform: Platform,
    assignment_index_path: Optional[Path] = None,
) -> Dict[str, Any]:
    contract_identity = (
        contract.get("schema"),
        contract.get("provider"),
    )
    if (
        contract_identity
        not in {
            (
                CROSS_LAYER_TIMING_CONTRACT_SCHEMA,
                CROSS_LAYER_TIMING_PROVIDER,
            ),
            (
                LEGACY_CROSS_LAYER_TIMING_CONTRACT_SCHEMA,
                LEGACY_CROSS_LAYER_TIMING_PROVIDER,
            ),
        }
        or schedule.get("schema") != TDM_SCHEDULE_SCHEMA
        or contract.get("design") != schedule.get("design")
        or contract.get("platform") != platform.name
        or schedule.get("platform") != platform.name
        or contract.get("schedule_binding", {}).get("sha256")
        != _canonical_sha256(schedule)
    ):
        raise ValidationError("cross-layer physical binding identity is invalid")

    entries = schedule.get("entries")
    if not isinstance(entries, Sequence) or isinstance(
        entries, (str, bytes)
    ):
        raise ValidationError("cross-layer physical schedule entries are invalid")
    expected_endpoints = {
        f"__emuflow_{kind}_{entry['id']}": {
            "endpoint": f"__emuflow_{kind}_{entry['id']}",
            "kind": kind,
            "schedule_entry": entry["id"],
            "fpga": entry["from"] if kind == "tx" else entry["to"],
        }
        for entry in entries
        for kind in ("tx", "rx")
    }
    endpoint_records: Dict[str, Dict[str, Any]] = {}
    raw_boundary = physical_summary.get("boundary_timing")
    identities = physical_summary.get("boundary_identities")
    if raw_boundary is not None:
        if not isinstance(raw_boundary, dict) or not isinstance(identities, dict):
            raise ValidationError("physical boundary evidence maps are invalid")
        if set(raw_boundary) != set(identities):
            raise ValidationError("physical boundary evidence coverage disagrees")
        for fpga in sorted(raw_boundary):
            database = raw_boundary[fpga]
            validate_boundary_timing_database(database, identities[fpga])
            for item in database["endpoints"]:
                endpoint = item["id"]
                if endpoint in endpoint_records:
                    raise ValidationError(
                        f"duplicate physical endpoint evidence {endpoint!r}"
                    )
                expected = expected_endpoints.get(endpoint)
                if expected is None or expected["fpga"] != fpga:
                    raise ValidationError(
                        f"unexpected physical endpoint evidence {endpoint!r}"
                    )
                endpoint_records[endpoint] = {
                    **expected,
                    "measured_min_delay_ns": _non_negative_delay(
                        item["delay_ns"], f"physical endpoint {endpoint}"
                    ),
                    "measured_max_delay_ns": _non_negative_delay(
                        item["delay_ns"], f"physical endpoint {endpoint}"
                    ),
                    "measurement": item.get("measurement"),
                }

    expected_logic: Dict[str, Mapping[str, Any]] = {}
    if contract.get("transport_semantics") == SAMPLED_VIRTUAL_WIRE:
        raw_segments = contract.get("logic_segment_bindings")
        if not isinstance(raw_segments, list):
            raise ValidationError("sampled transport lacks logic-segment identities")
        for segment in raw_segments:
            segment_id = (
                segment.get("segment") if isinstance(segment, dict) else None
            )
            if not isinstance(segment_id, str) or segment_id in expected_logic:
                raise ValidationError("sampled logic-segment identities are invalid")
            expected_logic[segment_id] = segment
        indexed_binding = contract.get("logic_segment_binding")
        if indexed_binding is not None:
            if (
                not isinstance(indexed_binding, dict)
                or indexed_binding.get("mode") != "phase3-assignment-index"
                or indexed_binding.get("semantic_contract_sha256")
                != contract.get("semantic_binding", {}).get("sha256")
                or assignment_index_path is None
            ):
                raise ValidationError(
                    "sampled transport indexed logic binding is invalid"
                )
            for fpga_record in platform.fpgas:
                partition = read_phase3_exact_semantic_partition(
                    assignment_index_path, fpga_record.id
                )
                if (
                    partition.get("sha256")
                    != indexed_binding["semantic_contract_sha256"]
                ):
                    raise ValidationError(
                        "indexed logic-segment digest disagrees"
                    )
                for semantic in partition["logic_segments"]:
                    segment_id = semantic.get("id")
                    if (
                        not isinstance(segment_id, str)
                        or segment_id in expected_logic
                        or semantic.get("fpga") != fpga_record.id
                    ):
                        raise ValidationError(
                            "indexed logic-segment identity is invalid"
                        )
                    binding = {
                        "segment": segment_id,
                        "kind": semantic.get("kind"),
                        "fpga": fpga_record.id,
                    }
                    if semantic.get("source_semantics") is not None:
                        binding["source_semantics"] = semantic[
                            "source_semantics"
                        ]
                    expected_logic[segment_id] = binding
    logic_measurements: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    physical_logic_ids = set()
    raw_logic = physical_summary.get("logic_segment_timing")
    if raw_logic is not None:
        if not isinstance(raw_logic, dict):
            raise ValidationError("physical logic-segment evidence is invalid")
        for fpga in sorted(raw_logic):
            database = raw_logic[fpga]
            validation = validate_logic_segment_timing(database)
            if validation["fpga"] != fpga:
                raise ValidationError("physical logic-segment FPGA disagrees")
            for item in database["segments"]:
                physical_id = item.get("id")
                if physical_id in physical_logic_ids:
                    raise ValidationError(
                        f"duplicate physical logic record {physical_id!r}"
                    )
                physical_logic_ids.add(physical_id)
                segment_id = item.get("static_exact_segment_id")
                if segment_id is None:
                    continue
                expected = expected_logic.get(segment_id)
                if expected is None or expected.get("fpga") != fpga:
                    raise ValidationError(
                        f"unexpected physical logic segment {segment_id!r}"
                    )
                _non_negative_delay(
                    item["delay_ns"], f"physical logic segment {segment_id}"
                )
                logic_measurements[segment_id].append(item)
    logic_records: Dict[str, Dict[str, Any]] = {}
    for segment_id, measurements in sorted(logic_measurements.items()):
        delays = [float(item["delay_ns"]) for item in measurements]
        cone_bound = any(
            item.get("measurement", "endpoint-exact")
            == "cut-net-cone-upper-bound"
            for item in measurements
        )
        logic_records[segment_id] = {
            "segment": segment_id,
            "fpga": expected_logic[segment_id].get("fpga"),
            "measured_min_delay_ns": min(delays),
            "measured_max_delay_ns": max(delays),
            "measurement": (
                "routed-cone-upper-bound"
                if cone_bound
                else "routed-endpoint-exact"
            ),
        }
    for segment_id, segment in expected_logic.items():
        if segment_id in logic_records:
            continue
        if segment.get("source_semantics") == "configuration-stable-constant":
            logic_records[segment_id] = {
                "segment": segment_id,
                "fpga": segment.get("fpga"),
                "measured_min_delay_ns": 0.0,
                "measured_max_delay_ns": 0.0,
                "measurement": "structural-configuration-stable-constant",
            }

    board_database = physical_summary.get("board_link_timing")
    if board_database is None:
        board_database = build_board_link_timing_model(platform)
    board_validation = validate_board_link_timing(board_database, platform)
    board_by_hop = {
        (item["link"], item["from"], item["to"]): {
            "delay_bound_ns": _non_negative_delay(
                item["delay_bound_ns"], "physical board-link delay"
            ),
            "qualification": item["qualification"],
        }
        for item in board_database["links"]
    }
    if contract.get("schema") == LEGACY_CROSS_LAYER_TIMING_CONTRACT_SCHEMA:
        contract_hops = contract.get("hop_bindings")
        if not isinstance(contract_hops, list):
            raise ValidationError("legacy cross-layer hop bindings are invalid")
    else:
        hop_binding = contract.get("hop_binding")
        scheduled_hops = contract.get("metrics", {}).get("scheduled_hops")
        if hop_binding != {
            "mode": CROSS_LAYER_HOP_BINDING_MODE,
            "scheduled_hops": scheduled_hops,
        } or not isinstance(scheduled_hops, int) or isinstance(
            scheduled_hops, bool
        ) or scheduled_hops < 0:
            raise ValidationError("cross-layer hop binding summary is invalid")
        contract_hops = (
            {
                "demand": demand,
                "link": link,
                "from": source,
                "to": target,
                "entry": entry,
            }
            for demand, link, source, target, entry in (
                _ordered_schedule_hop_records(schedule)
            )
        )
    board_records = []
    for hop in contract_hops:
        key = (hop["link"], hop["from"], hop["to"])
        board_records.append(
            {
                "schedule_entry": hop["entry"],
                "link": hop["link"],
                "from": hop["from"],
                "to": hop["to"],
                "delay_bound_ns": board_by_hop[key]["delay_bound_ns"],
                "measurement": board_by_hop[key]["qualification"],
            }
        )
    if len(board_records) != contract.get("metrics", {}).get("scheduled_hops"):
        raise ValidationError("cross-layer hop binding coverage disagrees")

    missing_endpoints = sorted(set(expected_endpoints) - set(endpoint_records))
    missing_logic = sorted(set(expected_logic) - set(logic_records))
    status = "pass" if not missing_endpoints and not missing_logic else "incomplete"
    return {
        "schema": CROSS_LAYER_PHYSICAL_BINDING_SCHEMA,
        "provider": CROSS_LAYER_PHYSICAL_BINDING_PROVIDER,
        "status": status,
        "design": contract["design"],
        "platform": contract["platform"],
        "transport_semantics": contract["transport_semantics"],
        "cross_layer_timing_sha256": _canonical_sha256(contract),
        "physical_source": {
            "provider": physical_summary.get("provider"),
            "qualification": physical_summary.get("qualification"),
        },
        "endpoint_bindings": [endpoint_records[key] for key in sorted(endpoint_records)],
        "logic_segment_bindings": [logic_records[key] for key in sorted(logic_records)],
        "board_bindings": board_records,
        "missing": {
            "endpoints": missing_endpoints,
            "logic_segments": missing_logic,
        },
        "metrics": {
            "required_endpoints": len(expected_endpoints),
            "measured_endpoints": len(endpoint_records),
            "required_logic_segments": len(expected_logic),
            "measured_logic_segments": len(logic_records),
            "routed_board_hops": len(board_records),
            "model_only_board_hops": sum(
                item["measurement"] == "model-only" for item in board_records
            ),
            "characterized_board_hops": sum(
                item["measurement"] == "characterized-upper-bound"
                for item in board_records
            ),
            "measured_board_hops": sum(
                item["measurement"] == "measured-upper-bound"
                for item in board_records
            ),
            "final_board_link_timing_signoff": board_validation[
                "final_link_timing_signoff"
            ],
        },
    }


def build_cross_layer_physical_binding(
    contract: Mapping[str, Any],
    schedule: Mapping[str, Any],
    physical_summary: Mapping[str, Any],
    platform: Platform,
    assignment_index_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Bind stable route/schedule IDs to measured Phase 7 evidence."""

    return _build_physical_binding(
        contract,
        schedule,
        physical_summary,
        platform,
        assignment_index_path,
    )


def validate_cross_layer_physical_binding(
    contract: Mapping[str, Any],
    schedule: Mapping[str, Any],
    physical_summary: Mapping[str, Any],
    platform: Platform,
    binding: Mapping[str, Any],
    assignment_index_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Independently rebuild physical evidence coverage and exact bindings."""

    expected = _build_physical_binding(
        contract,
        schedule,
        physical_summary,
        platform,
        assignment_index_path,
    )
    if binding != expected:
        raise ValidationError(
            "cross-layer physical binding does not match canonical inputs"
        )
    return {"status": expected["status"], **expected["metrics"]}
