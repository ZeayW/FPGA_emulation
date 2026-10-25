"""Global candidate routing adapter and independent route/timing checker."""

from __future__ import annotations

import copy
import heapq
import math
import sqlite3
import struct
import subprocess
import tempfile
from array import array
from collections import defaultdict, deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .sqlite_artifacts import immutable_sqlite_uri
from .errors import EmuFlowError, ValidationError
from .store_publication import (
    bind_store_source,
    require_store_sources_unchanged,
)
from .io import read_json, write_json
from .native_tools import resolve_native_stage_command
from .platform import Platform
from .bank_allocation import apply_bank_domain_ratios
from .routing import (
    assignment_has_static_exact_contract,
    ArcKey,
    _arc_key,
    build_directed_graph,
    demands_from_assignment,
    estimate_tdm_ratio,
    normalize_route_constraints,
    route_link_delay_ns,
    route_link_delay_coefficients_ns,
    validate_system_routes,
    _validate_route_tree,
)
from .routing_candidates import (
    ROUTE_CANDIDATE_GENERATORS,
    ROUTE_MASTER_GENERATORS,
    ROUTE_CANDIDATE_POOL_PROVIDER,
    ROUTE_CANDIDATE_POOL_SCHEMA,
    validate_route_candidate_pool,
)
from .routing_batches import (
    build_compact_route_refinement_batches,
    build_route_refinement_batches,
)
from .tdm import RUNTIME_BARRIER_SLOTS


STA_PATHS_SCHEMA = "emuflow.sta-paths/v1"
GLOBAL_CANDIDATE_PROVIDER = "timing-aware-global-candidate-v1"
ROUND_CAPACITY_CONTRACT_SCHEMA = "emuflow.route-round-capacity/v1"
_NATIVE_ROUTE_RESULT_APPLICATION_ID = 0x45545252
_NATIVE_ROUTE_RESULT_USER_VERSION = 1
_INTEGER_NATIVE_METRICS = {
    "iterations",
    "accepted_reroutes",
    "rolled_back_reroutes",
    "baseline_candidate_feasible",
    "balanced_candidate_feasible",
    "selected_delay_demand_balanced",
    "total_link_bit_hops",
    "estimated_max_tdm_ratio",
    "master_rounds",
    "master_switches",
    "master_exact",
    "metric_closure_candidate_feasible",
    "shallow_light_candidate_feasible",
    "adaptive_hop_candidate_feasible",
    "steiner_candidate_feasible",
    "candidate_workers",
    "parallel_candidate_tasks",
    "reroute_conflict_batches",
    "maximum_parallel_batch",
    "parallel_reroute_tasks",
}


def _round_available_slots(
    frame_slots: int,
    boundary: int,
    latency_cycles: int,
    transport_round: int,
) -> int:
    if transport_round == 0:
        return boundary - latency_cycles
    if transport_round == 1:
        return (
            frame_slots
            - RUNTIME_BARRIER_SLOTS
            - latency_cycles
            - boundary
        )
    raise ValidationError(
        "round-aware routing supports transport rounds 0 and 1"
    )


def _reference_round_usage(
    demands: Sequence[Mapping[str, Any]],
    adjacency: Mapping[str, Sequence[Mapping[str, Any]]],
    platform: Platform,
    constraints: Mapping[str, Any],
) -> Dict[Tuple[str, int], int]:
    """Build an uncongested reference load only for boundary ranking."""

    usage: Dict[Tuple[str, int], int] = defaultdict(int)
    for demand in demands:
        source = demand["source"]
        round_id = int(demand.get("transport_round", 0))
        best: Dict[str, Tuple[float, Tuple[ArcKey, ...]]] = {
            source: (0.0, ())
        }
        queue: List[Tuple[float, Tuple[ArcKey, ...], str]] = [
            (0.0, (), source)
        ]
        while queue:
            delay, path, node = heapq.heappop(queue)
            if best.get(node) != (delay, path):
                continue
            for arc in adjacency.get(node, []):
                key = _arc_key(arc["link"], node, arc["to"])
                candidate = (
                    delay
                    + route_link_delay_ns(
                        platform,
                        arc["link"],
                        node,
                        arc["to"],
                        constraints,
                    ),
                    (*path, key),
                )
                if candidate < best.get(arc["to"], (float("inf"), ())):
                    best[arc["to"]] = candidate
                    heapq.heappush(
                        queue, (candidate[0], candidate[1], arc["to"])
                    )
        tree_edges = {
            key for sink in demand["sinks"] for key in best[sink][1]
        }
        for key in tree_edges:
            arc = next(
                item
                for item in adjacency[key[1]]
                if _arc_key(item["link"], item["from"], item["to"])
                == key
            )
            usage[(arc["capacity_key"], round_id)] += int(
                demand["width_bits"]
            )
    return usage


def _select_round_boundary(
    demands: Sequence[Mapping[str, Any]],
    adjacency: Mapping[str, Sequence[Mapping[str, Any]]],
    capacities: Mapping[str, Mapping[str, Any]],
    platform: Platform,
    constraints: Mapping[str, Any],
) -> int:
    active_rounds = sorted(
        {int(demand.get("transport_round", 0)) for demand in demands}
    )
    if active_rounds != [0, 1]:
        raise ValidationError(
            "round-aware routing requires transport rounds 0 and 1"
        )
    link_by_id = {link.id: link for link in platform.links}
    frame_slots = int(constraints["frame_slots"])
    maximum_latency = max(
        link_by_id[capacity["link"]].latency_cycles
        for capacity in capacities.values()
    )
    first = maximum_latency + 1
    last = (
        frame_slots
        - RUNTIME_BARRIER_SLOTS
        - maximum_latency
        - 1
    )
    if first > last:
        raise ValidationError(
            "round-aware routing has no legal global round boundary"
        )
    reference_usage = _reference_round_usage(
        demands, adjacency, platform, constraints
    )
    midpoint = (first + last) / 2.0

    def score(boundary: int) -> Tuple[int, float, float, float, float]:
        overloaded = 0
        maximum_excess = 0.0
        total_excess = 0.0
        maximum_utilization = 0.0
        for key, capacity in capacities.items():
            link = link_by_id[capacity["link"]]
            for round_id in active_rounds:
                slots = _round_available_slots(
                    frame_slots,
                    boundary,
                    link.latency_cycles,
                    round_id,
                )
                available = (
                    capacity["capacity_bits"]
                    if capacity["link"] in constraints["sll_links"]
                    else link.transport_bits_per_cycle_per_direction * slots
                )
                used = reference_usage.get((key, round_id), 0)
                utilization = used / available
                maximum_utilization = max(maximum_utilization, utilization)
                if used > available:
                    overloaded += 1
                    excess = (used - available) / available
                    maximum_excess = max(maximum_excess, excess)
                    total_excess += excess
        return (
            overloaded,
            maximum_excess,
            total_excess,
            maximum_utilization,
            abs(boundary - midpoint),
        )

    return min(range(first, last + 1), key=lambda item: (score(item), item))


def _validate_round_capacity_contract(
    routes: Mapping[str, Any],
    platform: Platform,
    constraints: Mapping[str, Any],
    *,
    exact_dependency_schedule: bool = False,
) -> Dict[str, Any]:
    active_rounds = sorted(
        {
            int(route.get("transport_round", 0))
            for route in routes.get("routes", [])
        }
    )
    contract = routes.get("round_capacity_contract")
    if exact_dependency_schedule:
        if contract is not None:
            raise ValidationError(
                "Static Exact routes must not carry a global transport-round "
                "capacity contract"
            )
        return {
            "transport_round_policy": "dependency-diagnostic-only",
        }
    if active_rounds != [0, 1]:
        if contract is not None:
            raise ValidationError(
                "routes.round_capacity_contract is only valid for rounds 0/1"
            )
        return {}
    if not isinstance(contract, dict):
        raise ValidationError(
            "routes.round_capacity_contract is required for rounds 0/1"
        )
    boundary = contract.get("source_ready_slot")
    if isinstance(boundary, bool) or not isinstance(boundary, int):
        raise ValidationError(
            "routes.round_capacity_contract.source_ready_slot is invalid"
        )
    _, arcs, capacities = build_directed_graph(platform, constraints)
    link_by_id = {link.id: link for link in platform.links}
    maximum_latency = max(
        link_by_id[capacity["link"]].latency_cycles
        for capacity in capacities.values()
    )
    if not (
        maximum_latency + 1
        <= boundary
        <= int(constraints["frame_slots"])
        - RUNTIME_BARRIER_SLOTS
        - maximum_latency
        - 1
    ):
        raise ValidationError(
            "routes.round_capacity_contract has an illegal round boundary"
        )
    usage: Dict[Tuple[str, int], int] = defaultdict(int)
    for route in routes["routes"]:
        round_id = int(route.get("transport_round", 0))
        width = int(route["width_bits"])
        for edge in route["tree_edges"]:
            arc = arcs[_arc_key(edge["link"], edge["from"], edge["to"])]
            usage[(arc["capacity_key"], round_id)] += width
    expected_domains = []
    for capacity_key in sorted(capacities):
        capacity = capacities[capacity_key]
        link = link_by_id[capacity["link"]]
        for round_id in active_rounds:
            available_slots = _round_available_slots(
                int(constraints["frame_slots"]),
                boundary,
                link.latency_cycles,
                round_id,
            )
            capacity_bits = (
                capacity["capacity_bits"]
                if capacity["link"] in constraints["sll_links"]
                else link.transport_bits_per_cycle_per_direction
                * available_slots
            )
            used_bits = usage[(capacity_key, round_id)]
            if used_bits > capacity_bits:
                raise ValidationError(
                    "routes exceed transport-round capacity for "
                    f"{capacity_key!r} round {round_id}: "
                    f"{used_bits} > {capacity_bits}"
                )
            expected_domains.append(
                {
                    "key": capacity_key,
                    "link": capacity["link"],
                    "direction": capacity["direction"],
                    "transport_round": round_id,
                    "latency_cycles": link.latency_cycles,
                    "lanes": link.transport_bits_per_cycle_per_direction,
                    "available_slots": available_slots,
                    "capacity_bits": capacity_bits,
                    "used_bits": used_bits,
                    "utilization": used_bits / capacity_bits,
                }
            )
    expected = {
        "schema": ROUND_CAPACITY_CONTRACT_SCHEMA,
        "frame_slots": int(constraints["frame_slots"]),
        "barrier_slots": RUNTIME_BARRIER_SLOTS,
        "source_ready_slot": boundary,
        "active_rounds": active_rounds,
        "domains": expected_domains,
    }
    if contract != expected:
        raise ValidationError(
            "routes.round_capacity_contract does not match independent "
            "reconstruction"
        )
    return {
        "round_capacity_boundary": boundary,
        "round_capacity_domains": len(expected_domains),
        "max_round_capacity_utilization": max(
            (record["utilization"] for record in expected_domains),
            default=0.0,
        ),
    }


