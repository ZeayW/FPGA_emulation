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
