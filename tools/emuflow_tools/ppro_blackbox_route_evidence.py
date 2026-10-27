"""Recover payload paths from compact ordinary PPro per-hop route evidence."""

from __future__ import annotations

import heapq
from collections import defaultdict
from typing import Mapping, Sequence


def _distances(
    routes: Sequence[Mapping[str, object]], *, source: str, minimum_signal_count: int
) -> dict[str, int]:
    graph: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for route in routes:
        signal_count = route.get("signal_count")
        hops = route.get("effective_hops")
        route_source = route.get("source")
        sinks = route.get("sinks")
        if (
            isinstance(signal_count, bool)
            or not isinstance(signal_count, int)
            or signal_count < minimum_signal_count
            or isinstance(hops, bool)
            or not isinstance(hops, int)
            or hops < 1
            or not isinstance(route_source, str)
            or not isinstance(sinks, list)
        ):
            continue
        for route_sink in sinks:
            if isinstance(route_sink, str):
                graph[route_source].append((route_sink, hops))

    queue = [(0, source)]
    best = {source: 0}
    while queue:
        distance, node = heapq.heappop(queue)
        if distance != best[node]:
            continue
        for neighbor, cost in graph.get(node, []):
            candidate = distance + cost
            if candidate < best.get(neighbor, candidate + 1):
                best[neighbor] = candidate
                heapq.heappush(queue, (candidate, neighbor))
    return best


def shortest_payload_hops(
    routes: Sequence[Mapping[str, object]],
    *,
    source: str,
    sink: str,
    minimum_signal_count: int,
) -> int | None:
    """Return shortest observable payload hops, excluding narrower side traffic."""

    return _distances(
        routes, source=source, minimum_signal_count=minimum_signal_count
    ).get(sink)


def maximum_payload_hops(
    routes: Sequence[Mapping[str, object]],
    *,
    source: str,
    minimum_signal_count: int,
) -> int | None:
    """Return the longest shortest path from a producer over its payload graph."""

    distances = _distances(
        routes, source=source, minimum_signal_count=minimum_signal_count
    )
    reached = [distance for node, distance in distances.items() if node != source]
    return max(reached) if reached else None


def _capacity_edges(
    routes: Sequence[Mapping[str, object]],
) -> tuple[set[str], list[tuple[str, str, int, int]]]:
    nodes: set[str] = set()
    edges: list[tuple[str, str, int, int]] = []
    for route in routes:
        capacity = route.get("signal_count")
        hops = route.get("effective_hops")
        source = route.get("source")
        sinks = route.get("sinks")
        if (
            isinstance(capacity, bool)
            or not isinstance(capacity, int)
            or capacity <= 0
            or isinstance(hops, bool)
            or not isinstance(hops, int)
            or hops < 1
            or not isinstance(source, str)
            or not isinstance(sinks, list)
        ):
            continue
        nodes.add(source)
        for sink in sinks:
            if isinstance(sink, str):
                nodes.add(sink)
                edges.append((source, sink, hops, capacity))
    return nodes, edges


def _layered_flow(
    nodes: set[str],
    edges: Sequence[tuple[str, str, int, int]],
    *,
    source: str,
    sink: str,
    hop_limit: int,
    demand: int,
) -> int:
    """Bounded-hop integral max flow for the tiny observed FPGA graph."""

    graph: dict[tuple[str, int] | tuple[str, str], dict[tuple[str, int] | tuple[str, str], int]] = defaultdict(dict)
    start: tuple[str, int] | tuple[str, str] = (source, 0)
    terminal: tuple[str, int] | tuple[str, str] = ("__sink__", sink)

    def add_edge(left, right, capacity: int) -> None:
        graph[left][right] = graph[left].get(right, 0) + capacity
        graph[right].setdefault(left, 0)

    for left, right, hops, capacity in edges:
        for level in range(hop_limit - hops + 1):
            add_edge((left, level), (right, level + hops), capacity)
    for level in range(hop_limit + 1):
        add_edge((sink, level), terminal, demand)

    total = 0
    while total < demand:
        parent = {start: None}
        queue = [start]
        for node in queue:
            for neighbor, capacity in graph[node].items():
                if capacity > 0 and neighbor not in parent:
                    parent[neighbor] = node
                    queue.append(neighbor)
                    if neighbor == terminal:
                        break
            if terminal in parent:
                break
        if terminal not in parent:
            break
        amount = demand - total
        node = terminal
        while parent[node] is not None:
            amount = min(amount, graph[parent[node]][node])
            node = parent[node]
        node = terminal
        while parent[node] is not None:
            previous = parent[node]
            graph[previous][node] -= amount
            graph[node][previous] = graph[node].get(previous, 0) + amount
            node = previous
        total += amount
    return total


def maximum_capacity_payload_hops(
    routes: Sequence[Mapping[str, object]],
    *,
    source: str,
    sinks: Sequence[str],
    minimum_signal_count: int,
) -> int | None:
    """Return the hop bound needed to carry a possibly striped payload.

    Each multicast consumer is checked independently because a shared prefix
    carries one copy of the payload.  The layered network prevents a narrow
    direct control edge from masquerading as the payload merely because it
    provides a shorter topological path.
    """

    if minimum_signal_count <= 0 or not sinks:
        return None
    nodes, edges = _capacity_edges(routes)
    if source not in nodes or any(sink not in nodes for sink in sinks):
        return None
    maximum_edge_hops = max((edge[2] for edge in edges), default=0)
    maximum_limit = max(1, len(nodes) - 1) * maximum_edge_hops
    required_limits = []
    for sink in sinks:
        found = None
        for limit in range(1, maximum_limit + 1):
            if (
                _layered_flow(
                    nodes,
                    edges,
                    source=source,
                    sink=sink,
                    hop_limit=limit,
                    demand=minimum_signal_count,
                )
                >= minimum_signal_count
            ):
                found = limit
                break
        if found is None:
            return None
        required_limits.append(found)
    return max(required_limits)