def _number(value: Any, context: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{context}: expected a number")
    result = float(value)
    if positive and result <= 0.0:
        raise ValidationError(f"{context}: expected a positive number")
    return result


def _normalize_cut_transitions(
    value: Any,
    cut_nets: Sequence[str],
    demand_by_net: Mapping[str, Mapping[str, Any]],
    context: str,
) -> Optional[List[Dict[str, str]]]:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != len(cut_nets):
        raise ValidationError(f"{context}: expected one transition per cut net")
    result = []
    for index, (raw, net) in enumerate(zip(value, cut_nets)):
        item_context = f"{context}[{index}]"
        demand = demand_by_net[net]
        if (
            not isinstance(raw, dict)
            or set(raw) != {"net", "from", "to"}
            or raw.get("net") != net
            or raw.get("from") != demand["source"]
            or raw.get("to") not in demand["sinks"]
        ):
            raise ValidationError(f"{item_context}: invalid routed sink")
        result.append(dict(raw))
    if any(
        result[index - 1]["to"] != result[index]["from"]
        for index in range(1, len(result))
    ):
        raise ValidationError(f"{context}: partition chain is discontinuous")
    return result


def normalize_sta_paths(
    value: Mapping[str, Any],
    demands: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    if value.get("schema") != STA_PATHS_SCHEMA:
        raise ValidationError(
            f"timing paths.schema: expected {STA_PATHS_SCHEMA!r}, "
            f"got {value.get('schema')!r}"
        )
    demand_by_net = {demand["net"]: demand for demand in demands}
    raw_paths = value.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValidationError("timing paths.paths: expected a non-empty array")

    paths: List[Dict[str, Any]] = []
    path_ids = set()
    demand_nets = set(demand_by_net)
    for index, raw in enumerate(raw_paths):
        context = f"timing paths.paths[{index}]"
        if not isinstance(raw, dict):
            raise ValidationError(f"{context}: expected an object")
        path_id = raw.get("id")
        clock_domain = raw.get("clock_domain")
        if not isinstance(path_id, str) or not path_id:
            raise ValidationError(f"{context}.id: expected a non-empty string")
        if path_id in path_ids:
            raise ValidationError(f"{context}.id: duplicate {path_id!r}")
        path_ids.add(path_id)
        if not isinstance(clock_domain, str) or not clock_domain:
            raise ValidationError(
                f"{context}.clock_domain: expected a non-empty string"
            )
        clock_period = _number(
            raw.get("clock_period_ns"),
            f"{context}.clock_period_ns",
            positive=True,
        )
        slack = _number(raw.get("slack_ns"), f"{context}.slack_ns")
        fixed_delay = _number(
            raw.get("fixed_delay_ns"), f"{context}.fixed_delay_ns"
        )
        if fixed_delay < 0.0:
            raise ValidationError(
                f"{context}.fixed_delay_ns: expected a non-negative number"
            )
        raw_nets = raw.get("cut_nets")
        if (
            not isinstance(raw_nets, list)
            or not raw_nets
            or not all(isinstance(net, str) and net for net in raw_nets)
        ):
            raise ValidationError(
                f"{context}.cut_nets: expected a non-empty string array"
            )
        unknown = sorted(set(raw_nets) - demand_nets)
        if unknown:
            raise ValidationError(
                f"{context}.cut_nets: unknown partition cut nets {unknown}"
            )
        if len(set(raw_nets)) != len(raw_nets):
            raise ValidationError(
                f"{context}.cut_nets: duplicate cut nets are not supported"
            )
        signature = raw.get("cut_signature")
        if signature is None:
            signature = []
            for net in raw_nets:
                demand = demand_by_net[net]
                signature.append(
                    f"{demand['source']}->{','.join(demand['sinks'])}"
                )
        if (
            not isinstance(signature, list)
            or not signature
            or not all(isinstance(item, str) and item for item in signature)
        ):
            raise ValidationError(
                f"{context}.cut_signature: expected a non-empty string array"
            )
        path = {
                "id": path_id,
                "clock_domain": clock_domain,
                "clock_period_ns": clock_period,
                "slack_ns": slack,
                "fixed_delay_ns": fixed_delay,
                "cut_nets": list(raw_nets),
                "cut_signature": list(signature),
            }
        transitions = _normalize_cut_transitions(
            raw.get("cut_transitions"),
            raw_nets,
            demand_by_net,
            f"{context}.cut_transitions",
        )
        if transitions is not None:
            path["cut_transitions"] = transitions
        paths.append(path)

    positive_scale = max(
        (path["slack_ns"] for path in paths if path["slack_ns"] >= 0.0),
        default=1.0,
    )
    # A path exactly on its timing boundary is valid.  Keep its normalized
    # slack at zero without dividing by an all-zero positive scale.
    if positive_scale == 0.0:
        positive_scale = 1.0
    negative_scale = abs(
        min(
            (path["slack_ns"] for path in paths if path["slack_ns"] < 0.0),
            default=-1.0,
        )
    )
    max_period = max(path["clock_period_ns"] for path in paths)
    for path in paths:
        slack = path["slack_ns"]
        if slack >= 0.0:
            normalized = (
                slack
                * path["clock_period_ns"]
                / (positive_scale * max_period)
            )
        else:
            normalized = slack / (negative_scale * path["clock_period_ns"])
        path["normalized_slack"] = normalized

    return {
        "schema": STA_PATHS_SCHEMA,
        "design": value.get("design"),
        "normalization": {
            "positive_slack_scale_ns": positive_scale,
            "negative_slack_scale_ns": negative_scale,
            "max_clock_period_ns": max_period,
        },
        "paths": paths,
    }


def compress_sta_paths(paths_artifact: Mapping[str, Any]) -> Dict[str, Any]:
    """Losslessly compress paths with timing-equivalent ordered cuts.

    Equal cut signatures alone are insufficient across clock domains because
    normalized slack has a different scale.  Requiring the same domain,
    period, source-STA required time, and cut-net sequence makes the
    maximum-fixed-delay representative dominate the other members for every
    possible route delay.
    """

    representatives: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    members: Dict[Tuple[Any, ...], List[str]] = defaultdict(list)
    for path in paths_artifact["paths"]:
        required_time_ns = path["fixed_delay_ns"] + path["slack_ns"]
        signature = (
            path["clock_domain"],
            path["clock_period_ns"],
            required_time_ns,
            tuple(path["cut_signature"]),
            tuple(path["cut_nets"]),
            tuple(
                (item["net"], item["from"], item["to"])
                for item in path.get("cut_transitions", [])
            ),
        )
        members[signature].append(path["id"])
        current = representatives.get(signature)
        key = (
            path["fixed_delay_ns"],
            -path["normalized_slack"],
            path["id"],
        )
        if current is None:
            representatives[signature] = path
        else:
            current_key = (
                current["fixed_delay_ns"],
                -current["normalized_slack"],
                current["id"],
            )
            if key > current_key:
                representatives[signature] = path

    compressed = []
    for signature in sorted(representatives):
        representative = dict(representatives[signature])
        representative["compressed_path_ids"] = sorted(members[signature])
        compressed.append(representative)
    return {
        **dict(paths_artifact),
        "compression": {
            "original_paths": len(paths_artifact["paths"]),
            "compressed_paths": len(compressed),
            "lossless_by": (
                "clock-domain+period+required-time+ordered-cut-signature+"
                "cut-net-sequence+member-sink-transitions/max-fixed-delay"
            ),
        },
        "paths": compressed,
    }


def load_sta_paths(
    path: Path,
    demands: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValidationError("timing paths: expected an object")
    if "compression" in value or "normalization" in value:
        if value.get("schema") != STA_PATHS_SCHEMA:
            raise ValidationError(
                f"timing paths.schema: expected {STA_PATHS_SCHEMA!r}"
            )
        compression = value.get("compression")
        normalization = value.get("normalization")
        paths = value.get("paths")
        if (
            not isinstance(compression, dict)
            or not isinstance(normalization, dict)
            or not isinstance(paths, list)
            or not paths
        ):
            raise ValidationError(
                "normalized timing paths: invalid normalization/compression"
            )
        if compression.get("compressed_paths") != len(paths):
            raise ValidationError(
                "normalized timing paths: compressed path count mismatch"
            )
        original_count = compression.get("original_paths")
        if (
            isinstance(original_count, bool)
            or not isinstance(original_count, int)
            or original_count < len(paths)
        ):
            raise ValidationError(
                "normalized timing paths: invalid original path count"
            )
        demand_by_net = {demand["net"]: demand for demand in demands}
        demand_nets = {demand["net"] for demand in demands}
        positive_scale = _number(
            normalization.get("positive_slack_scale_ns"),
            "timing paths.normalization.positive_slack_scale_ns",
            positive=True,
        )
        negative_scale = _number(
            normalization.get("negative_slack_scale_ns"),
            "timing paths.normalization.negative_slack_scale_ns",
            positive=True,
        )
        max_period = _number(
            normalization.get("max_clock_period_ns"),
            "timing paths.normalization.max_clock_period_ns",
            positive=True,
        )
        ids = set()
        for index, item in enumerate(paths):
            context = f"normalized timing paths.paths[{index}]"
            if not isinstance(item, dict):
                raise ValidationError(f"{context}: expected an object")
            path_id = item.get("id")
            if not isinstance(path_id, str) or not path_id or path_id in ids:
                raise ValidationError(f"{context}.id: invalid or duplicate")
            ids.add(path_id)
            period = _number(
                item.get("clock_period_ns"),
                f"{context}.clock_period_ns",
                positive=True,
            )
            slack = _number(item.get("slack_ns"), f"{context}.slack_ns")
            fixed_delay = _number(
                item.get("fixed_delay_ns"), f"{context}.fixed_delay_ns"
            )
            if fixed_delay < 0.0:
                raise ValidationError(
                    f"{context}.fixed_delay_ns: expected non-negative"
                )
            if (
                not isinstance(item.get("clock_domain"), str)
                or not item["clock_domain"]
            ):
                raise ValidationError(f"{context}.clock_domain: invalid")
            nets = item.get("cut_nets")
            if (
                not isinstance(nets, list)
                or not nets
                or not all(isinstance(net, str) and net for net in nets)
                or len(set(nets)) != len(nets)
                or not set(nets) <= demand_nets
            ):
                raise ValidationError(f"{context}.cut_nets: invalid")
            signature = item.get("cut_signature")
            if (
                not isinstance(signature, list)
                or not signature
                or not all(
                    isinstance(part, str) and part for part in signature
                )
            ):
                raise ValidationError(f"{context}.cut_signature: invalid")
            _normalize_cut_transitions(
                item.get("cut_transitions"),
                nets,
                demand_by_net,
                f"{context}.cut_transitions",
            )
            members = item.get("compressed_path_ids")
            if (
                not isinstance(members, list)
                or not members
                or not all(isinstance(member, str) for member in members)
            ):
                raise ValidationError(
                    f"{context}.compressed_path_ids: invalid"
                )
            expected_normalized = (
                slack * period / (positive_scale * max_period)
                if slack >= 0.0
                else slack / (negative_scale * period)
            )
            if (
                abs(
                    _number(
                        item.get("normalized_slack"),
                        f"{context}.normalized_slack",
                    )
                    - expected_normalized
                )
                > 1.0e-12
            ):
                raise ValidationError(
                    f"{context}.normalized_slack: inconsistent"
                )
        return dict(value)
    return compress_sta_paths(normalize_sta_paths(value, demands))


def _static_predicted_delay(
    source: str,
    sinks: Sequence[str],
    adjacency: Mapping[str, Sequence[Mapping[str, Any]]],
    platform: Platform,
    constraints: Mapping[str, Any],
    distance_cache: Optional[Dict[str, Dict[str, float]]] = None,
) -> float:
    distance = None if distance_cache is None else distance_cache.get(source)
    if distance is None:
        distance = {source: 0.0}
        queue = [(0.0, source)]
        while queue:
            current, node = heapq.heappop(queue)
            if current != distance.get(node):
                continue
            for arc in adjacency.get(node, []):
                candidate = current + route_link_delay_ns(
                    platform,
                    arc["link"],
                    node,
                    arc["to"],
                    constraints,
                )
                if candidate < distance.get(arc["to"], float("inf")):
                    distance[arc["to"]] = candidate
                    heapq.heappush(queue, (candidate, arc["to"]))
        if distance_cache is not None:
            distance_cache[source] = distance
    missing = sorted(set(sinks) - set(distance))
    if missing:
        raise ValidationError(
            f"timing-aware routing: source {source!r} cannot reach {missing}"
        )
    return max(distance[sink] for sink in sinks)


class _PackedNativeDemandSequence(list):
    """Fixed-width native router demands with transient logical projection."""

    def __init__(self) -> None:
        super().__init__()
        self._sources = array("Q")
        self._sink_offsets = array("Q", [0])
        self._sinks = array("Q")
        self._widths = array("Q")
        self._normalized_slacks = array("d")
        self._predicted_delays = array("d")

    def append_record(
        self,
        source: int,
        sinks: Sequence[int],
        width: int,
        normalized_slack: float,
        predicted_delay_ns: float,
    ) -> None:
        self._sources.append(source)
        self._sinks.extend(sinks)
        self._sink_offsets.append(len(self._sinks))
        self._widths.append(width)
        self._normalized_slacks.append(normalized_slack)
        self._predicted_delays.append(predicted_delay_ns)

    def __len__(self) -> int:
        return len(self._sources)

    def _record(self, index: int) -> Dict[str, Any]:
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        start = self._sink_offsets[index]
        end = self._sink_offsets[index + 1]
        return {
            "index": index,
            "source": int(self._sources[index]),
            "sinks": [int(value) for value in self._sinks[start:end]],
            "width": int(self._widths[index]),
            "normalized_slack": float(self._normalized_slacks[index]),
            "predicted_delay_ns": float(self._predicted_delays[index]),
        }

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for index in range(len(self)):
            yield self._record(index)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self._record(item) for item in range(*index.indices(len(self)))]
        return self._record(index)

    def iter_native_records(
        self,
    ) -> Iterator[tuple[int, int, array, int, float, float]]:
        for index in range(len(self)):
            start = self._sink_offsets[index]
            end = self._sink_offsets[index + 1]
            yield (
                index,
                int(self._sources[index]),
                self._sinks[start:end],
                int(self._widths[index]),
                float(self._normalized_slacks[index]),
                float(self._predicted_delays[index]),
            )

    @property
    def payload_bytes(self) -> int:
        return sum(
            len(values) * values.itemsize
            for values in (
                self._sources,
                self._sink_offsets,
                self._sinks,
                self._widths,
                self._normalized_slacks,
                self._predicted_delays,
            )
        )

    def clear(self) -> None:
        del self._sources[:]
        del self._sink_offsets[1:]
        del self._sinks[:]
        del self._widths[:]
        del self._normalized_slacks[:]
        del self._predicted_delays[:]


class _PackedNativePathSequence(list):
    """Fixed-width native timing paths with CSR demand membership."""

    def __init__(self) -> None:
        super().__init__()
        self._periods = array("d")
        self._baseline_slacks = array("d")
        self._fixed_delays = array("d")
        self._demand_offsets = array("Q", [0])
        self._demands = array("Q")

    def append_record(
        self,
        clock_period_ns: float,
        baseline_slack_ns: float,
        fixed_delay_ns: float,
        demands: Sequence[int],
    ) -> None:
        self._periods.append(clock_period_ns)
        self._baseline_slacks.append(baseline_slack_ns)
        self._fixed_delays.append(fixed_delay_ns)
        self._demands.extend(demands)
        self._demand_offsets.append(len(self._demands))

    def __len__(self) -> int:
        return len(self._periods)

    def _record(self, index: int) -> Dict[str, Any]:
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        start = self._demand_offsets[index]
        end = self._demand_offsets[index + 1]
        return {
            "index": index,
            "clock_period_ns": float(self._periods[index]),
            "baseline_slack_ns": float(self._baseline_slacks[index]),
            "fixed_delay_ns": float(self._fixed_delays[index]),
            "demands": [int(value) for value in self._demands[start:end]],
        }

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for index in range(len(self)):
            yield self._record(index)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self._record(item) for item in range(*index.indices(len(self)))]
        return self._record(index)

    def iter_native_records(
        self,
    ) -> Iterator[tuple[int, float, float, float, array]]:
        for index in range(len(self)):
            start = self._demand_offsets[index]
            end = self._demand_offsets[index + 1]
            yield (
                index,
                float(self._periods[index]),
                float(self._baseline_slacks[index]),
                float(self._fixed_delays[index]),
                self._demands[start:end],
            )

    @property
    def payload_bytes(self) -> int:
        return sum(
            len(values) * values.itemsize
            for values in (
                self._periods,
                self._baseline_slacks,
                self._fixed_delays,
                self._demand_offsets,
                self._demands,
            )
        )

    def clear(self) -> None:
        del self._periods[:]
        del self._baseline_slacks[:]
        del self._fixed_delays[:]
        del self._demand_offsets[1:]
        del self._demands[:]


class _PackedNativeRouteSequence(list):
    """Variable-length native routes backed by fixed-width columns.

    Native output is allowed to arrive in any demand order.  Offsets therefore
    point into one append-only arc column instead of relying on CSR arrival
    order.  Logical dictionaries are created only for the record currently
    consumed by validation or RouteStore publication.
    """

    def __init__(self, count: int) -> None:
        super().__init__()
        self._coverage = bytearray(count)
        self._delays = array("d", [0.0]) * count
        self._arc_offsets = array("Q", [0]) * count
        self._arc_counts = array("Q", [0]) * count
        self._arcs = array("Q")
        self._covered = 0

    def __len__(self) -> int:
        return len(self._coverage)

    def covered(self, index: int) -> bool:
        return bool(self._coverage[index])

    @property
    def complete(self) -> bool:
        return self._covered == len(self)

    def set_record(
        self, index: int, delay_ns: float, arcs: Sequence[int]
    ) -> None:
        if index < 0 or index >= len(self) or self._coverage[index]:
            raise EmuFlowError("TLR router returned duplicate/unknown route")
        offset = len(self._arcs)
        self._arcs.extend(arcs)
        self._arc_offsets[index] = offset
        self._arc_counts[index] = len(arcs)
        self._delays[index] = delay_ns
        self._coverage[index] = 1
        self._covered += 1

    def _record(self, index: int) -> Dict[str, Any]:
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        if not self._coverage[index]:
            raise EmuFlowError("TLR router route coverage is incomplete")
        offset = self._arc_offsets[index]
        end = offset + self._arc_counts[index]
        return {
            "max_delay_ns": float(self._delays[index]),
            "arcs": [int(value) for value in self._arcs[offset:end]],
        }

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for index in range(len(self)):
            yield self._record(index)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self._record(item) for item in range(*index.indices(len(self)))]
        return self._record(index)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Sequence) and not isinstance(other, (str, bytes)):
            return len(self) == len(other) and all(
                left == right for left, right in zip(self, other)
            )
        return False

    def __ne__(self, other: object) -> bool:
        return not self == other

    @property
    def payload_bytes(self) -> int:
        return len(self._coverage) + sum(
            len(values) * values.itemsize
            for values in (
                self._delays,
                self._arc_offsets,
                self._arc_counts,
                self._arcs,
            )
        )


