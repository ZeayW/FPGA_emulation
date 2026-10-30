"""Independent small-fixture oracle for native Chimew signal preparation."""
import math
from collections import defaultdict
from typing import Any, Dict, Mapping, Optional, Tuple
from pathlib import Path
from emuflow_tools.document_io import read_json
from emuflow.common.errors import ValidationError

def _crossed_boundaries(value: float, anchor: float, count: int) -> list[int]:
    if count <= 0:
        return []
    lower, upper = sorted((value, anchor))
    return [
        boundary
        for boundary in range(count)
        if lower < float(boundary + 1) / float(count + 1) <= upper
    ]

def _timing_weights(
    timing_paths_path: Optional[Path],
    schedule: Mapping[str, Any],
    routes: Mapping[str, Any],
    *,
    scale: float = 9.0,
    path_scope: str = "exact-hop",
) -> Tuple[Dict[str, float], Optional[Path], Dict[str, int]]:
    """Return a stable per-entry timing weight for the EmuFlow extension.

    Chimew's published geometric assignment treats signals uniformly.  The
    Xilinx integration may additionally bind partition-projected STA paths
    and use the same bounded power-law criticality employed by EmuFlow's
    timing-driven partitioner.  The native kernel still performs the complete
    matching; this adapter merely materializes the source-bound weights.
    """

    if not math.isfinite(scale) or scale < 0.0:
        raise ValidationError("Xilinx Chimew timing weight scale is invalid")
    if path_scope not in {"exact-hop", "whole-net"}:
        raise ValidationError("Xilinx Chimew timing path scope is invalid")
    if timing_paths_path is None:
        return {}, None, {"exact_path_hops": 0, "whole_net_fallbacks": 0}
    document = read_json(timing_paths_path)
    if document.get("schema") != "emuflow.sta-paths/v1":
        raise ValidationError("Xilinx Chimew timing path schema is invalid")
    if document.get("design") != schedule.get("design"):
        raise ValidationError("Xilinx Chimew timing path design differs")
    entries_by_net: Dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for entry in schedule.get("entries", []):
        entries_by_net[entry["net"]].append(entry)
    route_timing_paths = {
        item["path"]: item
        for item in routes.get("timing", {}).get("paths", [])
        if isinstance(item, Mapping) and isinstance(item.get("path"), str)
    }
    route_by_net = {
        item["net"]: item
        for item in routes.get("routes", [])
        if isinstance(item, Mapping) and isinstance(item.get("net"), str)
    }
    criticality: Dict[str, float] = {}
    paths = document.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ValidationError("Xilinx Chimew timing paths are empty")
    validated_paths: list[Tuple[Mapping[str, Any], float]] = []
    negative_deficits: list[float] = []
    for index, path in enumerate(paths):
        if not isinstance(path, Mapping):
            raise ValidationError(
                f"Xilinx Chimew timing paths[{index}] is invalid"
            )
        period = path.get("clock_period_ns")
        slack = path.get("slack_ns")
        cut_nets = path.get("cut_nets")
        if (
            isinstance(period, bool)
            or not isinstance(period, (int, float))
            or not math.isfinite(float(period))
            or not float(period) > 0.0
            or isinstance(slack, bool)
            or not isinstance(slack, (int, float))
            or not math.isfinite(float(slack))
            or not isinstance(cut_nets, list)
        ):
            raise ValidationError(
                f"Xilinx Chimew timing paths[{index}] is malformed"
            )
        normalized = path.get("normalized_slack")
        if normalized is None:
            normalized = float(slack) / float(period)
        if (
            isinstance(normalized, bool)
            or not isinstance(normalized, (int, float))
            or not math.isfinite(float(normalized))
        ):
            raise ValidationError(
                f"Xilinx Chimew timing paths[{index}] has invalid normalized slack"
            )
        normalized_value = float(normalized)
        validated_paths.append((path, normalized_value))
        negative_deficits.append(max(0.0, -normalized_value))
    maximum_deficit = max(negative_deficits, default=0.0)
    exact_path_hops = 0
    whole_net_fallbacks = 0
    for path, normalized_slack in validated_paths:
        if maximum_deficit > 0.0:
            value = max(0.0, -normalized_slack) / maximum_deficit
        else:
            period = float(path["clock_period_ns"])
            slack = float(path["slack_ns"])
            value = max(0.0, min(1.0, 1.0 - slack / period))
        selected_entries: set[str] = set()
        route_timing = (
            route_timing_paths.get(path.get("id"))
            if path_scope == "exact-hop"
            else None
        )
        if route_timing is not None:
            for transition in route_timing.get("cut_transitions", []):
                if not isinstance(transition, Mapping):
                    raise ValidationError(
                        "Xilinx Chimew route timing transition is invalid"
                    )
                net = transition.get("net")
                source = transition.get("from")
                target = transition.get("to")
                route = route_by_net.get(net)
                if route is None:
                    raise ValidationError(
                        f"Xilinx Chimew timing path route for {net!r} is absent"
                    )
                parents: Dict[str, Tuple[str, str]] = {}
                for edge in route.get("tree_edges", []):
                    if not isinstance(edge, Mapping):
                        raise ValidationError(
                            "Xilinx Chimew route tree edge is invalid"
                        )
                    parents[edge["to"]] = (edge["from"], edge["link"])
                current = target
                while current != source:
                    parent = parents.get(current)
                    if parent is None:
                        raise ValidationError(
                            f"Xilinx Chimew route tree does not reach {target!r}"
                        )
                    previous, link = parent
                    matches = [
                        entry["id"]
                        for entry in entries_by_net.get(net, [])
                        if entry.get("from") == previous
                        and entry.get("to") == current
                        and entry.get("link") == link
                    ]
                    if len(matches) != 1:
                        raise ValidationError(
                            "Xilinx Chimew timing hop does not identify one schedule entry"
                        )
                    selected_entries.add(matches[0])
                    current = previous
            exact_path_hops += len(selected_entries)
        else:
            whole_net_fallbacks += 1
            for net in path["cut_nets"]:
                if not isinstance(net, str):
                    raise ValidationError(
                        "Xilinx Chimew timing path has an invalid cut net"
                    )
                selected_entries.update(
                    entry["id"] for entry in entries_by_net.get(net, [])
                )
        for entry in selected_entries:
            criticality[entry] = max(criticality.get(entry, 0.0), value)
    weights = {
        entry["id"]: 1.0 + scale * criticality.get(entry["id"], 0.0) ** 2.0
        for entry in schedule.get("entries", [])
    }
    return weights, timing_paths_path, {
        "exact_path_hops": exact_path_hops,
        "whole_net_fallbacks": whole_net_fallbacks,
    }
