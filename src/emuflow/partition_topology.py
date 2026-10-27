"""Small board-level routing facts shared by native partition configuration."""
from collections import defaultdict, deque
import heapq
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .platform import Platform
from .routing import build_directed_graph, route_link_delay_ns


def _shortest_routes(
    platform: Platform, route_constraints: Mapping[str, Any]
) -> Dict[str, Dict[str, Optional[List[Dict[str, Any]]]]]:
    adjacency, _, _ = build_directed_graph(platform, route_constraints)
    result: Dict[str, Dict[str, Optional[List[Dict[str, Any]]]]] = {}
    for source in sorted(adjacency):
        # The tuple deliberately includes the complete arc identity.  Equal
        # delay/hop alternatives therefore have a stable provider-independent
        # ordering rather than depending on heap insertion order.
        best: Dict[str, Tuple[float, int, Tuple[Tuple[str, str, str], ...]]] = {
            source: (0.0, 0, ())
        }
        queue: List[
            Tuple[float, int, Tuple[Tuple[str, str, str], ...], str]
        ] = [(0.0, 0, (), source)]
        while queue:
            delay, hops, identity, node = heapq.heappop(queue)
            if best.get(node) != (delay, hops, identity):
                continue
            for arc in adjacency[node]:
                arc_identity = (arc["link"], arc["from"], arc["to"])
                candidate = (
                    delay
                    + route_link_delay_ns(
                        platform,
                        arc["link"],
                        arc["from"],
                        arc["to"],
                        route_constraints,
                    ),
                    hops + 1,
                    identity + (arc_identity,),
                )
                if arc["to"] not in best or candidate < best[arc["to"]]:
                    best[arc["to"]] = candidate
                    heapq.heappush(queue, (*candidate, arc["to"]))
        by_sink: Dict[str, Optional[List[Dict[str, Any]]]] = {}
        for sink in sorted(adjacency):
            if sink not in best:
                by_sink[sink] = None
                continue
            by_sink[sink] = [
                {
                    "link": link,
                    "from": left,
                    "to": right,
                }
                for link, left, right in best[sink][2]
            ]
        result[source] = by_sink
    return result


def _capacity_key_by_arc(model: Mapping[str, Any]) -> Dict[Tuple[str, str, str], str]:
    by_link = defaultdict(list)
    for record in model["capacities"]:
        by_link[record["link"]].append(record)
    result = {}
    for source, by_sink in model["shortest_routes"].items():
        for sink, route in by_sink.items():
            if route is None:
                continue
            for arc in route:
                candidates = by_link[arc["link"]]
                shared = next(
                    (item for item in candidates if item["direction"] == "shared"),
                    None,
                )
                record = shared or next(
                    item
                    for item in candidates
                    if item["direction"] == f"{arc['from']}->{arc['to']}"
                )
                result[(arc["link"], arc["from"], arc["to"])] = record["key"]
    return result


def _shortest_hop_distances(
    platform: Platform,
    route_constraints: Mapping[str, Any],
) -> Dict[str, Dict[str, Optional[int]]]:
    adjacency, _, _ = build_directed_graph(platform, route_constraints)
    result: Dict[str, Dict[str, Optional[int]]] = {}
    for source in sorted(adjacency):
        distances = {source: 0}
        queue = deque([source])
        while queue:
            node = queue.popleft()
            for arc in adjacency[node]:
                sink = arc["to"]
                if sink in distances:
                    continue
                distances[sink] = distances[node] + 1
                queue.append(sink)
        result[source] = {
            sink: distances.get(sink) for sink in sorted(adjacency)
        }
    return result