class _PackedNativeSelectionSequence(list):
    """One-byte native candidate selection ordinals."""

    _MISSING = 0xFF

    def __init__(self, count: int) -> None:
        super().__init__()
        self._ordinals = bytearray([self._MISSING]) * count
        self._covered = 0

    def __len__(self) -> int:
        return len(self._ordinals)

    def covered(self, index: int) -> bool:
        return self._ordinals[index] != self._MISSING

    @property
    def complete(self) -> bool:
        return self._covered == len(self)

    def set_record(self, index: int, generator: str) -> None:
        if index < 0 or index >= len(self) or self.covered(index):
            raise EmuFlowError(
                "TLR router returned duplicate/unknown selection"
            )
        try:
            ordinal = ROUTE_CANDIDATE_GENERATORS.index(generator)
        except ValueError as error:
            raise EmuFlowError(
                "TLR router returned duplicate/unknown selection"
            ) from error
        self._ordinals[index] = ordinal
        self._covered += 1

    def __iter__(self) -> Iterator[str]:
        for index in range(len(self)):
            yield self[index]

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        ordinal = self._ordinals[index]
        if ordinal == self._MISSING:
            raise EmuFlowError("TLR router selection coverage is incomplete")
        return ROUTE_CANDIDATE_GENERATORS[ordinal]

    @property
    def payload_bytes(self) -> int:
        return len(self._ordinals)


class _PackedNativeTimingSequence(list):
    """Fixed-width native path results without transient result dictionaries."""

    def __init__(self, count: int) -> None:
        super().__init__()
        self._coverage = bytearray(count)
        self._delays = array("d", [0.0]) * count
        self._slacks = array("d", [0.0]) * count
        self._normalized_slacks = array("d", [0.0]) * count
        self._covered = 0

    def __len__(self) -> int:
        return len(self._coverage)

    def covered(self, index: int) -> bool:
        return bool(self._coverage[index])

    @property
    def complete(self) -> bool:
        return self._covered == len(self)

    def set_record(
        self,
        index: int,
        delay_ns: float,
        slack_ns: float,
        normalized_slack: float,
    ) -> None:
        if index < 0 or index >= len(self) or self._coverage[index]:
            raise EmuFlowError(
                "TLR router returned duplicate/unknown timing path"
            )
        self._delays[index] = delay_ns
        self._slacks[index] = slack_ns
        self._normalized_slacks[index] = normalized_slack
        self._coverage[index] = 1
        self._covered += 1

    def _record(self, index: int) -> Dict[str, float]:
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        if not self._coverage[index]:
            raise EmuFlowError("TLR router timing coverage is incomplete")
        return {
            "delay_ns": float(self._delays[index]),
            "slack_ns": float(self._slacks[index]),
            "normalized_slack": float(self._normalized_slacks[index]),
        }

    def __iter__(self) -> Iterator[Dict[str, float]]:
        for index in range(len(self)):
            yield self._record(index)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self._record(item) for item in range(*index.indices(len(self)))]
        return self._record(index)

    @property
    def payload_bytes(self) -> int:
        return len(self._coverage) + sum(
            len(values) * values.itemsize
            for values in (
                self._delays,
                self._slacks,
                self._normalized_slacks,
            )
        )


class _ProjectedSystemRouteSequence(list):
    """Repeatable public route cursor over demands and packed native results."""

    def __init__(
        self,
        demands: Sequence[Mapping[str, Any]],
        native_routes: _PackedNativeRouteSequence,
        arc_keys: Sequence[ArcKey],
        latency_cycles: array,
        native_result_path: Optional[Path] = None,
        native_result_owner: Any = None,
        native_result_source=None,
    ) -> None:
        super().__init__()
        self._demands = demands
        self._native_routes = native_routes
        self._arc_keys = arc_keys
        self._latency_cycles = latency_cycles
        self._native_result_path = native_result_path
        self._native_result_owner = native_result_owner
        self._native_result_source = native_result_source
        if native_result_path is not None and native_result_source is None:
            self._native_result_source = bind_store_source(
                native_result_path, "tlr-output-v1"
            )

    def bound_store_sources(self):
        if self._native_result_source is None:
            return ()
        if self._native_result_path is None:
            raise ValidationError("native route result lease is unavailable")
        require_store_sources_unchanged((self._native_result_source,))
        return (self._native_result_source,)

    @property
    def has_native_route_relations(self) -> bool:
        return (
            self._native_result_path is not None
            and self._native_result_path.is_file()
        )

    def publish_native_route_relations(
        self, connection: sqlite3.Connection
    ) -> None:
        """Copy checked native delay/arc relations into RouteStore by SQL.

        The private result is held by this sequence until publication.  Route
        identities and assignment-owned dependency metadata still come from
        the public demand contract; only router-owned facts are transferred.
        """

        self.bound_store_sources()
        path = self._native_result_path
        if path is None or not path.is_file():
            raise ValidationError("native route result lease is unavailable")
        uri = immutable_sqlite_uri(path)
        connection.execute(
            "ATTACH DATABASE ? AS native_route_result", (uri,)
        )
        connection.execute("PRAGMA native_route_result.cache_size=-1024")
        if (
            connection.execute(
                "PRAGMA native_route_result.application_id"
            ).fetchone()[0]
            != _NATIVE_ROUTE_RESULT_APPLICATION_ID
            or connection.execute(
                "PRAGMA native_route_result.user_version"
            ).fetchone()[0]
            != _NATIVE_ROUTE_RESULT_USER_VERSION
        ):
            raise ValidationError("native route result envelope changed")
        expected_tables = {
            "direction_locks",
            "routes",
            "route_arcs",
            "candidate_bundles",
            "selections",
            "timing_paths",
            "metrics",
        }
        objects = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT type,name FROM native_route_result.sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%'"
            )
        }
        if objects != {("table", table) for table in expected_tables}:
            raise ValidationError("native route result schema changed")
        count, minimum, maximum = connection.execute(
            "SELECT count(*),min(demand_index),max(demand_index) "
            "FROM native_route_result.routes"
        ).fetchone()
        if (
            count != len(self)
            or minimum != (0 if len(self) else None)
            or maximum != (len(self) - 1 if len(self) else None)
        ):
            raise ValidationError("native route result coverage changed")
        if (
            connection.execute("SELECT count(*) FROM routes").fetchone()[0]
            or connection.execute("SELECT count(*) FROM route_edges").fetchone()[0]
        ):
            raise ValidationError("RouteStore native relations were already populated")
        metadata_count = connection.execute(
            "SELECT count(*) FROM native_route_metadata"
        ).fetchone()[0]
        if metadata_count != len(self):
            raise ValidationError("native route metadata coverage changed")
        connection.execute(
            "CREATE TEMP TABLE native_route_arc_catalog("
            "arc_index INTEGER PRIMARY KEY,link_id TEXT NOT NULL,"
            "source_fpga TEXT NOT NULL,sink_fpga TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO native_route_arc_catalog VALUES(?,?,?,?)",
            (
                (index, key[0], key[1], key[2])
                for index, key in enumerate(self._arc_keys)
            ),
        )
        malformed = connection.execute(
            "SELECT a.demand_index FROM native_route_result.route_arcs a "
            "LEFT JOIN native_route_result.routes r "
            "ON r.demand_index=a.demand_index "
            "LEFT JOIN native_route_arc_catalog c "
            "ON c.arc_index=a.arc_index "
            "WHERE r.demand_index IS NULL OR c.arc_index IS NULL "
            "UNION ALL SELECT demand_index "
            "FROM native_route_result.route_arcs GROUP BY demand_index "
            "HAVING min(seq)!=0 OR max(seq)!=count(*)-1 LIMIT 1"
        ).fetchone()
        if malformed is not None:
            raise ValidationError("native route arc relation changed")
        connection.execute(
            "INSERT INTO routes("
            "seq,demand_id,net_id,source_fpga,width_bits,transport_round,"
            "cut_class,dependency_level,combinational_dependency_depth,"
            "max_latency_cycles,predicted_max_delay_ns) "
            "SELECT m.seq,m.demand_id,m.net_id,m.source_fpga,m.width_bits,"
            "m.transport_round,m.cut_class,m.dependency_level,"
            "m.combinational_dependency_depth,m.max_latency_cycles,"
            "n.max_delay_ns FROM native_route_metadata m "
            "JOIN native_route_result.routes n ON n.demand_index=m.seq "
            "ORDER BY m.seq"
        )
        selection_requests = connection.execute(
            "SELECT 1 FROM temp.sqlite_master "
            "WHERE type='table' AND name='native_route_selection_requests'"
        ).fetchone()
        if selection_requests is not None:
            selection_count = connection.execute(
                "SELECT count(*) FROM native_route_selection_requests"
            ).fetchone()[0]
            malformed_selection = connection.execute(
                "SELECT q.seq FROM native_route_selection_requests q "
                "LEFT JOIN native_route_result.selections s "
                "ON s.demand_index=q.seq "
                "WHERE s.demand_index IS NULL OR s.generator NOT BETWEEN 0 AND 5 "
                "LIMIT 1"
            ).fetchone()
            if selection_count != len(self) or malformed_selection is not None:
                raise ValidationError("native master-selection coverage changed")
            connection.execute(
                "INSERT INTO route_master_selections(seq,demand_id,generator) "
                "SELECT q.seq,q.demand_id,CASE s.generator "
                "WHEN 0 THEN 'shortest-path-tree' "
                "WHEN 1 THEN 'delay-demand-balanced' "
                "WHEN 2 THEN 'nearest-terminal-steiner' "
                "WHEN 3 THEN 'directed-metric-closure' "
                "WHEN 4 THEN 'shallow-light-tree' "
                "WHEN 5 THEN 'adaptive-hop-tree' END "
                "FROM native_route_selection_requests q "
                "JOIN native_route_result.selections s "
                "ON s.demand_index=q.seq ORDER BY q.seq"
            )
        connection.execute(
            "INSERT INTO route_edges(route_seq,seq,link_id,source_fpga,sink_fpga) "
            "SELECT a.demand_index,a.seq,c.link_id,c.source_fpga,c.sink_fpga "
            "FROM native_route_result.route_arcs a "
            "JOIN native_route_arc_catalog c USING(arc_index) "
            "ORDER BY a.demand_index,a.seq"
        )
        expected_edges = connection.execute(
            "SELECT count(*) FROM native_route_result.route_arcs"
        ).fetchone()[0]
        observed_edges = connection.execute(
            "SELECT count(*) FROM route_edges"
        ).fetchone()[0]
        missing_delay = connection.execute(
            "SELECT seq FROM routes WHERE predicted_max_delay_ns IS NULL LIMIT 1"
        ).fetchone()
        if observed_edges != expected_edges or missing_delay is not None:
            raise ValidationError("native RouteStore publication is incomplete")

    def release_native_route_relations(self) -> None:
        owner = self._native_result_owner
        self._native_result_owner = None
        self._native_result_path = None
        if owner is not None:
            owner.cleanup()

    def iter_route_storage_metadata(
        self,
    ) -> Iterator[tuple[Mapping[str, Any], int]]:
        """Yield assignment-owned route fields without rebuilding edge trees."""

        for index, demand in enumerate(self._demands):
            yield demand, int(self._latency_cycles[index])

    def __len__(self) -> int:
        return len(self._demands)

    def _record(self, index: int) -> Dict[str, Any]:
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        demand = self._demands[index]
        native = self._native_routes[index]
        return {
            **demand,
            "tree_edges": [
                {"link": key[0], "from": key[1], "to": key[2]}
                for key in (self._arc_keys[item] for item in native["arcs"])
            ],
            "max_latency_cycles": int(self._latency_cycles[index]),
            "predicted_max_delay_ns": native["max_delay_ns"],
        }

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for index in range(len(self)):
            yield self._record(index)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self._record(item) for item in range(*index.indices(len(self)))]
        return self._record(index)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Sequence) and not isinstance(other, (str, bytes)):
            return len(self) == len(other) and all(
                left == right for left, right in zip(self, other)
            )
        return False

    def __ne__(self, other: object) -> bool:
        return not self == other

    def __deepcopy__(self, memo: Dict[int, Any]) -> list[Dict[str, Any]]:
        return [copy.deepcopy(record, memo) for record in self]


class _ProjectedMasterSelectionSequence(list):
    """Repeatable master-selection cursor without route-scale dictionaries."""

    def __init__(
        self,
        demands: Sequence[Mapping[str, Any]],
        selections: _PackedNativeSelectionSequence,
    ) -> None:
        super().__init__()
        self._demands = demands
        self._selections = selections

    def __len__(self) -> int:
        return len(self._demands)

    def _record(self, index: int) -> Dict[str, str]:
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        return {
            "demand": str(self._demands[index]["id"]),
            "generator": self._selections[index],
        }

    def __iter__(self) -> Iterator[Dict[str, str]]:
        for index in range(len(self)):
            yield self._record(index)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self._record(item) for item in range(*index.indices(len(self)))]
        return self._record(index)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, list):
            return NotImplemented
        return len(self) == len(other) and all(a == b for a, b in zip(self, other))

    def __ne__(self, other: object) -> bool:
        equal = self.__eq__(other)
        return NotImplemented if equal is NotImplemented else not equal

    def __deepcopy__(self, memo: Dict[int, Any]) -> list[Dict[str, str]]:
        return [copy.deepcopy(record, memo) for record in self]


class _ProjectedTimingRecordSequence(list):
    """Repeatable timing cursor joining packed native results by demand ordinal."""

    def __init__(
        self,
        paths: Sequence[Mapping[str, Any]],
        native_paths: _PackedNativePathSequence,
        native_timing: _PackedNativeTimingSequence,
        native_routes: _PackedNativeRouteSequence,
        arc_keys: Sequence[ArcKey],
        physical_arc_index: Mapping[ArcKey, int],
    ) -> None:
        super().__init__()
        self._paths = paths
        self._native_paths = native_paths
        self._native_timing = native_timing
        self._native_routes = native_routes
        self._arc_keys = arc_keys
        self._physical_arc_index = physical_arc_index

    def __len__(self) -> int:
        return len(self._paths)

    def _record(self, index: int) -> Dict[str, Any]:
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        path = self._paths[index]
        demand_indices = self._native_paths[index]["demands"]
        signature = [
            arc
            for demand_index in demand_indices
            for arc in sorted(
                self._physical_arc_index[
                    self._arc_keys[native_arc]
                ]
                for native_arc in self._native_routes[demand_index]["arcs"]
            )
        ]
        return {
            "path": path["id"],
            "clock_domain": path["clock_domain"],
            "clock_period_ns": path["clock_period_ns"],
            "required_time_ns": path["fixed_delay_ns"] + path["slack_ns"],
            "fixed_delay_ns": path["fixed_delay_ns"],
            "cut_nets": path["cut_nets"],
            "cut_signature": path["cut_signature"],
            "compressed_path_ids": path["compressed_path_ids"],
            **(
                {"cut_transitions": path["cut_transitions"]}
                if "cut_transitions" in path
                else {}
            ),
            **self._native_timing[index],
            "route_signature": ",".join(str(arc) for arc in signature) or "-",
        }

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for index in range(len(self)):
            yield self._record(index)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self._record(item) for item in range(*index.indices(len(self)))]
        return self._record(index)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Sequence) and not isinstance(other, (str, bytes)):
            return len(self) == len(other) and all(
                left == right for left, right in zip(self, other)
            )
        return False

    def __ne__(self, other: object) -> bool:
        return not self == other

    def __deepcopy__(self, memo: Dict[int, Any]) -> list[Dict[str, Any]]:
        return [copy.deepcopy(record, memo) for record in self]


def _prepare_native_model(
    assignment: Mapping[str, Any],
    platform: Platform,
    constraints: Mapping[str, Any],
    timing_paths: Optional[Mapping[str, Any]],
) -> Tuple[List[str], Dict[str, Any]]:
    demands = demands_from_assignment(assignment, platform)
    exact_dependency_schedule = assignment_has_static_exact_contract(
        assignment
    )
    adjacency, arcs_by_key, capacities = build_directed_graph(
        platform,
        constraints,
        reserve_arrival_and_commit=exact_dependency_schedule,
    )
    physical_nodes = sorted(fpga.id for fpga in platform.fpgas)
    active_rounds = sorted(
        {int(demand.get("transport_round", 0)) for demand in demands}
    )
    # A global round barrier is only the conservative fallback used by the
    # legacy registered-boundary transport, which has no explicit dependency
    # graph. Static Exact carries the real per-cut dependency DAG; splitting
    # its physical routing capacity into two synthetic windows would reject
    # legal schedules and bias routing based on a diagnostic label.
    layered_rounds = (
        not exact_dependency_schedule
        and active_rounds == [0, 1]
    )
    round_boundary = (
        _select_round_boundary(
            demands, adjacency, capacities, platform, constraints
        )
        if layered_rounds
        else None
    )
    node_layers: List[Optional[int]] = (
        list(active_rounds) if layered_rounds else [None]
    )
    nodes = [
        f"round{round_id}:{node}" if round_id is not None else node
        for round_id in node_layers
        for node in physical_nodes
    ]
    node_index = {
        (round_id, node): index
        for index, (round_id, node) in enumerate(
            (round_id, node)
            for round_id in node_layers
            for node in physical_nodes
        )
    }
    physical_capacity_keys = sorted(capacities)
    native_capacity_keys = [
        (key, round_id)
        for round_id in node_layers
        for key in physical_capacity_keys
    ]
    capacity_index = {
        key: index for index, key in enumerate(native_capacity_keys)
    }
    link_index = {
        link.id: index
        for index, link in enumerate(sorted(platform.links, key=lambda x: x.id))
    }

    physical_arc_keys = sorted(arcs_by_key)
    ordered_arc_keys = [
        (round_id, key)
        for round_id in node_layers
        for key in physical_arc_keys
    ]
    arc_index = {key: index for index, key in enumerate(ordered_arc_keys)}
    direction_group_by_link = {}
    for link in sorted(platform.links, key=lambda item: item.id):
        if link.direction == "half_duplex":
            direction_group_by_link[link.id] = len(direction_group_by_link)
    sll_links = set(constraints.get("sll_links", []))
    link_by_id = {link.id: link for link in platform.links}
    native_arcs = []
    for index, (round_id, key) in enumerate(ordered_arc_keys):
        arc = arcs_by_key[key]
        link = link_by_id[arc["link"]]
        opposite_key = (
            round_id,
            _arc_key(link.id, arc["to"], arc["from"]),
        )
        capacity_bits = capacities[arc["capacity_key"]]["capacity_bits"]
        if layered_rounds and link.id not in sll_links:
            capacity_bits = (
                link.transport_bits_per_cycle_per_direction
                * _round_available_slots(
                    int(constraints["frame_slots"]),
                    int(round_boundary),
                    link.latency_cycles,
                    int(round_id),
                )
            )
        native_arcs.append(
            {
                "index": index,
                "link_index": link_index[link.id],
                "from": node_index[(round_id, arc["from"])],
                "to": node_index[(round_id, arc["to"])],
                "capacity_domain": capacity_index[
                    (arc["capacity_key"], round_id)
                ],
                "direction_group": direction_group_by_link.get(link.id, -1),
                "opposite_arc": arc_index.get(opposite_key, -1),
                "capacity": capacity_bits,
                "lanes": link.transport_bits_per_cycle_per_direction,
                "delay_ns": route_link_delay_ns(
                    platform,
                    link.id,
                    arc["from"],
                    arc["to"],
                    constraints,
                ),
                "beta_ns": route_link_delay_coefficients_ns(
                    platform, link.id, arc["from"], arc["to"], constraints
                )[1],
                "is_sll": link.id in sll_links,
                "bank_count": arc.get("bank_count", 0),
                "wires_per_bank": arc.get("wires_per_bank", 0),
            }
        )

    normalized_timing = timing_paths or {
        "normalization": {
            "positive_slack_scale_ns": 1.0,
            "negative_slack_scale_ns": 1.0,
            "max_clock_period_ns": 1.0,
        },
        "paths": [],
    }
    path_by_net: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for path in normalized_timing["paths"]:
        for net in path["cut_nets"]:
            path_by_net[net].append(path)
    native_demands = _PackedNativeDemandSequence()
    demand_index = {demand["net"]: index for index, demand in enumerate(demands)}
    static_distance_cache: Dict[str, Dict[str, float]] = {}
    for index, demand in enumerate(demands):
        related = path_by_net.get(demand["net"], [])
        normalized_slack = min(
            (path["normalized_slack"] for path in related), default=1.0
        )
        demand_round = (
            int(demand.get("transport_round", 0))
            if layered_rounds
            else None
        )
        native_demands.append_record(
            node_index[(demand_round, demand["source"])],
            [
                node_index[(demand_round, sink)]
                for sink in demand["sinks"]
            ],
            demand["width_bits"],
            normalized_slack,
            _static_predicted_delay(
                demand["source"],
                demand["sinks"],
                adjacency,
                platform,
                constraints,
                static_distance_cache,
            ),
        )

    native_paths = _PackedNativePathSequence()
    for path in normalized_timing["paths"]:
        native_paths.append_record(
            path["clock_period_ns"],
            path["slack_ns"],
            path["fixed_delay_ns"],
            [demand_index[net] for net in path["cut_nets"]],
        )
    return nodes, {
        "demands": demands,
        "arc_keys": [key for _round_id, key in ordered_arc_keys],
        "arc_rounds": [round_id for round_id, _key in ordered_arc_keys],
        "arcs": native_arcs,
        "paths": native_paths,
        "native_demands": native_demands,
        "capacity_keys": [
            (
                key
                if round_id is None
                else f"{key}@transport-round-{round_id}"
            )
            for key, round_id in native_capacity_keys
        ],
        "physical_capacity_keys": physical_capacity_keys,
        "active_rounds": active_rounds,
        "round_boundary": round_boundary,
        "normalization": normalized_timing["normalization"],
    }


def _validate_single_node_cut_capacity(
    nodes: Sequence[str], model: Mapping[str, Any]
) -> None:
    """Reject provably impossible routing inputs before native search.

    Every bit sourced at a node must cross one of that node's outgoing
    capacity domains, and every bit consumed at a node must cross one of its
    incoming domains.  These two single-node cuts are only necessary (not
    sufficient) conditions for a multicommodity route, but they cheaply catch
    grossly undersized platform selections and provide a useful certificate
    instead of allowing a candidate heuristic to fail after a long search.
    """

    capacities: Dict[int, int] = {}
    outgoing_domains: Dict[int, set[int]] = defaultdict(set)
    incoming_domains: Dict[int, set[int]] = defaultdict(set)
    for arc in model["arcs"]:
        domain = int(arc["capacity_domain"])
        capacity = int(arc["capacity"])
        previous = capacities.setdefault(domain, capacity)
        if previous != capacity:
            raise EmuFlowError(
                "routing capacity domain has inconsistent capacity"
            )
        outgoing_domains[int(arc["from"])].add(domain)
        incoming_domains[int(arc["to"])].add(domain)

    sourced = [0] * len(nodes)
    consumed = [0] * len(nodes)
    native_demands = model["native_demands"]
    typed_cursor = getattr(native_demands, "iter_native_records", None)
    demand_records = (
        typed_cursor()
        if callable(typed_cursor)
        else (
            (
                demand["index"],
                demand["source"],
                demand["sinks"],
                demand["width"],
                demand["normalized_slack"],
                demand["predicted_delay_ns"],
            )
            for demand in native_demands
        )
    )
    for _index, source, sinks, width, _slack, _delay in demand_records:
        source = int(source)
        width = int(width)
        sourced[source] += width
        for sink in set(int(item) for item in sinks):
            consumed[sink] += width

    violations = []
    for index, node in enumerate(nodes):
        outgoing = sum(
            capacities[domain] for domain in outgoing_domains[index]
        )
        incoming = sum(
            capacities[domain] for domain in incoming_domains[index]
        )
        if sourced[index] > outgoing:
            violations.append(
                f"{node} outbound demand {sourced[index]} > cut capacity "
                f"{outgoing}"
            )
        if consumed[index] > incoming:
            violations.append(
                f"{node} inbound demand {consumed[index]} > cut capacity "
                f"{incoming}"
            )
    if violations:
        raise EmuFlowError(
            "routing infeasible by single-node cut certificate: "
            + "; ".join(violations)
        )


def _write_native_input(
    path: Path,
    node_count: int,
    model: Mapping[str, Any],
    constraints: Mapping[str, Any],
    provider: str,
    candidate_workers: int = 1,
) -> None:
    normalization = model["normalization"]
    with path.open("w", encoding="utf-8") as stream:
        if provider != GLOBAL_CANDIDATE_PROVIDER:
            raise ValueError(f"unsupported Phase 4 routing provider {provider!r}")
        stream.write("EMUFLOW_GLOBAL_ROUTE_INPUT_V1\n")
        stream.write(
            "PARAM "
            f"{node_count} "
            f"{constraints['max_iterations']} "
            f"{constraints.get('reroute_rounds', 8)} "
            f"{constraints.get('lambda_load', 2.0):.17g} "
            f"{constraints.get('lambda_timing', 4.0):.17g} "
            f"{constraints.get('lambda_history', 1.0):.17g} "
            f"{constraints.get('lambda_tdm', 0.1):.17g} "
            f"{constraints.get('tdm_ratio_quantum', 8)} "
            f"{constraints['tdm_max_ratio']} "
            f"{normalization['positive_slack_scale_ns']:.17g} "
            f"{normalization['negative_slack_scale_ns']:.17g} "
            f"{normalization['max_clock_period_ns']:.17g} "
            f"{int(constraints.get('tree_edge_sum_tdm', False))} "
            f"{constraints.get('tdm_min_ratio', 1)} "
            f"{int(constraints.get('hard_sll_capacity', False))} "
            f"{constraints.get('max_route_hops') or 0} "
            f"{candidate_workers}\n"
        )
        for arc in model["arcs"]:
            stream.write(
                "ARC "
                f"{arc['index']} {arc['link_index']} {arc['from']} {arc['to']} "
                f"{arc['capacity_domain']} {arc['direction_group']} "
                f"{arc['opposite_arc']} {arc['capacity']} "
                f"{arc['lanes']} {arc['delay_ns']:.17g} "
                f"{arc['beta_ns']:.17g} {int(arc['is_sll'])} "
                f"{arc['bank_count']} {arc['wires_per_bank']}\n"
            )
        native_demands = model["native_demands"]
        typed_demands = getattr(native_demands, "iter_native_records", None)
        demand_records = (
            typed_demands()
            if callable(typed_demands)
            else (
                (
                    demand["index"],
                    demand["source"],
                    demand["sinks"],
                    demand["width"],
                    demand["normalized_slack"],
                    demand["predicted_delay_ns"],
                )
                for demand in native_demands
            )
        )
        for (
            demand_index,
            source,
            demand_sinks,
            width,
            normalized_slack,
            predicted_delay,
        ) in demand_records:
            sinks = ",".join(str(sink) for sink in demand_sinks)
            stream.write(
                "DEMAND "
                f"{demand_index} {source} {sinks} "
                f"{width} {normalized_slack:.17g} "
                f"{predicted_delay:.17g}\n"
            )
        native_paths = model["paths"]
        typed_paths = getattr(native_paths, "iter_native_records", None)
        path_records = (
            typed_paths()
            if callable(typed_paths)
            else (
                (
                    timing_path["index"],
                    timing_path["clock_period_ns"],
                    timing_path["baseline_slack_ns"],
                    timing_path["fixed_delay_ns"],
                    timing_path["demands"],
                )
                for timing_path in native_paths
            )
        )
        for (
            path_index,
            clock_period,
            baseline_slack,
            fixed_delay,
            path_demands,
        ) in path_records:
            demands = ",".join(str(item) for item in path_demands)
            stream.write(
                "PATH "
                f"{path_index} "
                f"{clock_period:.17g} "
                f"{baseline_slack:.17g} "
                f"{fixed_delay:.17g} {demands}\n"
            )


def _validate_compact_native_candidate(
    demand_index: int,
    arc_indices: Sequence[int],
    reported_delay: float,
    model: Mapping[str, Any],
    direction_locks: Mapping[int, int],
    max_route_hops: Optional[int],
) -> tuple[int, int]:
    """Validate one candidate and return ``(max_hops, domain_bitset)``."""

    if demand_index < 0 or demand_index >= len(model["native_demands"]):
        raise EmuFlowError("TLR router returned an unknown candidate demand")
    demand = model["native_demands"][demand_index]
    graph: Dict[int, List[tuple[int, float]]] = defaultdict(list)
    indegree: Dict[int, int] = defaultdict(int)
    edge_nodes = set()
    seen_arcs = set()
    domain_mask = 0
    for arc_index in arc_indices:
        if (
            arc_index < 0
            or arc_index >= len(model["arcs"])
            or arc_index in seen_arcs
        ):
            raise EmuFlowError(
                "TLR router returned an invalid/duplicate candidate arc"
            )
        seen_arcs.add(arc_index)
        arc = model["arcs"][arc_index]
        source = int(arc["from"])
        sink = int(arc["to"])
        graph[source].append((sink, float(arc["delay_ns"])))
        indegree[sink] += 1
        edge_nodes.update((source, sink))
        domain_mask |= 1 << int(arc["capacity_domain"])
        if int(arc["bank_count"]) > 0:
            opposite = int(arc["opposite_arc"])
            if opposite < 0 or opposite >= len(model["arcs"]):
                raise EmuFlowError(
                    "TLR router candidate bank arc lacks its opposite"
                )
            domain_mask |= 1 << int(
                model["arcs"][opposite]["capacity_domain"]
            )
        group = int(arc["direction_group"])
        if group >= 0:
            locked_arc = direction_locks.get(group)
            if locked_arc is None or (
                model["arc_keys"][locked_arc]
                != model["arc_keys"][arc_index]
            ):
                raise EmuFlowError(
                    "TLR router candidate violates a direction lock"
                )

    source = int(demand["source"])
    reachable = {source}
    delay = {source: 0.0}
    hops = {source: 0}
    queue = deque([source])
    while queue:
        node = queue.popleft()
        for sink, arc_delay in sorted(graph.get(node, [])):
            if sink in reachable:
                raise EmuFlowError(
                    "TLR router candidate contains a cycle or multiple path"
                )
            reachable.add(sink)
            delay[sink] = delay[node] + arc_delay
            hops[sink] = hops[node] + 1
            queue.append(sink)
    if not edge_nodes <= reachable:
        raise EmuFlowError(
            "TLR router candidate has edges disconnected from its source"
        )
    sinks = {int(item) for item in demand["sinks"]}
    if not sinks <= reachable:
        raise EmuFlowError("TLR router candidate does not cover every sink")
    if any(node != source and count != 1 for node, count in indegree.items()):
        raise EmuFlowError("TLR router candidate is not an arborescence")
    maximum_hops = max((hops[sink] for sink in sinks), default=0)
    if max_route_hops is not None and maximum_hops > max_route_hops:
        raise EmuFlowError("TLR router candidate exceeds maximum route hops")
    predicted_delay = max((delay[sink] for sink in sinks), default=0.0)
    if (
        not math.isfinite(reported_delay)
        or abs(reported_delay - predicted_delay) > 1.0e-9
    ):
        raise EmuFlowError(
            "TLR router candidate delay was not independently reconstructed"
        )
    return maximum_hops, domain_mask


def _finish_parsed_native_output(
    *,
    routes: _PackedNativeRouteSequence,
    candidates: Dict[int, Dict[str, Any]],
    selections: _PackedNativeSelectionSequence,
    locks: Dict[int, int],
    timing: _PackedNativeTimingSequence,
    metrics: Dict[str, Any],
    candidate_coverage: bytearray,
    candidate_domains: List[int],
    candidate_counts: List[int],
    candidate_max_hops: int,
    compact_candidates: bool,
) -> Dict[str, Any]:
    if not routes.complete:
        raise EmuFlowError("TLR router did not return exact demand coverage")
    if not timing.complete:
        raise EmuFlowError(
            "TLR router did not return exact timing-path coverage"
        )
    expected_metrics = {
        "iterations",
        "accepted_reroutes",
        "rolled_back_reroutes",
        "baseline_candidate_feasible",
        "balanced_candidate_feasible",
        "selected_delay_demand_balanced",
        "worst_slack_ns",
        "worst_normalized_slack",
        "estimated_worst_tdm_slack_ns",
        "estimated_worst_tdm_normalized_slack",
        "estimated_max_tdm_ratio",
        "max_utilization",
        "total_link_bit_hops",
    }
    generator_index = {
        generator: index
        for index, generator in enumerate(ROUTE_CANDIDATE_GENERATORS)
    }
    expected_metrics.update(
        {
            "steiner_candidate_feasible",
            "metric_closure_candidate_feasible",
            "shallow_light_candidate_feasible",
            "adaptive_hop_candidate_feasible",
            "master_rounds",
            "master_switches",
            "master_exact",
            "candidate_workers",
            "parallel_candidate_tasks",
            "reroute_conflict_batches",
            "maximum_parallel_batch",
            "parallel_reroute_tasks",
        }
    )
    refined_bit = 1 << generator_index["refined-final"]
    if any(not (coverage & refined_bit) for coverage in candidate_coverage):
        raise EmuFlowError(
            "TLR router did not return each selected route as a candidate"
        )
    if not selections.complete:
        raise EmuFlowError(
            "TLR router master selection coverage is not exact"
        )
    for demand_index, selection in enumerate(selections):
        if (
            selection not in ROUTE_MASTER_GENERATORS
            or not (
                candidate_coverage[demand_index]
                & (1 << generator_index[selection])
            )
        ):
            raise EmuFlowError(
                "TLR router selected an unavailable/non-master candidate"
            )
    if set(metrics) != expected_metrics:
        raise EmuFlowError("TLR router returned incomplete metric coverage")
    return {
        "routes": routes,
        "candidates": candidates,
        "selections": selections,
        "direction_locks": locks,
        "timing": timing,
        "metrics": metrics,
        "compact_candidate_summary": (
            {
                "domains_by_demand": candidate_domains,
                "candidate_counts": candidate_counts,
                "max_route_hops_observed": candidate_max_hops,
            }
            if compact_candidates
            else None
        ),
    }


def _parse_native_output_sqlite(
    path: Path,
    model: Mapping[str, Any],
    *,
    compact_candidates: bool,
    max_route_hops: Optional[int],
) -> Dict[str, Any]:
    demand_count = len(model["demands"])
    path_count = len(model["paths"])
    routes = _PackedNativeRouteSequence(demand_count)
    candidates: Dict[int, Dict[str, Any]] = defaultdict(dict)
    selections = _PackedNativeSelectionSequence(demand_count)
    locks: Dict[int, int] = {}
    timing = _PackedNativeTimingSequence(path_count)
    candidate_coverage = bytearray(demand_count)
    candidate_domains = [0] * demand_count
    candidate_counts = [0] * len(ROUTE_CANDIDATE_GENERATORS)
    candidate_max_hops = 0
    metrics: Dict[str, Any] = {}
    expected_tables = {
        "direction_locks": (
            ("group_index", "INTEGER", 0, 1),
            ("arc_index", "INTEGER", 1, 0),
        ),
        "routes": (
            ("demand_index", "INTEGER", 0, 1),
            ("max_delay_ns", "REAL", 1, 0),
        ),
        "route_arcs": (
            ("demand_index", "INTEGER", 1, 1),
            ("seq", "INTEGER", 1, 2),
            ("arc_index", "INTEGER", 1, 0),
        ),
        "candidate_bundles": (
            ("demand_index", "INTEGER", 0, 1),
            ("record_count", "INTEGER", 1, 0),
            ("records", "BLOB", 1, 0),
        ),
        "selections": (
            ("demand_index", "INTEGER", 0, 1),
            ("generator", "INTEGER", 1, 0),
        ),
        "timing_paths": (
            ("path_index", "INTEGER", 0, 1),
            ("delay_ns", "REAL", 1, 0),
            ("slack_ns", "REAL", 1, 0),
            ("normalized_slack", "REAL", 1, 0),
        ),
        "metrics": (
            ("name", "TEXT", 1, 1),
            ("kind", "INTEGER", 1, 0),
            ("integer_value", "INTEGER", 0, 0),
            ("real_value", "REAL", 0, 0),
        ),
    }
    try:
        connection = sqlite3.connect(
            immutable_sqlite_uri(path), uri=True
        )
        connection.row_factory = sqlite3.Row
        try:
            if (
                connection.execute("PRAGMA application_id").fetchone()[0]
                != _NATIVE_ROUTE_RESULT_APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0]
                != _NATIVE_ROUTE_RESULT_USER_VERSION
            ):
                raise EmuFlowError(
                    "TLR router returned an unsupported typed output envelope"
                )
            objects = {
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT type,name FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%'"
                )
            }
            if objects != {("table", table) for table in expected_tables}:
                raise EmuFlowError(
                    "TLR router returned an invalid typed output schema"
                )
            for table, expected_columns in expected_tables.items():
                columns = tuple(
                    (
                        str(row[1]),
                        str(row[2]).upper(),
                        int(row[3]),
                        int(row[5]),
                    )
                    for row in connection.execute(f"PRAGMA table_info({table})")
                )
                if columns != expected_columns:
                    raise EmuFlowError(
                        "TLR router returned an invalid typed output schema"
                    )

            for row in connection.execute(
                "SELECT group_index,arc_index FROM direction_locks "
                "ORDER BY group_index"
            ):
                group = int(row[0])
                arc_index = int(row[1])
                if group in locks or arc_index < 0 or arc_index >= len(model["arcs"]):
                    raise EmuFlowError("TLR router returned an invalid lock")
                locks[group] = arc_index

            arc_rows = iter(
                connection.execute(
                    "SELECT demand_index,seq,arc_index FROM route_arcs "
                    "ORDER BY demand_index,seq"
                )
            )
            arc_row = next(arc_rows, None)
            for row in connection.execute(
                "SELECT demand_index,max_delay_ns FROM routes "
                "ORDER BY demand_index"
            ):
                demand_index = int(row[0])
                record_arcs = []
                while arc_row is not None and int(arc_row[0]) == demand_index:
                    if int(arc_row[1]) != len(record_arcs):
                        raise EmuFlowError(
                            "TLR router returned unordered route arcs"
                        )
                    record_arcs.append(int(arc_row[2]))
                    arc_row = next(arc_rows, None)
                if arc_row is not None and int(arc_row[0]) < demand_index:
                    raise EmuFlowError("TLR router returned orphan route arcs")
                routes.set_record(demand_index, float(row[1]), record_arcs)
            if arc_row is not None:
                raise EmuFlowError("TLR router returned orphan route arcs")

            for row in connection.execute(
                "SELECT demand_index,record_count,records "
                "FROM candidate_bundles ORDER BY demand_index"
            ):
                demand_index = int(row[0])
                record_count = int(row[1])
                payload = bytes(row[2])
                if demand_index < 0 or demand_index >= demand_count or not (
                    1 <= record_count <= len(ROUTE_CANDIDATE_GENERATORS)
                ):
                    raise EmuFlowError(
                        "TLR router returned duplicate/unknown candidate"
                    )
                offset = 0
                previous_ordinal = -1
                for _record in range(record_count):
                    if len(payload) - offset < 13:
                        raise EmuFlowError(
                            "TLR router returned malformed candidate bundle"
                        )
                    ordinal = payload[offset]
                    candidate_delay = struct.unpack_from(
                        "<d", payload, offset + 1
                    )[0]
                    arc_count = struct.unpack_from(
                        "<I", payload, offset + 9
                    )[0]
                    offset += 13
                    arc_bytes = 4 * arc_count
                    if (
                        ordinal <= previous_ordinal
                        or ordinal >= len(ROUTE_CANDIDATE_GENERATORS)
                        or len(payload) - offset < arc_bytes
                    ):
                        raise EmuFlowError(
                            "TLR router returned malformed candidate bundle"
                        )
                    record_arcs = (
                        []
                        if arc_count == 0
                        else list(
                            struct.unpack_from(
                                f"<{arc_count}I", payload, offset
                            )
                        )
                    )
                    offset += arc_bytes
                    previous_ordinal = ordinal
                    bit = 1 << ordinal
                    if candidate_coverage[demand_index] & bit:
                        raise EmuFlowError(
                            "TLR router returned duplicate/unknown candidate"
                        )
                    candidate_coverage[demand_index] |= bit
                    candidate_counts[ordinal] += 1
                    generator = ROUTE_CANDIDATE_GENERATORS[ordinal]
                    if compact_candidates:
                        hops, domain_mask = _validate_compact_native_candidate(
                            demand_index,
                            record_arcs,
                            candidate_delay,
                            model,
                            locks,
                            max_route_hops,
                        )
                        candidate_max_hops = max(candidate_max_hops, hops)
                        if generator in ROUTE_MASTER_GENERATORS:
                            candidate_domains[demand_index] |= domain_mask
                        if generator == "refined-final":
                            route = routes[demand_index]
                            if (
                                route["arcs"] != record_arcs
                                or route["max_delay_ns"] != candidate_delay
                            ):
                                raise EmuFlowError(
                                    "TLR router refined candidate does not "
                                    "match its final route"
                                )
                    else:
                        candidates[demand_index][generator] = {
                            "max_delay_ns": candidate_delay,
                            "arcs": record_arcs,
                        }
                if offset != len(payload):
                    raise EmuFlowError(
                        "TLR router returned trailing candidate bundle bytes"
                    )
            for row in connection.execute(
                "SELECT demand_index,generator FROM selections "
                "ORDER BY demand_index"
            ):
                ordinal = int(row[1])
                if ordinal < 0 or ordinal >= len(ROUTE_CANDIDATE_GENERATORS):
                    raise EmuFlowError(
                        "TLR router returned an unknown master selection"
                    )
                selections.set_record(
                    int(row[0]), ROUTE_CANDIDATE_GENERATORS[ordinal]
                )
            for row in connection.execute(
                "SELECT path_index,delay_ns,slack_ns,normalized_slack "
                "FROM timing_paths ORDER BY path_index"
            ):
                timing.set_record(
                    int(row[0]), float(row[1]), float(row[2]), float(row[3])
                )
            for row in connection.execute(
                "SELECT name,kind,integer_value,real_value FROM metrics "
                "ORDER BY name"
            ):
                name = str(row[0])
                kind = int(row[1])
                if name in metrics or kind not in {0, 1}:
                    raise EmuFlowError(
                        "TLR router returned invalid typed metrics"
                    )
                if (name in _INTEGER_NATIVE_METRICS) != (kind == 0):
                    raise EmuFlowError(
                        "TLR router returned invalid typed metrics"
                    )
                metrics[name] = int(row[2]) if kind == 0 else float(row[3])
        finally:
            connection.close()
    except sqlite3.DatabaseError as error:
        raise EmuFlowError("TLR router returned corrupt typed output") from error

    return _finish_parsed_native_output(
        routes=routes,
        candidates=candidates,
        selections=selections,
        locks=locks,
        timing=timing,
        metrics=metrics,
        candidate_coverage=candidate_coverage,
        candidate_domains=candidate_domains,
        candidate_counts=candidate_counts,
        candidate_max_hops=candidate_max_hops,
        compact_candidates=compact_candidates,
    )


def _parse_native_output(
    path: Path,
    model: Mapping[str, Any],
    *,
    compact_candidates: bool = False,
    max_route_hops: Optional[int] = None,
) -> Dict[str, Any]:
    return _parse_native_output_sqlite(
        path, model, compact_candidates=compact_candidates,
        max_route_hops=max_route_hops,
    )


def _tree_latency_cycles(
    source: str,
    sinks: Sequence[str],
    edge_keys: Sequence[ArcKey],
    arcs: Mapping[ArcKey, Mapping[str, Any]],
) -> int:
    graph: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for key in edge_keys:
        graph[key[1]].append((key[2], int(arcs[key]["latency_cycles"])))
    latency = {source: 0}
    queue = deque([source])
    while queue:
        node = queue.popleft()
        for sink, edge_latency in graph[node]:
            latency[sink] = latency[node] + edge_latency
            queue.append(sink)
    return max(latency[sink] for sink in sinks)


def route_system_native(
    assignment: Mapping[str, Any],
    platform: Platform,
    constraints: Mapping[str, Any],
    timing_paths: Optional[Mapping[str, Any]] = None,
    executable: Optional[str] = None,
    provider: str = GLOBAL_CANDIDATE_PROVIDER,
    candidate_pool_path: Optional[Path] = None,
    candidate_workers: int = 1,
    materialize_result: bool = True,
) -> Dict[str, Any]:
    if provider != GLOBAL_CANDIDATE_PROVIDER:
        raise ValueError(f"unsupported Phase 4 routing provider {provider!r}")
    if (
        isinstance(candidate_workers, bool)
        or not isinstance(candidate_workers, int)
        or candidate_workers <= 0
    ):
        raise ValueError("candidate_workers must be a positive integer")
    if not materialize_result:
        from .native_tools import require_native_schema
        require_native_schema("system-route-seal-request-v1")
    nodes, model = _prepare_native_model(
        assignment, platform, constraints, timing_paths
    )
    _validate_single_node_cut_capacity(nodes, model)
    if not model["demands"]:
        _, _, capacities = build_directed_graph(
            platform, constraints,
            reserve_arrival_and_commit=assignment_has_static_exact_contract(assignment),
        )
        utilization = [
            {
                **capacities[key],
                "used_bits": 0,
                "utilization": 0.0,
            }
            for key in sorted(capacities)
        ]
        direction_locks = [
            {
                "group": group,
                "link": link.id,
                "from": link.endpoints[0],
                "to": link.endpoints[1],
            }
            for group, link in enumerate(
                sorted(
                    (
                        link
                        for link in platform.links
                        if link.direction == "half_duplex"
                    ),
                    key=lambda item: item.id,
                )
            )
        ]
        metrics = {
            "demands": 0,
            "routed_sinks": 0,
            "tree_edges": 0,
            "iterations": 0,
            "accepted_reroutes": 0,
            "rolled_back_reroutes": 0,
            "max_link_utilization": 0.0,
            "total_link_bit_hops": 0,
            "estimated_max_tdm_ratio": 0,
            **(
                {"max_route_hops_observed": 0}
                if constraints.get("max_route_hops") is not None
                else {}
            ),
        }
        result = {
            "schema": "emuflow.system-routes/v1",
            "design": assignment.get("design"),
            "platform": platform.name,
            "provider": provider,
            "constraints": dict(constraints),
            "demands": [],
            "routes": [],
            "link_utilization": utilization,
            "direction_locks": direction_locks,
            "metrics": metrics,
        }
        if assignment_has_static_exact_contract(assignment):
            result["semantic_contract_schema"] = (
                assignment.get("semantic_contract_schema")
                or assignment["semantic_contract"]["schema"]
            )
            result["semantic_contract_sha256"] = assignment[
                "semantic_contract_sha256"
            ]
        return result
    command = resolve_native_stage_command(
        "phase4",
        "route",
        executable,
    )
    if command[-2:] != ["phase4", "route"]:
        raise EmuFlowError("Phase 4 requires the unified emuflow-native entry")
    native_result_owner = tempfile.TemporaryDirectory(prefix="emuflow-tlr-")
    root = Path(native_result_owner.name)
    native_input = root / "router.in"
    native_output = root / "router-result.sqlite"
    _write_native_input(
        native_input,
        len(nodes),
        model,
        constraints,
        provider,
        candidate_workers=candidate_workers,
    )
    completed = subprocess.run(
        [
            *command,
            "--typed-output",
            str(native_input),
            str(native_output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise EmuFlowError(
            f"in-tree TLR router failed with exit code "
            f"{completed.returncode}: {detail}"
        )
    native_result_source = bind_store_source(native_output, "tlr-output-v1")
    native = _parse_native_output(
        native_output,
        model,
        compact_candidates=(candidate_pool_path is None),
        max_route_hops=constraints.get("max_route_hops"),
    )
    require_store_sources_unchanged((native_result_source,))
    for name in ("master_rounds", "master_switches", "master_exact"):
        native["metrics"].setdefault(name, 0)

    # The native-only records are no longer needed after output parsing.  On
    # multi-million-demand cases, releasing them before materializing the
    # public route schema avoids retaining two complete demand views.
    model["native_demands"].clear()
    _, arcs, capacities = build_directed_graph(
        platform,
        constraints,
        reserve_arrival_and_commit=(
            assignment_has_static_exact_contract(assignment)
        ),
    )
    native_routes = native["routes"]
    route_latency_cycles = array("Q", [0]) * len(model["demands"])
    usage = {key: 0 for key in capacities}
    round_usage: Dict[Tuple[str, int], int] = defaultdict(int)
    maximum_observed_hops = 0
    for demand_index, demand in enumerate(model["demands"]):
        native_route = native_routes[demand_index]
        edge_keys = [
            model["arc_keys"][index] for index in native_route["arcs"]
        ]
        for key in edge_keys:
            usage[arcs[key]["capacity_key"]] += demand["width_bits"]
            round_usage[
                (
                    arcs[key]["capacity_key"],
                    int(demand.get("transport_round", 0)),
                )
            ] += demand["width_bits"]
        latency_cycles = _tree_latency_cycles(
            demand["source"], demand["sinks"], edge_keys, arcs
        )
        route_latency_cycles[demand_index] = latency_cycles
        route = {
            **demand,
            "tree_edges": [
                {"link": key[0], "from": key[1], "to": key[2]}
                for key in edge_keys
            ],
            "max_latency_cycles": latency_cycles,
            "predicted_max_delay_ns": native_route["max_delay_ns"],
        }
        _, _, route_hops = _validate_route_tree(route, arcs)
        maximum_observed_hops = max(maximum_observed_hops, route_hops)
    routes = _ProjectedSystemRouteSequence(
        model["demands"],
        native_routes,
        model["arc_keys"],
        route_latency_cycles,
        native_output,
        native_result_owner,
        native_result_source,
    )

    utilization = []
    for key in sorted(capacities):
        capacity = capacities[key]
        used = usage[key]
        utilization.append(
            {
                **capacity,
                "used_bits": used,
                "utilization": used / capacity["capacity_bits"],
            }
        )
    direction_locks = []
    for group, arc_index in sorted(native["direction_locks"].items()):
        key = model["arc_keys"][arc_index]
        direction_locks.append(
            {
                "group": group,
                "link": key[0],
                "from": key[1],
                "to": key[2],
            }
        )
    candidate_pool = None
    if native["candidates"] and candidate_pool_path is not None:
        candidates = []
        candidate_max_hops = 0
        for demand_index, demand in enumerate(model["demands"]):
            for generator in ROUTE_CANDIDATE_GENERATORS:
                raw = native["candidates"][demand_index].get(generator)
                if raw is None:
                    continue
                edge_keys = [
                    model["arc_keys"][index] for index in raw["arcs"]
                ]
                candidate = {
                    **demand,
                    "id": f"{demand['id']}:{generator}",
                    "demand_id": demand["id"],
                    "generator": generator,
                    "selected": generator == "refined-final",
                    "tree_edges": [
                        {"link": key[0], "from": key[1], "to": key[2]}
                        for key in edge_keys
                    ],
                    "max_latency_cycles": _tree_latency_cycles(
                        demand["source"], demand["sinks"], edge_keys, arcs
                    ),
                    "predicted_max_delay_ns": raw["max_delay_ns"],
                }
                _, _, candidate_hops = _validate_route_tree(candidate, arcs)
                candidate_max_hops = max(candidate_max_hops, candidate_hops)
                candidates.append(candidate)
        candidates_by_generator = {
            generator: sum(
                item["generator"] == generator for item in candidates
            )
            for generator in ROUTE_CANDIDATE_GENERATORS
            if any(item["generator"] == generator for item in candidates)
        }
        candidate_pool = {
            "schema": ROUTE_CANDIDATE_POOL_SCHEMA,
            "provider": ROUTE_CANDIDATE_POOL_PROVIDER,
            "design": assignment.get("design"),
            "platform": platform.name,
            "constraints": dict(constraints),
            "demands": model["demands"],
            "direction_locks": direction_locks,
            "candidates": candidates,
            "metrics": {
                "demands": len(model["demands"]),
                "candidates": len(candidates),
                "generators": len(candidates_by_generator),
                "candidates_by_generator": candidates_by_generator,
                "max_route_hops_observed": candidate_max_hops,
            },
        }
        validate_route_candidate_pool(assignment, platform, candidate_pool)
        if candidate_pool_path is not None:
            write_json(candidate_pool_path, candidate_pool)
    else:
        # Candidate alternatives are diagnostic evidence, not a downstream
        # routing interface.  Managed runs deliberately omit that optional
        # artifact and release the native candidate table before constructing
        # the canonical routes/timing payload.
        native["candidates"].clear()
    batch_certificate = None
    compact_summary = native["compact_candidate_summary"]
    if candidate_pool is not None:
        batch_certificate = build_route_refinement_batches(
            assignment, platform, candidate_pool,
            timing_paths or {"paths": [], "normalization": model["normalization"]},
        )
    elif compact_summary is not None:
        batch_certificate = build_compact_route_refinement_batches(
            model["paths"],
            model["normalization"],
            compact_summary["domains_by_demand"],
        )
        compact_summary["domains_by_demand"].clear()
    else:
        raise EmuFlowError(
            "TLR router lacks candidate evidence for batch validation"
        )
    if (
        batch_certificate["batch_count"]
        != native["metrics"]["reroute_conflict_batches"]
        or batch_certificate["maximum_parallel_batch"]
        != native["metrics"]["maximum_parallel_batch"]
    ):
        raise EmuFlowError(
            "TLR router conflict batches do not match independent "
            "reconstruction: native="
            f"({native['metrics']['reroute_conflict_batches']}, "
            f"{native['metrics']['maximum_parallel_batch']}), "
            "independent="
            f"({batch_certificate['batch_count']}, "
            f"{batch_certificate['maximum_parallel_batch']})"
        )
    physical_arc_index = {
        key: index for index, key in enumerate(sorted(arcs))
    }
    timing_records: Sequence[Mapping[str, Any]] = []
    if timing_paths is not None:
        timing_records = _ProjectedTimingRecordSequence(
            timing_paths["paths"],
            model["paths"],
            native["timing"],
            native_routes,
            model["arc_keys"],
            physical_arc_index,
        )
    else:
        model["paths"].clear()
    result = {
        "schema": "emuflow.system-routes/v1",
        "design": assignment.get("design"),
        "platform": platform.name,
        "provider": provider,
        "constraints": dict(constraints),
        "demands": model["demands"],
        "routes": routes,
        "link_utilization": utilization,
        "direction_locks": direction_locks,
        "metrics": {
            "demands": len(model["demands"]),
            "routed_sinks": sum(len(route["sinks"]) for route in routes),
            "tree_edges": sum(len(route["tree_edges"]) for route in routes),
            "iterations": native["metrics"]["iterations"],
            "accepted_reroutes": native["metrics"]["accepted_reroutes"],
            "rolled_back_reroutes": native["metrics"][
                "rolled_back_reroutes"
            ],
            "max_link_utilization": max(
                (record["utilization"] for record in utilization), default=0.0
            ),
            "total_link_bit_hops": sum(usage.values()),
            "estimated_max_tdm_ratio": native["metrics"][
                "estimated_max_tdm_ratio"
            ],
            **(
                {"max_route_hops_observed": maximum_observed_hops}
                if constraints.get("max_route_hops") is not None
                else {}
            ),
        },
        **(
            {
                "joint_optimization": {
                    "method": (
                        "checked-candidate-pool+deterministic-batch-conflict-lns-v2"
                    ),
                    "objective": (
                        "lexicographic estimated TDM normalized slack, "
                        "route normalized slack, utilization, bit-hops"
                    ),
                    "ratio_model": "quantized-domain-load-over-lanes",
                    "candidate_generation": {
                        "shortest_path_tree": bool(
                            native["metrics"][
                                "baseline_candidate_feasible"
                            ]
                        ),
                        "delay_demand_balanced_tree": bool(
                            native["metrics"][
                                "balanced_candidate_feasible"
                            ]
                        ),
                        "selected": (
                            "delay-demand-balanced"
                            if native["metrics"][
                                "selected_delay_demand_balanced"
                            ]
                            else "shortest-path-tree"
                        ),
                    },
                    "reference": (
                        "DAC 2025 delay-demand-balanced die-level routing "
                        "and ASP-DAC 2026 timing-aware load-balanced "
                        "routing/rip-up-reroute"
                    ),
                }
            }
        ),
    }
    if model["round_boundary"] is not None:
        link_by_id = {link.id: link for link in platform.links}
        round_domains = []
        for capacity_key in model["physical_capacity_keys"]:
            capacity = capacities[capacity_key]
            link = link_by_id[capacity["link"]]
            for round_id in model["active_rounds"]:
                available_slots = _round_available_slots(
                    int(constraints["frame_slots"]),
                    int(model["round_boundary"]),
                    link.latency_cycles,
                    round_id,
                )
                capacity_bits = (
                    capacity["capacity_bits"]
                    if capacity["link"] in constraints["sll_links"]
                    else link.transport_bits_per_cycle_per_direction
                    * available_slots
                )
                used_bits = round_usage[(capacity_key, round_id)]
                round_domains.append(
                    {
                        "key": capacity_key,
                        "link": capacity["link"],
                        "direction": capacity["direction"],
                        "transport_round": round_id,
                        "latency_cycles": link.latency_cycles,
                        "lanes": link.transport_bits_per_cycle_per_direction,
                        "available_slots": available_slots,
                        "capacity_bits": capacity_bits,
                        "used_bits": used_bits,
                        "utilization": used_bits / capacity_bits,
                    }
                )
        result["round_capacity_contract"] = {
            "schema": ROUND_CAPACITY_CONTRACT_SCHEMA,
            "frame_slots": int(constraints["frame_slots"]),
            "barrier_slots": RUNTIME_BARRIER_SLOTS,
            "source_ready_slot": int(model["round_boundary"]),
            "active_rounds": model["active_rounds"],
            "domains": round_domains,
        }
    assert batch_certificate is not None
    result["metrics"].update(
        {
            "master_rounds": native["metrics"]["master_rounds"],
            "master_switches": native["metrics"]["master_switches"],
            "master_exact": bool(native["metrics"]["master_exact"]),
            "reroute_conflict_batches": native["metrics"][
                "reroute_conflict_batches"
            ],
            "maximum_parallel_batch": native["metrics"][
                "maximum_parallel_batch"
            ],
        }
    )
    result["joint_optimization"]["refinement_batches"] = (
        batch_certificate
    )
    result["joint_optimization"]["candidate_generation"][
        "master_selection"
    ] = _ProjectedMasterSelectionSequence(
        model["demands"], native["selections"]
    )
    if candidate_pool is not None:
        selected_candidates = {
            (candidate["demand_id"], candidate["generator"]): candidate
            for candidate in candidate_pool["candidates"]
        }
        route_by_id = {route["id"]: route for route in routes}
        for record in result["joint_optimization"][
            "candidate_generation"
        ]["master_selection"]:
            candidate = selected_candidates[
                (record["demand"], "refined-final")
            ]
            route = route_by_id[record["demand"]]
            if (
                route["tree_edges"] != candidate["tree_edges"]
                or route["predicted_max_delay_ns"]
                != candidate["predicted_max_delay_ns"]
            ):
                raise EmuFlowError(
                    "TLR router refined candidate does not match final "
                    "routes"
                )
    if timing_paths is not None:
        result["timing"] = {
            "schema": STA_PATHS_SCHEMA,
            "normalization": timing_paths["normalization"],
            "compression": timing_paths["compression"],
            "paths": timing_records,
        }
        result["metrics"].update(
            {
                "worst_slack_ns": native["metrics"]["worst_slack_ns"],
                "worst_normalized_slack": native["metrics"][
                    "worst_normalized_slack"
                ],
                "estimated_worst_tdm_slack_ns": native["metrics"][
                    "estimated_worst_tdm_slack_ns"
                ],
                "estimated_worst_tdm_normalized_slack": native["metrics"][
                    "estimated_worst_tdm_normalized_slack"
                ],
            }
        )
    if assignment_has_static_exact_contract(assignment):
        # Phase 3 is the sole owner of the structural semantic contract.
        # Downstream route artifacts bind that contract by stable identity;
        # copying the full (potentially tens-of-megabytes) document here makes
        # every Phase 5/7 consumer parse the same information again.
        result["semantic_contract_schema"] = (
            assignment.get("semantic_contract_schema")
            or assignment["semantic_contract"]["schema"]
        )
        result["semantic_contract_sha256"] = assignment[
            "semantic_contract_sha256"
        ]
    if timing_paths is None:
        result["timing_evaluation"] = {
            "mode": "post-route-evaluation-only",
            "optimization_enabled": False,
        }
    if materialize_result:
        # Keep the public Python API mutable for reference callers and tests.
        # The production Phase 4 adapter disables this compatibility
        # materialization and feeds the repeatable projections directly into
        # independent validation and RouteStore publication.
        result["routes"] = list(result["routes"])
        timing_result = result.get("timing")
        if isinstance(timing_result, dict):
            timing_result["paths"] = list(timing_result["paths"])
        candidate_generation = result["joint_optimization"][
            "candidate_generation"
        ]
        candidate_generation["master_selection"] = list(
            candidate_generation["master_selection"]
        )
    return result


def reconstruct_system_route_timing(
    assignment: Mapping[str, Any],
    platform: Platform,
    routes: Mapping[str, Any],
    timing_paths: Mapping[str, Any],
    *,
    include_records: bool = False,
) -> Dict[str, Any]:
    """Evaluate any legal route provider against one normalized STA input."""
    validation = validate_system_routes(assignment, platform, routes)
    constraints = normalize_route_constraints(
        routes.get("constraints"), platform
    )
    _, arcs, capacities = build_directed_graph(
        platform,
        constraints,
        reserve_arrival_and_commit=(
            assignment_has_static_exact_contract(assignment)
        ),
    )
    link_by_id = {link.id: link for link in platform.links}

    ordered_arc_keys = sorted(arcs)
    arc_index = {key: index for index, key in enumerate(ordered_arc_keys)}
    route_count = len(routes["routes"])
    route_rank_by_net: Dict[str, int] = {}
    route_delays = array("d", [0.0]) * route_count
    route_signature_offsets = array("Q", [0])
    route_signature_arcs = array("Q")
    round_aware = sorted(
        {int(route.get("transport_round", 0)) for route in routes["routes"]}
    ) == [0, 1]
    capacity_usage: Dict[Any, int] = defaultdict(int)
    for route_rank, route in enumerate(routes["routes"]):
        graph: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        signature = []
        for edge in route["tree_edges"]:
            key = _arc_key(edge["link"], edge["from"], edge["to"])
            graph[edge["from"]].append((edge["to"], edge["link"]))
            capacity_key: Any = arcs[key]["capacity_key"]
            if round_aware:
                capacity_key = (
                    capacity_key,
                    int(route.get("transport_round", 0)),
                )
            capacity_usage[capacity_key] += int(route["width_bits"])
            signature.append(arc_index[key])
        delay_by_node = {route["source"]: 0.0}
        queue = deque([route["source"]])
        while queue:
            node = queue.popleft()
            for sink, link_id in graph[node]:
                delay_by_node[sink] = (
                    delay_by_node[node]
                    + route_link_delay_ns(
                        platform, link_id, node, sink, constraints
                    )
                )
                queue.append(sink)
        net = route["net"]
        if net in route_rank_by_net:
            raise ValidationError(
                f"routes contain duplicate net identity {net!r}"
            )
        route_rank_by_net[net] = route_rank
        route_delays[route_rank] = max(
            delay_by_node[sink] for sink in route["sinks"]
        )
        route_signature_arcs.extend(sorted(signature))
        route_signature_offsets.append(len(route_signature_arcs))

    ratios: Dict[Any, int] = {}
    ratio_keys = (
        [
            (key, round_id)
            for key in capacities
            for round_id in (0, 1)
        ]
        if round_aware
        else list(capacities)
    )
    for ratio_key in ratio_keys:
        key = ratio_key[0] if round_aware else ratio_key
        capacity = capacities[key]
        lanes = link_by_id[
            capacity["link"]
        ].transport_bits_per_cycle_per_direction
        signals = capacity_usage[ratio_key]
        ratios[ratio_key] = estimate_tdm_ratio(
            signals,
            lanes,
            constraints,
            is_sll=capacity["link"] in constraints["sll_links"],
        )

    ratios = apply_bank_domain_ratios(capacities, capacity_usage, ratios, constraints)
    route_tdm_delays = array("d", [0.0]) * route_count
    for route_rank, route in enumerate(routes["routes"]):
        graph: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
        for edge in route["tree_edges"]:
            key = _arc_key(edge["link"], edge["from"], edge["to"])
            capacity_key: Any = arcs[key]["capacity_key"]
            if round_aware:
                capacity_key = (
                    capacity_key,
                    int(route.get("transport_round", 0)),
                )
            graph[edge["from"]].append(
                (
                    edge["to"],
                    edge["link"],
                    capacity_key,
                )
            )
        delay_by_node = {route["source"]: 0.0}
        queue = deque([route["source"]])
        while queue:
            node = queue.popleft()
            for sink, link_id, capacity_key in graph[node]:
                link = link_by_id[link_id]
                delay_by_node[sink] = (
                    delay_by_node[node]
                    + route_link_delay_ns(
                        platform, link_id, node, sink, constraints
                    )
                    + (
                        0.0
                        if link_id in constraints["sll_links"]
                        else route_link_delay_coefficients_ns(
                            platform, link_id, node, sink, constraints
                        )[1] * (ratios[capacity_key] - 1)
                    )
                )
                queue.append(sink)
        if constraints.get("tree_edge_sum_tdm", False):
            route_tdm_delays[route_rank] = sum(
                route_link_delay_ns(
                    platform,
                    edge["link"],
                    edge["from"],
                    edge["to"],
                    constraints,
                )
                + (
                    0.0
                    if edge["link"] in constraints["sll_links"]
                    else (
                        route_link_delay_coefficients_ns(
                            platform, edge["link"], edge["from"], edge["to"], constraints
                        )[1]
                    )
                    * (
                        ratios[
                            (
                                arcs[
                                    _arc_key(
                                        edge["link"],
                                        edge["from"],
                                        edge["to"],
                                    )
                                ]["capacity_key"],
                                int(route.get("transport_round", 0)),
                            )
                            if round_aware
                            else arcs[
                                _arc_key(
                                    edge["link"],
                                    edge["from"],
                                    edge["to"],
                                )
                            ]["capacity_key"]
                        ]
                        - 1
                    )
                )
                for edge in route["tree_edges"]
            )
        else:
            route_tdm_delays[route_rank] = max(
                delay_by_node[sink] for sink in route["sinks"]
            )

    normalization = timing_paths["normalization"]
    path_records = [] if include_records else None
    path_count = 0
    worst_slack = (float("inf"), "")
    worst_normalized = (float("inf"), "")
    worst_tdm_slack = (float("inf"), "")
    worst_tdm_normalized = (float("inf"), "")
    for path in timing_paths["paths"]:
        path_count += 1
        try:
            path_route_ranks = [
                route_rank_by_net[net] for net in path["cut_nets"]
            ]
        except KeyError as error:
            raise ValidationError(
                f"route timing path {path['id']!r} references an unknown "
                "cut net"
            ) from error
        required_time = path["fixed_delay_ns"] + path["slack_ns"]
        delay = path["fixed_delay_ns"] + sum(
            route_delays[rank] for rank in path_route_ranks
        )
        slack = required_time - delay
        normalized = (
            slack
            * path["clock_period_ns"]
            / (
                normalization["positive_slack_scale_ns"]
                * normalization["max_clock_period_ns"]
            )
            if slack >= 0.0
            else slack
            / (
                normalization["negative_slack_scale_ns"]
                * path["clock_period_ns"]
            )
        )
        tdm_delay = path["fixed_delay_ns"] + sum(
            route_tdm_delays[rank] for rank in path_route_ranks
        )
        tdm_slack = required_time - tdm_delay
        tdm_normalized = (
            tdm_slack
            * path["clock_period_ns"]
            / (
                normalization["positive_slack_scale_ns"]
                * normalization["max_clock_period_ns"]
            )
            if tdm_slack >= 0.0
            else tdm_slack
            / (
                normalization["negative_slack_scale_ns"]
                * path["clock_period_ns"]
            )
        )
        route_signature = (
            ",".join(
                str(arc)
                for rank in path_route_ranks
                for arc in route_signature_arcs[
                    route_signature_offsets[rank]
                    : route_signature_offsets[rank + 1]
                ]
            )
            or "-"
        )
        path_id = path["id"]
        worst_slack = min(worst_slack, (slack, path_id))
        worst_normalized = min(
            worst_normalized, (normalized, path_id)
        )
        worst_tdm_slack = min(
            worst_tdm_slack, (tdm_slack, path_id)
        )
        worst_tdm_normalized = min(
            worst_tdm_normalized, (tdm_normalized, path_id)
        )
        if path_records is not None:
            path_records.append({
                "path": path["id"],
                "clock_domain": path["clock_domain"],
                "clock_period_ns": path["clock_period_ns"],
                "required_time_ns": required_time,
                "fixed_delay_ns": path["fixed_delay_ns"],
                "cut_nets": path["cut_nets"],
                "cut_signature": path["cut_signature"],
                "compressed_path_ids": path["compressed_path_ids"],
                **(
                    {"cut_transitions": path["cut_transitions"]}
                    if "cut_transitions" in path
                    else {}
                ),
                "delay_ns": delay,
                "slack_ns": slack,
                "normalized_slack": normalized,
                "estimated_tdm_slack_ns": tdm_slack,
                "estimated_tdm_normalized_slack": tdm_normalized,
                "route_signature": route_signature,
            })
    if path_count == 0:
        raise ValidationError(
            "route timing reconstruction has no normalized STA paths"
        )
    result = {
        **validation,
        "provider": routes["provider"],
        "timing_paths_original": timing_paths["compression"][
            "original_paths"
        ],
        "timing_paths_compressed": timing_paths["compression"][
            "compressed_paths"
        ],
        "worst_slack_path": worst_slack[1],
        "worst_slack_ns": worst_slack[0],
        "worst_normalized_path": worst_normalized[1],
        "worst_normalized_slack": worst_normalized[0],
        "estimated_worst_tdm_slack_path": worst_tdm_slack[1],
        "estimated_worst_tdm_slack_ns": worst_tdm_slack[0],
        "estimated_worst_tdm_normalized_path": worst_tdm_normalized[1],
        "estimated_worst_tdm_normalized_slack": worst_tdm_normalized[0],
        "estimated_max_tdm_ratio": max(ratios.values(), default=1),
    }
    if include_records:
        assert path_records is not None
        result["_timing_records"] = path_records
    return result


def annotate_system_route_timing(
    assignment: Mapping[str, Any],
    platform: Platform,
    routes: Mapping[str, Any],
    timing_paths: Mapping[str, Any],
) -> Dict[str, Any]:
    """Bind TimingPathDB evaluation to a timing-oblivious legal route.

    The route tree is produced without timing criticality, then evaluated
    against the complete normalized TimingPathDB. The global candidate engine
    remains the route owner; a disabled ``timing-driven`` policy preserves
    the path identities required by Phase 5 and whole-design Phase 7C timing.
    """

    evaluation = reconstruct_system_route_timing(
        assignment,
        platform,
        routes,
        timing_paths,
        include_records=True,
    )
    records = evaluation.pop("_timing_records")
    annotated = dict(routes)
    annotated["provider"] = GLOBAL_CANDIDATE_PROVIDER
    annotated["timing_evaluation"] = {
        "mode": "post-route-evaluation-only",
        "optimization_enabled": False,
    }
    annotated["timing"] = {
        "schema": STA_PATHS_SCHEMA,
        "normalization": timing_paths["normalization"],
        "compression": timing_paths["compression"],
        "paths": records,
    }
    annotated["metrics"] = {
        **routes["metrics"],
        "worst_slack_ns": evaluation["worst_slack_ns"],
        "worst_normalized_slack": evaluation[
            "worst_normalized_slack"
        ],
        "estimated_worst_tdm_slack_ns": evaluation[
            "estimated_worst_tdm_slack_ns"
        ],
        "estimated_worst_tdm_normalized_slack": evaluation[
            "estimated_worst_tdm_normalized_slack"
        ],
        "estimated_max_tdm_ratio": evaluation[
            "estimated_max_tdm_ratio"
        ],
    }
    return annotated


def validate_native_system_routes(
    assignment: Mapping[str, Any],
    platform: Platform,
    routes: Mapping[str, Any],
    timing_paths: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    validation = validate_system_routes(assignment, platform, routes)
    provider = routes.get("provider")
    if provider != GLOBAL_CANDIDATE_PROVIDER:
        raise ValidationError("routes.provider: expected the global candidate provider")
    evaluation = routes.get("timing_evaluation")
    if evaluation is not None and evaluation != {
        "mode": "post-route-evaluation-only",
        "optimization_enabled": False,
    }:
        raise ValidationError("global candidate routes have invalid timing evaluation metadata")
    if routes["routes"]:
        joint = routes.get("joint_optimization")
        if not isinstance(joint, dict) or joint.get("method") != (
            "checked-candidate-pool+deterministic-batch-conflict-lns-v2"
        ):
            raise ValidationError("routes.joint_optimization: invalid global candidate metadata")
        selection = joint.get("candidate_generation", {}).get(
            "master_selection"
        )
        projected_selection = isinstance(
            selection, _ProjectedMasterSelectionSequence
        )
        projected_identity_valid = (
            projected_selection
            and all(
                selection_record.get("demand") == route.get("id")
                for selection_record, route in zip(
                    selection, routes.get("routes", [])
                )
            )
        )
        if (
            not isinstance(selection, list)
            or len(selection) != len(routes.get("routes", []))
            or (
                not projected_identity_valid
                if projected_selection
                else {
                    record.get("demand")
                    for record in selection
                    if isinstance(record, dict)
                }
                != {
                    route.get("id")
                    for route in routes.get("routes", [])
                }
            )
            or any(
                not isinstance(record, dict)
                or record.get("generator") not in ROUTE_CANDIDATE_GENERATORS
                or record.get("generator") == "refined-final"
                for record in selection
            )
        ):
            raise ValidationError(
                "routes.joint_optimization master selection is invalid"
            )
    locks = routes.get("direction_locks")
    if not isinstance(locks, list):
        raise ValidationError("routes.direction_locks: expected an array")
    lock_by_link = {}
    for lock in locks:
        if not isinstance(lock, dict):
            raise ValidationError("routes.direction_locks: invalid record")
        link = next(
            (item for item in platform.links if item.id == lock.get("link")),
            None,
        )
        if link is None or link.direction != "half_duplex":
            raise ValidationError(
                "routes.direction_locks: lock must name a half-duplex link"
            )
        direction = (lock.get("from"), lock.get("to"))
        if set(direction) != set(link.endpoints):
            raise ValidationError(
                "routes.direction_locks: direction does not match endpoints"
            )
        if link.id in lock_by_link:
            raise ValidationError(
                f"routes.direction_locks: duplicate link {link.id!r}"
            )
        lock_by_link[link.id] = direction
    expected_locks = {
        link.id for link in platform.links if link.direction == "half_duplex"
    }
    if set(lock_by_link) != expected_locks:
        raise ValidationError(
            "routes.direction_locks: half-duplex lock coverage is not exact"
        )
    for route in routes["routes"]:
        for edge in route["tree_edges"]:
            locked = lock_by_link.get(edge["link"])
            if locked is not None and locked != (edge["from"], edge["to"]):
                raise ValidationError(
                    f"route {route['id']!r}: violates direction lock"
                )

    constraints = normalize_route_constraints(
        routes.get("constraints"), platform
    )
    round_capacity_validation = _validate_round_capacity_contract(
        routes,
        platform,
        constraints,
        exact_dependency_schedule=assignment_has_static_exact_contract(
            assignment
        ),
    )
    timed_provider = timing_paths is not None
    route_count = len(routes["routes"])
    route_rank_by_net: Dict[str, int] = {}
    route_delays = array("d", [0.0]) * route_count
    route_signature_offsets = array("Q", [0])
    route_signature_arcs = array("Q")
    ordered_arc_keys = sorted(
        build_directed_graph(platform, constraints)[1]
    )
    arc_index = {key: index for index, key in enumerate(ordered_arc_keys)}
    for route_rank, route in enumerate(routes["routes"]):
        graph: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        signature = []
        for edge in route["tree_edges"]:
            graph[edge["from"]].append((edge["to"], edge["link"]))
            signature.append(
                arc_index[_arc_key(edge["link"], edge["from"], edge["to"])]
            )
        delay_by_node = {route["source"]: 0.0}
        queue = deque([route["source"]])
        while queue:
            node = queue.popleft()
            for sink, link_id in graph[node]:
                delay_by_node[sink] = delay_by_node[node] + route_link_delay_ns(
                    platform, link_id, node, sink, constraints
                )
                queue.append(sink)
        predicted_delay = max(
            delay_by_node[sink] for sink in route["sinks"]
        )
        if (
            abs(
                float(route.get("predicted_max_delay_ns", float("nan")))
                - predicted_delay
            )
            > 1.0e-9
        ):
            raise ValidationError(
                f"route {route['id']!r}.predicted_max_delay_ns does not "
                "match independent edge-delay recomputation"
            )
        route_delays[route_rank] = predicted_delay
        if timed_provider:
            net = route["net"]
            if net in route_rank_by_net:
                raise ValidationError(
                    f"routes contain duplicate net identity {net!r}"
                )
            route_rank_by_net[net] = route_rank
            route_signature_arcs.extend(sorted(signature))
            route_signature_offsets.append(len(route_signature_arcs))

    if timing_paths is None:
        if "timing" in routes:
            raise ValidationError(
                "routes without STA input must not contain timing records"
            )
        return {
            **validation,
            "provider": provider,
            "timing_optimization_enabled": False,
            "direction_locks": len(lock_by_link),
            "predicted_delay_check": "pass",
            **round_capacity_validation,
        }

    # Independently reconstruct the route/TDM co-optimization proxy used by
    # the native router.  This is deliberately separate from native output:
    # a corrupt or incorrectly implemented optimizer cannot self-certify.
    _, arcs, capacities = build_directed_graph(
        platform,
        constraints,
        reserve_arrival_and_commit=(
            assignment_has_static_exact_contract(assignment)
        ),
    )
    round_aware = (
        not assignment_has_static_exact_contract(assignment)
        and sorted(
            {
                int(route.get("transport_round", 0))
                for route in routes["routes"]
            }
        )
        == [0, 1]
    )
    capacity_usage: Dict[Any, int] = defaultdict(int)
    for route in routes["routes"]:
        width = int(route["width_bits"])
        for edge in route["tree_edges"]:
            key = _arc_key(edge["link"], edge["from"], edge["to"])
            capacity_key: Any = arcs[key]["capacity_key"]
            if round_aware:
                capacity_key = (
                    capacity_key,
                    int(route.get("transport_round", 0)),
                )
            capacity_usage[capacity_key] += width
    link_by_id = {link.id: link for link in platform.links}
    ratios: Dict[Any, int] = {}
    ratio_keys = (
        [
            (key, round_id)
            for key in capacities
            for round_id in (0, 1)
        ]
        if round_aware
        else list(capacities)
    )
    for ratio_key in ratio_keys:
        key = ratio_key[0] if round_aware else ratio_key
        capacity = capacities[key]
        lanes = link_by_id[
            capacity["link"]
        ].transport_bits_per_cycle_per_direction
        signals = capacity_usage[ratio_key]
        ratios[ratio_key] = estimate_tdm_ratio(
            signals,
            lanes,
            constraints,
            is_sll=capacity["link"] in constraints["sll_links"],
        )
    ratios = apply_bank_domain_ratios(capacities, capacity_usage, ratios, constraints)
    route_tdm_delays = array("d", [0.0]) * route_count
    for route_rank, route in enumerate(routes["routes"]):
        graph: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
        for edge in route["tree_edges"]:
            key = _arc_key(edge["link"], edge["from"], edge["to"])
            capacity_key: Any = arcs[key]["capacity_key"]
            if round_aware:
                capacity_key = (
                    capacity_key,
                    int(route.get("transport_round", 0)),
                )
            graph[edge["from"]].append(
                (edge["to"], edge["link"], capacity_key)
            )
        delay_by_node = {route["source"]: 0.0}
        queue = deque([route["source"]])
        while queue:
            node = queue.popleft()
            for sink, link_id, capacity_key in graph[node]:
                link = link_by_id[link_id]
                delay_by_node[sink] = (
                    delay_by_node[node]
                    + route_link_delay_ns(
                        platform, link_id, node, sink, constraints
                    )
                    + route_link_delay_coefficients_ns(
                        platform, link_id, node, sink, constraints
                    )[1] * (ratios[capacity_key] - 1)
                )
                queue.append(sink)
        if constraints.get("tree_edge_sum_tdm", False):
            route_tdm_delays[route_rank] = sum(
                route_link_delay_ns(
                    platform,
                    edge["link"],
                    edge["from"],
                    edge["to"],
                    constraints,
                )
                + (
                    0.0
                    if edge["link"] in constraints["sll_links"]
                    else (
                        route_link_delay_coefficients_ns(
                            platform, edge["link"], edge["from"], edge["to"], constraints
                        )[1]
                    )
                    * (
                        ratios[
                            (
                                arcs[
                                    _arc_key(
                                        edge["link"],
                                        edge["from"],
                                        edge["to"],
                                    )
                                ]["capacity_key"],
                                int(route.get("transport_round", 0)),
                            )
                            if round_aware
                            else arcs[
                                _arc_key(
                                    edge["link"],
                                    edge["from"],
                                    edge["to"],
                                )
                            ]["capacity_key"]
                        ]
                        - 1
                    )
                )
                for edge in route["tree_edges"]
            )
        else:
            route_tdm_delays[route_rank] = max(
                delay_by_node[sink] for sink in route["sinks"]
            )

    timing = routes.get("timing")
    if not isinstance(timing, dict) or timing.get("schema") != STA_PATHS_SCHEMA:
        raise ValidationError("routes.timing: invalid timing artifact")
    records = timing.get("paths")
    if not isinstance(records, list) or len(records) != len(
        timing_paths["paths"]
    ):
        raise ValidationError("routes.timing.paths: coverage is not exact")
    if timing.get("normalization") != timing_paths["normalization"]:
        raise ValidationError(
            "routes.timing.normalization does not match normalized STA input"
        )
    if timing.get("compression") != timing_paths["compression"]:
        raise ValidationError(
            "routes.timing.compression does not match normalized STA input"
        )
    positive_scale = timing_paths["normalization"][
        "positive_slack_scale_ns"
    ]
    negative_scale = timing_paths["normalization"][
        "negative_slack_scale_ns"
    ]
    max_period = timing_paths["normalization"]["max_clock_period_ns"]
    worst_slack = float("inf")
    worst_normalized = float("inf")
    estimated_worst_tdm_slack = float("inf")
    estimated_worst_tdm_normalized = float("inf")
    for expected, actual in zip(timing_paths["paths"], records):
        if actual.get("path") != expected["id"]:
            raise ValidationError("routes.timing.paths: order/identity mismatch")
        for key in (
            "clock_domain",
            "clock_period_ns",
            "fixed_delay_ns",
            "cut_nets",
            "cut_signature",
            "compressed_path_ids",
        ):
            if actual.get(key) != expected[key]:
                raise ValidationError(
                    f"routes.timing path {expected['id']!r}.{key}: "
                    "does not match normalized STA input"
                )
        required_time = expected["fixed_delay_ns"] + expected["slack_ns"]
        if actual.get("required_time_ns") != required_time:
            raise ValidationError(
                f"routes.timing path {expected['id']!r}.required_time_ns: "
                "does not match normalized STA input"
            )
        if actual.get("cut_transitions") != expected.get(
            "cut_transitions"
        ):
            raise ValidationError(
                f"routes.timing path {expected['id']!r}.cut_transitions: "
                "does not match normalized STA input"
            )
        try:
            path_route_ranks = [
                route_rank_by_net[net] for net in expected["cut_nets"]
            ]
        except KeyError as error:
            raise ValidationError(
                f"routes.timing path {expected['id']!r} references an "
                "unknown cut net"
            ) from error
        delay = expected["fixed_delay_ns"] + sum(
            route_delays[rank] for rank in path_route_ranks
        )
        slack = required_time - delay
        if slack >= 0.0:
            normalized = (
                slack
                * expected["clock_period_ns"]
                / (positive_scale * max_period)
            )
        else:
            normalized = (
                slack / (negative_scale * expected["clock_period_ns"])
            )
        for key, value in (
            ("delay_ns", delay),
            ("slack_ns", slack),
            ("normalized_slack", normalized),
        ):
            if abs(float(actual.get(key, float("nan"))) - value) > 1.0e-9:
                raise ValidationError(
                    f"routes.timing path {expected['id']!r}.{key}: "
                    "does not match independent recomputation"
                )
        expected_signature = ",".join(
            str(arc)
            for rank in path_route_ranks
            for arc in route_signature_arcs[
                route_signature_offsets[rank]
                : route_signature_offsets[rank + 1]
            ]
        )
        if not expected_signature:
            expected_signature = "-"
        if actual.get("route_signature") != expected_signature:
            raise ValidationError(
                f"routes.timing path {expected['id']!r}.route_signature: "
                "does not match independently reconstructed routes"
            )
        worst_slack = min(worst_slack, slack)
        worst_normalized = min(worst_normalized, normalized)
        tdm_delay = expected["fixed_delay_ns"] + sum(
            route_tdm_delays[rank] for rank in path_route_ranks
        )
        tdm_slack = required_time - tdm_delay
        if tdm_slack >= 0.0:
            tdm_normalized = (
                tdm_slack
                * expected["clock_period_ns"]
                / (positive_scale * max_period)
            )
        else:
            tdm_normalized = (
                tdm_slack
                / (negative_scale * expected["clock_period_ns"])
            )
        for key, value in (
            ("estimated_tdm_slack_ns", tdm_slack),
            ("estimated_tdm_normalized_slack", tdm_normalized),
        ):
            if abs(float(actual.get(key, float("nan"))) - value) > 1.0e-9:
                raise ValidationError(
                    f"routes.timing path {expected['id']!r}.{key}: "
                    "does not match independent recomputation"
                )
        estimated_worst_tdm_slack = min(
            estimated_worst_tdm_slack, tdm_slack
        )
        estimated_worst_tdm_normalized = min(
            estimated_worst_tdm_normalized, tdm_normalized
        )
    metrics = routes.get("metrics", {})
    if abs(metrics.get("worst_slack_ns", float("nan")) - worst_slack) > 1.0e-9:
        raise ValidationError(
            "routes.metrics.worst_slack_ns does not match timing paths"
        )
    if (
        abs(
            metrics.get("worst_normalized_slack", float("nan"))
            - worst_normalized
        )
        > 1.0e-9
    ):
        raise ValidationError(
            "routes.metrics.worst_normalized_slack does not match timing paths"
        )
    for key, expected in (
        ("estimated_worst_tdm_slack_ns", estimated_worst_tdm_slack),
        (
            "estimated_worst_tdm_normalized_slack",
            estimated_worst_tdm_normalized,
        ),
        ("estimated_max_tdm_ratio", max(ratios.values(), default=1)),
    ):
        if abs(float(metrics.get(key, float("nan"))) - expected) > 1.0e-9:
            raise ValidationError(
                f"routes.metrics.{key} does not match independent "
                "route/TDM proxy recomputation"
            )
    # The common timing reconstruction below is a separate implementation
    # check.  Release this validator's route-scale identity/metric columns
    # before invoking it so the two independent representations never overlap
    # at peak RSS.
    del route_rank_by_net
    del route_delays
    del route_signature_offsets
    del route_signature_arcs
    del route_tdm_delays
    common_timing = reconstruct_system_route_timing(
        assignment, platform, routes, timing_paths
    )
    for key, expected in (
        ("worst_slack_ns", worst_slack),
        ("worst_normalized_slack", worst_normalized),
        ("estimated_worst_tdm_slack_ns", estimated_worst_tdm_slack),
        (
            "estimated_worst_tdm_normalized_slack",
            estimated_worst_tdm_normalized,
        ),
        ("estimated_max_tdm_ratio", max(ratios.values(), default=1)),
    ):
        if abs(float(common_timing[key]) - expected) > 1.0e-9:
            raise ValidationError(
                f"common route timing reconstruction disagrees on {key}"
            )
    return {
        **common_timing,
        "direction_locks": len(locks),
        "timing_optimization_enabled": evaluation is None,
        "accepted_reroutes": metrics.get("accepted_reroutes"),
        "rolled_back_reroutes": metrics.get("rolled_back_reroutes"),
        **round_capacity_validation,
    }
