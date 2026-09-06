"""Contest-derived Chimew validation for the public EDA 2023 die graph.

The contest supplies real node-to-die placement, routed die hops, link kinds,
and TDM ratios.  It does not supply a revision-controlled package-pin BSP or
intra-die site placement.  This adapter therefore exercises the Chimew kernels
at the die/region abstraction and labels the electrical inventory synthetic.
It is an algorithm-scale validation path, not FPGA implementation closure.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .chimew_bank_channel import (
    CHIMEW_BANK_CHANNEL_INPUT_PROVIDER,
    CHIMEW_BANK_CHANNEL_INPUT_SCHEMA,
)
from .chimew_grouping import (
    CHIMEW_ACADEMIC_CROSSING_PROVIDER,
    CHIMEW_CROSSING_SCHEMA,
    build_chimew_initial_groups,
)
from .chimew_phase6 import (
    CHIMEW_ELECTRICAL_MAP_PROVIDER,
    CHIMEW_ELECTRICAL_MAP_SCHEMA,
)
from .chimew_pipeline import (
    run_chimew_phase6_pipeline,
    validate_chimew_phase6_pipeline,
)
from .chimew_qualification import canonical_sha256
from .chimew_refinement import (
    CHIMEW_POSITION_PROVIDER,
    CHIMEW_POSITION_SCHEMA,
    refine_chimew_groups,
)
from .chimew_rudy import CHIMEW_RUDY_INPUT_PROVIDER, CHIMEW_RUDY_INPUT_SCHEMA
from .errors import ValidationError
from .io import read_json, write_json
from .pin_planning import build_pin_plan
from .platform import Platform
from .routing import SYSTEM_ROUTES_SCHEMA
from .tdm import TDM_SCHEDULE_SCHEMA


EDA2023_CONTEST_CHIMEW_MATERIALIZATION_SCHEMA = (
    "emuflow.eda2023-contest-chimew-materialization/v2"
)
EDA2023_CONTEST_CHIMEW_AB_SCHEMA = "emuflow.eda2023-contest-chimew-ab/v2"
EDA2023_CONTEST_CHIMEW_PROVIDER = (
    "eda2023-route-local-sll-lookahead+synthetic-electrical-map-v2"
)
EDA2023_CONTEST_CHIMEW_QUALIFICATION = (
    "contest-derived-virtual-die-algorithm-validation"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_document(path: Path, schema: str, label: str) -> Dict[str, Any]:
    if not path.is_file():
        raise ValidationError(f"EDA 2023 Chimew {label} is missing: {path}")
    document = read_json(path)
    if document.get("schema") != schema:
        raise ValidationError(f"EDA 2023 Chimew {label} schema is invalid")
    return document


def _hierarchy_model(
    instance: Mapping[str, Any], hierarchy: Mapping[str, Any]
) -> Tuple[Dict[str, Tuple[str, int]], Dict[str, Tuple[float, float]], int]:
    raw_fpgas = hierarchy.get("physical_fpgas")
    if not isinstance(raw_fpgas, list) or not raw_fpgas:
        raise ValidationError("EDA 2023 die hierarchy has no physical FPGAs")
    die_location: Dict[str, Tuple[str, int]] = {}
    die_points: Dict[str, Tuple[float, float]] = {}
    maximum_dies = 0
    for fpga_index, record in enumerate(raw_fpgas):
        if not isinstance(record, dict):
            raise ValidationError("EDA 2023 physical FPGA record is invalid")
        fpga = record.get("id")
        dies = record.get("dies")
        if not isinstance(fpga, str) or not fpga or not isinstance(dies, list) or not dies:
            raise ValidationError("EDA 2023 physical FPGA hierarchy is malformed")
        maximum_dies = max(maximum_dies, len(dies))
        for local_index, die in enumerate(dies):
            if not isinstance(die, str) or die in die_location:
                raise ValidationError("EDA 2023 die hierarchy duplicates a die")
            die_location[die] = (fpga, local_index)
            die_points[die] = (float(fpga_index * 10), float(local_index * 2 + 1))
    expected = set(instance.get("dies", []))
    if not expected or set(die_location) != expected:
        raise ValidationError("EDA 2023 die hierarchy does not cover the instance")
    return die_location, die_points, max(1, maximum_dies - 1)


def _guarded_points(points: list[Tuple[float, float]]) -> Tuple[list[Tuple[float, float]], int]:
    unique = list(dict.fromkeys(points))
    if not unique:
        raise ValidationError("EDA 2023 Chimew net has no placed endpoint")
    guards = 0
    if len(unique) == 1:
        unique.append((unique[0][0] + 0.5, unique[0][1] + 0.5))
        return unique, 1
    if max(point[0] for point in unique) == min(point[0] for point in unique):
        unique.append((unique[0][0] + 0.5, unique[0][1]))
        guards += 1
    if max(point[1] for point in unique) == min(point[1] for point in unique):
        unique.append((unique[0][0], unique[0][1] + 0.5))
        guards += 1
    return unique, guards


def _sll_index(
    edge: Mapping[str, Any],
    die_location: Mapping[str, Tuple[str, int]],
) -> int:
    source_fpga, source_index = die_location[edge["from"]]
    sink_fpga, sink_index = die_location[edge["to"]]
    if source_fpga != sink_fpga or abs(source_index - sink_index) != 1:
        raise ValidationError("EDA 2023 SLL link is inconsistent with hierarchy")
    return min(source_index, sink_index)


def _routed_external_crossings(
    routes: Mapping[str, Any],
    instance_links: Mapping[str, Mapping[str, Any]],
    die_location: Mapping[str, Tuple[str, int]],
) -> Dict[Tuple[str, str, str, str], Dict[str, Any]]:
    """Map each routed inter-FPGA edge to its local source/sink SLL masks.

    Phase 5 only emits TDM records for constrained external links.  The SLL
    segments that feed and fan out from that external edge remain in the
    Phase 4 route tree, so derive Chimew's two endpoint encodings from the
    nearest same-FPGA SLL prefix and suffix instead of looking at the TDM
    edge's own link kind.
    """

    raw_routes = routes.get("routes")
    if not isinstance(raw_routes, list):
        raise ValidationError("EDA 2023 routes array is invalid")
    result: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    seen_nets = set()
    for route in raw_routes:
        if not isinstance(route, dict):
            raise ValidationError("EDA 2023 route record is invalid")
        net = route.get("net")
        source = route.get("source")
        route_sinks = route.get("sinks")
        raw_edges = route.get("tree_edges")
        if (
            not isinstance(net, str)
            or not net
            or net in seen_nets
            or source not in die_location
            or not isinstance(route_sinks, list)
            or not route_sinks
            or len(set(route_sinks)) != len(route_sinks)
            or any(sink not in die_location for sink in route_sinks)
            or not isinstance(raw_edges, list)
        ):
            raise ValidationError("EDA 2023 routed tree identity is invalid")
        seen_nets.add(net)
        incoming: Dict[str, Mapping[str, Any]] = {}
        outgoing: Dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        edges = []
        for edge in raw_edges:
            if not isinstance(edge, dict):
                raise ValidationError("EDA 2023 routed tree edge is invalid")
            link_id = edge.get("link")
            edge_source = edge.get("from")
            edge_sink = edge.get("to")
            if (
                link_id not in instance_links
                or edge_source not in die_location
                or edge_sink not in die_location
                or (edge_source, edge_sink)
                not in (
                    tuple(instance_links[link_id]["endpoints"]),
                    tuple(reversed(instance_links[link_id]["endpoints"])),
                )
                or edge_sink in incoming
            ):
                raise ValidationError("EDA 2023 routed tree edge is inconsistent")
            incoming[edge_sink] = edge
            outgoing[edge_source].append(edge)
            edges.append(edge)

        if source in incoming:
            raise ValidationError("EDA 2023 routed tree source has an incoming edge")
        reachable = {source}
        stack = [source]
        while stack:
            node = stack.pop()
            for edge in outgoing.get(node, []):
                if edge["to"] in reachable:
                    raise ValidationError("EDA 2023 routed tree contains a cycle")
                reachable.add(edge["to"])
                stack.append(edge["to"])
        if len(reachable) != len(edges) + 1 or not set(route_sinks).issubset(
            reachable
        ):
            raise ValidationError("EDA 2023 routed tree is disconnected")

        for edge in edges:
            link = instance_links[edge["link"]]
            if link.get("kind") != "wire":
                continue
            source_slls = set()
            node = edge["from"]
            while node != source:
                parent = incoming.get(node)
                if parent is None:
                    raise ValidationError("EDA 2023 routed tree is disconnected")
                parent_link = instance_links[parent["link"]]
                if parent_link.get("kind") != "sll":
                    break
                source_slls.add(_sll_index(parent, die_location))
                node = parent["from"]
            source_anchor = node

            sink_slls = set()
            sink_anchors = set()
            stack = [edge["to"]]
            visited = set()
            while stack:
                node = stack.pop()
                if node in visited:
                    raise ValidationError("EDA 2023 routed tree contains a cycle")
                visited.add(node)
                children = outgoing.get(node, [])
                if node in route_sinks or not children:
                    sink_anchors.add(node)
                for child in children:
                    child_link = instance_links[child["link"]]
                    if child_link.get("kind") != "sll":
                        sink_anchors.add(node)
                        continue
                    sink_slls.add(_sll_index(child, die_location))
                    stack.append(child["to"])
            key = (net, edge["link"], edge["from"], edge["to"])
            if key in result:
                raise ValidationError("EDA 2023 routed external edge is duplicated")
            if not sink_anchors:
                raise ValidationError("EDA 2023 routed external edge has no local sink")
            result[key] = {
                "source_slls": sorted(source_slls),
                "sink_slls": sorted(sink_slls),
                "source_anchor": source_anchor,
                "sink_anchors": sorted(sink_anchors),
            }
    return result


def materialize_eda2023_contest_chimew_inputs(
    *,
    import_dir: Path,
    routes_path: Path,
    tdm_plan_path: Path,
    output_dir: Path,
    grouper: Optional[str] = None,
    refiner: Optional[str] = None,
) -> Dict[str, Any]:
    """Materialize byte-bound Chimew inputs from one frozen EDA 2023 result."""

    instance_path = import_dir / "contest_instance.json"
    hierarchy_path = import_dir / "die_hierarchy.json"
    platform_path = import_dir / "boarddb.json"
    instance = _require_document(
        instance_path, "emuflow.contest-eda2023-instance/v1", "instance"
    )
    hierarchy = _require_document(
        hierarchy_path, "emuflow.die-hierarchy/v1", "die hierarchy"
    )
    routes = _require_document(
        routes_path, SYSTEM_ROUTES_SCHEMA, "routes"
    )
    tdm_plan = _require_document(
        tdm_plan_path, "emuflow.contest-eda2023-tdm/v1", "TDM plan"
    )
    platform = Platform.load(platform_path)
    design = instance.get("name")
    if not isinstance(design, str) or not design:
        raise ValidationError("EDA 2023 instance name is invalid")
    if any(
        identity != design
        for identity in (
            hierarchy.get("platform"),
            routes.get("design"),
            tdm_plan.get("instance"),
            platform.name,
        )
    ):
        raise ValidationError("EDA 2023 Chimew source identities disagree")

    die_location, die_points, sll_count = _hierarchy_model(instance, hierarchy)
    link_by_id = {link.id: link for link in platform.links}
    instance_links = {record["id"]: record for record in instance.get("links", [])}
    if set(instance_links) != set(link_by_id):
        raise ValidationError("EDA 2023 BoardDB does not match contest links")
    known_nets = {f"net_{record['id']:07d}" for record in instance.get("nets", [])}

    raw_hops = tdm_plan.get("hops")
    if not isinstance(raw_hops, list) or not raw_hops:
        raise ValidationError("EDA 2023 Chimew TDM plan has no hops")
    schedule_entries = []
    seen_indices = set()
    for order, hop in enumerate(sorted(raw_hops, key=lambda item: item.get("index", -1))):
        if not isinstance(hop, dict):
            raise ValidationError("EDA 2023 Chimew TDM hop is malformed")
        index = hop.get("index")
        ratio = hop.get("ratio")
        lane = hop.get("lane")
        link_id = hop.get("link")
        source = hop.get("from")
        sink = hop.get("to")
        net = hop.get("net")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or index in seen_indices
            or isinstance(ratio, bool)
            or not isinstance(ratio, int)
            or ratio < 1
            or isinstance(lane, bool)
            or not isinstance(lane, int)
            or lane < 0
            or link_id not in link_by_id
            or source not in die_points
            or sink not in die_points
            or net not in known_nets
        ):
            raise ValidationError("EDA 2023 Chimew TDM hop is invalid")
        link = link_by_id[link_id]
        if (source, sink) not in (link.endpoints, tuple(reversed(link.endpoints))):
            raise ValidationError("EDA 2023 Chimew TDM hop leaves its link")
        if lane >= link.transport_bits_per_cycle_per_direction:
            raise ValidationError("EDA 2023 Chimew TDM lane exceeds BoardDB capacity")
        seen_indices.add(index)
        schedule_entries.append(
            {
                "id": f"contest-hop-{index:09d}",
                "net": net,
                "link": link_id,
                "from": source,
                "to": sink,
                "lane": lane,
                "slot": order,
                "tdm_ratio": ratio,
                "contest_hop_index": index,
                "continuous_ratio": hop.get("continuous_ratio"),
            }
        )
    schedule = {
        "schema": TDM_SCHEDULE_SCHEMA,
        "design": design,
        "platform": platform.name,
        "provider": "eda2023-contest-derived-chimew-schedule-v1",
        "transport_semantics": "registered-boundary",
        "claim_boundary": EDA2023_CONTEST_CHIMEW_QUALIFICATION,
        "entries": schedule_entries,
    }

    routing_sha = _sha256(routes_path)
    placement_sha = _sha256(instance_path)
    architecture_sha = _sha256(hierarchy_path)
    routed_crossings = _routed_external_crossings(
        routes, instance_links, die_location
    )
    crossing_entries = []
    total_crossings = 0
    route_context_by_entry: Dict[str, Dict[str, Any]] = {}
    for entry in schedule_entries:
        link_record = instance_links[entry["link"]]
        if link_record.get("kind") != "wire":
            raise ValidationError(
                "EDA 2023 Phase 6 schedule contains an intra-FPGA SLL hop"
            )
        key = (entry["net"], entry["link"], entry["from"], entry["to"])
        if key not in routed_crossings:
            raise ValidationError(
                "EDA 2023 TDM hop is not bound to a routed external edge"
            )
        route_context = routed_crossings[key]
        route_context_by_entry[entry["id"]] = route_context
        source_slls = route_context["source_slls"]
        sink_slls = route_context["sink_slls"]
        encoding = sum(1 << value for value in source_slls) | (
            sum(1 << value for value in sink_slls) << sll_count
        )
        total_crossings += len(source_slls) + len(sink_slls)
        crossing_entries.append(
            {
                "schedule_entry": entry["id"],
                "source_slls": source_slls,
                "sink_slls": sink_slls,
                "encoding": encoding,
            }
        )
    crossings = {
        "schema": CHIMEW_CROSSING_SCHEMA,
        "provider": CHIMEW_ACADEMIC_CROSSING_PROVIDER,
        "qualification": "academic-virtual-region-lookahead",
        "coordinate_system": "normalized-placement-y",
        "design": design,
        "platform": platform.name,
        "slls_per_fpga": sll_count,
        "provenance": {
            "producer": EDA2023_CONTEST_CHIMEW_PROVIDER,
            "producer_version": "2",
            "routing_sha256": routing_sha,
            "claim_boundary": (
                "contest-routed die/SLL abstraction; not intra-die vendor routing"
            ),
        },
        "metrics": {
            "signals": len(schedule_entries),
            "physical_sll_crossings": total_crossings,
        },
        "entries": crossing_entries,
    }
    positions = {
        "schema": CHIMEW_POSITION_SCHEMA,
        "provider": CHIMEW_POSITION_PROVIDER,
        "design": design,
        "platform": platform.name,
        "coordinate_system": "physical-site-y",
        "provenance": {
            "producer": EDA2023_CONTEST_CHIMEW_PROVIDER,
            "producer_version": "1",
            "placement_sha256": placement_sha,
        },
        "metrics": {"signals": len(schedule_entries)},
        "entries": [
            {
                "schedule_entry": entry["id"],
                "source_y": die_points[
                    route_context_by_entry[entry["id"]]["source_anchor"]
                ][1],
            }
            for entry in schedule_entries
        ],
    }

    initial = build_chimew_initial_groups(schedule, crossings, executable=grouper)
    refined = refine_chimew_groups(
        schedule, crossings, initial, positions, executable=refiner
    )
    group_by_entry = {
        record["schedule_entry"]: record["group"] for record in refined["entries"]
    }
    grouped: Dict[Tuple[str, int], list[Dict[str, Any]]] = defaultdict(list)
    directions: Dict[Tuple[str, int], str] = {}
    entry_by_id = {entry["id"]: entry for entry in schedule_entries}
    for entry_id, group in group_by_entry.items():
        entry = entry_by_id[entry_id]
        link = link_by_id[entry["link"]]
        direction = (
            "a_to_b"
            if (entry["from"], entry["to"]) == link.endpoints
            else "b_to_a"
        )
        key = (entry["link"], group)
        previous = directions.setdefault(key, direction)
        if previous != direction:
            raise ValidationError("EDA 2023 Chimew group mixes link directions")
        grouped[key].append(
            {
                "id": entry_id,
                "fanout": {
                    "x": die_points[
                        route_context_by_entry[entry_id]["source_anchor"]
                    ][0],
                    "y": die_points[
                        route_context_by_entry[entry_id]["source_anchor"]
                    ][1],
                },
                "fanins": [
                    {
                        "x": die_points[sink][0],
                        "y": die_points[sink][1],
                    }
                    for sink in route_context_by_entry[entry_id]["sink_anchors"]
                ],
            }
        )

    groups_by_link: Dict[str, list[Tuple[int, list[Dict[str, Any]]]]] = defaultdict(list)
    for (link_id, group), members in grouped.items():
        groups_by_link[link_id].append((group, members))
    domains = []
    bank_pairs = []
    bank_groups = []
    electrical_channels = []
    package_records = []
    for link_id in sorted(groups_by_link):
        link = link_by_id[link_id]
        endpoint_a, endpoint_b = link.endpoints
        link_groups = sorted(groups_by_link[link_id])
        if len(link_groups) > link.transport_bits_per_cycle_per_direction:
            raise ValidationError("EDA 2023 Chimew groups exceed physical lanes")
        domains.append({"id": link_id, "fpga_a": endpoint_a, "fpga_b": endpoint_b})
        channels = []
        for lane, (group, members) in enumerate(link_groups):
            channel_id = f"contest-{link_id}-channel-{lane:05d}"
            fraction = 0.5 if len(link_groups) == 1 else lane / (len(link_groups) - 1)
            point_a = die_points[endpoint_a]
            point_b = die_points[endpoint_b]
            pin_y_a = point_a[1] - 0.5 + fraction
            pin_y_b = point_b[1] - 0.5 + fraction
            channels.append(
                {
                    "id": channel_id,
                    "order": lane,
                    "pin_a": {"x": point_a[0], "y": pin_y_a},
                    "pin_b": {"x": point_b[0], "y": pin_y_b},
                }
            )
            pin_a = f"ACADEMIC_{endpoint_a}_{link_id}_{lane}_P"
            pin_b = f"ACADEMIC_{endpoint_b}_{link_id}_{lane}_P"
            package_records.extend(
                [{"fpga": endpoint_a, "pin": pin_a}, {"fpga": endpoint_b, "pin": pin_b}]
            )
            electrical_channels.append(
                {
                    "chimew_channel": channel_id,
                    "link": link_id,
                    "physical_lane": lane,
                    "direction": "either",
                    "bank_a": f"contest-{endpoint_a}-{link_id}-bank",
                    "bank_b": f"contest-{endpoint_b}-{link_id}-bank",
                    "package_pin_a": pin_a,
                    "package_pin_b": pin_b,
                    "iostandard": "LVCMOS18",
                    "supported_iostandards": ["LVCMOS18"],
                    "bank_voltage": 1.8,
                    "electrical_class": "single_ended_parallel",
                    "reserved": False,
                }
            )
            bank_groups.append(
                {
                    "id": f"contest-{link_id}-group-{group}",
                    "domain": link_id,
                    "kind": "tdm_group",
                    "direction": directions[(link_id, group)],
                    "members": members,
                }
            )
        point_a = die_points[endpoint_a]
        point_b = die_points[endpoint_b]
        bank_pairs.append(
            {
                "id": f"contest-{link_id}-bank-pair",
                "domain": link_id,
                "bank_a": {
                    "id": f"contest-{endpoint_a}-{link_id}-bank",
                    "x": point_a[0],
                    "y": point_a[1],
                },
                "bank_b": {
                    "id": f"contest-{endpoint_b}-{link_id}-bank",
                    "x": point_b[0],
                    "y": point_b[1],
                },
                "channels": channels,
            }
        )

    sources_dir = output_dir / "sources"
    inputs_dir = output_dir / "inputs"
    sources_dir.mkdir(parents=True, exist_ok=True)
    inputs_dir.mkdir(parents=True, exist_ok=True)
    package_source = sources_dir / "package-pins.json"
    write_json(
        package_source,
        {
            "schema": "emuflow.eda2023-academic-package-pins/v1",
            "qualification": "synthetic-algorithm-validation-only",
            "pins": package_records,
        },
    )
    package_sha = _sha256(package_source)

    rudy_nets = []
    guard_points = 0
    for net in instance.get("nets", []):
        dies = [net.get("source_die"), *net.get("sink_dies", [])]
        if any(die not in die_points for die in dies):
            raise ValidationError("EDA 2023 net placement references an unknown die")
        points, guards = _guarded_points([die_points[die] for die in dies])
        guard_points += guards
        rudy_nets.append(
            {
                "id": f"contest-net-{net['id']:07d}",
                "pins": [{"x": point[0], "y": point[1]} for point in points],
            }
        )
    max_x = max(point["x"] for net in rudy_nets for point in net["pins"]) + 1.0
    max_y = max(point["y"] for net in rudy_nets for point in net["pins"]) + 1.0
    capacity = max(1.0, len(rudy_nets) * (max_x + max_y) * 4.0)
    rudy_input = {
        "schema": CHIMEW_RUDY_INPUT_SCHEMA,
        "provider": CHIMEW_RUDY_INPUT_PROVIDER,
        "design": design,
        "platform": platform.name,
        "coordinate_system": "physical-site-xy",
        "degenerate_bbox_policy": "reject",
        "wire_pitch_per_layer": 1.0,
        "max_utilization": 1.0,
        "provenance": {
            "producer": EDA2023_CONTEST_CHIMEW_PROVIDER,
            "producer_version": "1",
            "placement_sha256": placement_sha,
            "netlist_sha256": placement_sha,
            "architecture_sha256": architecture_sha,
        },
        "grid": {
            "origin_x": 0.0,
            "origin_y": 0.0,
            "bin_width": max_x,
            "bin_height": max_y,
            "columns": 1,
            "rows": 1,
            "capacities": [capacity],
        },
        "academic_bbox_guard_points": guard_points,
        "metrics": {
            "nets": len(rudy_nets),
            "pins": sum(len(net["pins"]) for net in rudy_nets),
        },
        "nets": rudy_nets,
    }
    bank_input = {
        "schema": CHIMEW_BANK_CHANNEL_INPUT_SCHEMA,
        "provider": CHIMEW_BANK_CHANNEL_INPUT_PROVIDER,
        "design": design,
        "platform": platform.name,
        "coordinate_system": "physical-site-xy",
        "cost_quantization_per_site": 1000,
        "provenance": {
            "producer": EDA2023_CONTEST_CHIMEW_PROVIDER,
            "producer_version": "1",
            "grouping_sha256": canonical_sha256(refined),
            "placement_sha256": placement_sha,
            "architecture_sha256": architecture_sha,
        },
        "domains": domains,
        "bank_pairs": bank_pairs,
        "groups": bank_groups,
        "metrics": {
            "groups": len(bank_groups),
            "signals": len(schedule_entries),
            "fanins": sum(
                len(member["fanins"])
                for group in bank_groups
                for member in group["members"]
            ),
            "bank_pairs": len(bank_pairs),
            "channels": len(electrical_channels),
        },
    }
    domain_endpoints = sorted(
        {
            endpoint
            for domain in domains
            for endpoint in (domain["fpga_a"], domain["fpga_b"])
        }
    )
    physical_fpga_ys: Dict[str, list[float]] = defaultdict(list)
    for die, (physical_fpga, _) in die_location.items():
        physical_fpga_ys[physical_fpga].append(die_points[die][1])
    electrical_map = {
        "schema": CHIMEW_ELECTRICAL_MAP_SCHEMA,
        "provider": CHIMEW_ELECTRICAL_MAP_PROVIDER,
        "design": design,
        "platform": platform.name,
        "provenance": {
            "producer": EDA2023_CONTEST_CHIMEW_PROVIDER,
            "producer_version": "1",
            "boarddb_sha256": _sha256(platform_path),
            "package_pin_inventory_sha256": package_sha,
        },
        "fpga_y_bounds": [
            {
                "fpga": die,
                "y_min": min(physical_fpga_ys[die_location[die][0]]) - 1.0,
                "y_max": max(physical_fpga_ys[die_location[die][0]]) + 1.0,
            }
            for die in domain_endpoints
        ],
        "channels": electrical_channels,
        "metrics": {
            "channels": len(electrical_channels),
            "package_pins": len(package_records),
            "concrete_lanes": len(electrical_channels),
        },
    }
    documents = {
        "schedule": schedule,
        "crossings": crossings,
        "positions": positions,
        "rudy_input": rudy_input,
        "bank_channel_input": bank_input,
        "electrical_map": electrical_map,
    }
    paths = {}
    for label, document in documents.items():
        path = (
            output_dir / "schedule.json"
            if label == "schedule"
            else inputs_dir / f"{label}.json"
        )
        write_json(path, document)
        paths[label] = path
    report = {
        "schema": EDA2023_CONTEST_CHIMEW_MATERIALIZATION_SCHEMA,
        "status": "pass",
        "provider": EDA2023_CONTEST_CHIMEW_PROVIDER,
        "qualification": EDA2023_CONTEST_CHIMEW_QUALIFICATION,
        "claim_boundary": (
            "real contest node-to-die placement, routed die/SLL hops, and TDM ratios; "
            "synthetic package pins; no intra-die placement, vendor routing, timing, or DRC"
        ),
        "design": design,
        "platform": platform.name,
        "metrics": {
            "signals": len(schedule_entries),
            "nets": len(rudy_nets),
            "groups": len(bank_groups),
            "routed_sll_hops": total_crossings,
            "virtual_package_pins": len(package_records),
            "academic_bbox_guard_points": guard_points,
        },
        "artifacts": {
            label: {"path": str(path), "sha256": _sha256(path)}
            for label, path in paths.items()
        },
        "sources": {
            "routing": str(routes_path),
            "placement": str(instance_path),
            "netlist": str(instance_path),
            "architecture": str(hierarchy_path),
            "package_pins": str(package_source),
            "platform": str(platform_path),
            "tdm_plan": str(tdm_plan_path),
        },
    }
    write_json(output_dir / "materialization_report.json", report)
    return report


def _routed_group_metrics(
    plan: Mapping[str, Any],
    crossings: Mapping[str, Any],
    positions: Mapping[str, Any],
) -> Dict[str, float]:
    group_by_entry = {
        record["schedule_entry"]: record["group"] for record in plan["entries"]
    }
    encoding_by_entry = {
        record["schedule_entry"]: record["encoding"]
        for record in crossings["entries"]
    }
    position_by_entry = {
        record["schedule_entry"]: float(record["source_y"])
        for record in positions["entries"]
    }
    if set(group_by_entry) != set(encoding_by_entry) or set(group_by_entry) != set(
        position_by_entry
    ):
        raise ValidationError("EDA 2023 A/B plans do not cover routed signals")
    encodings: Dict[int, int] = defaultdict(int)
    grouped_positions: Dict[int, list[float]] = defaultdict(list)
    for entry_id, group in group_by_entry.items():
        encodings[group] |= encoding_by_entry[entry_id]
        grouped_positions[group].append(position_by_entry[entry_id])
    pairwise_y = 0.0
    for values in grouped_positions.values():
        prefix = 0.0
        for index, value in enumerate(sorted(values)):
            pairwise_y += value * index - prefix
            prefix += value
    return {
        "routed_sll_crossing_bits": float(
            sum(bin(encoding).count("1") for encoding in encodings.values())
        ),
        "routed_source_pairwise_y": pairwise_y,
    }


def run_eda2023_contest_chimew_ab(
    *,
    import_dir: Path,
    routes_path: Path,
    tdm_plan_path: Path,
    output_dir: Path,
    grouper: Optional[str] = None,
    refiner: Optional[str] = None,
    rudy: Optional[str] = None,
    assigner: Optional[str] = None,
    pin_planner: Optional[str] = None,
) -> Dict[str, Any]:
    """Run source-bound Chimew and the previous placement-aware baseline."""

    materialization = materialize_eda2023_contest_chimew_inputs(
        import_dir=import_dir,
        routes_path=routes_path,
        tdm_plan_path=tdm_plan_path,
        output_dir=output_dir / "materialized",
        grouper=grouper,
        refiner=refiner,
    )
    artifacts = materialization["artifacts"]
    sources = materialization["sources"]
    pipeline_root = output_dir / "chimew"
    pipeline = run_chimew_phase6_pipeline(
        Path(artifacts["schedule"]["path"]),
        Path(sources["platform"]),
        Path(artifacts["crossings"]["path"]),
        Path(artifacts["positions"]["path"]),
        Path(artifacts["rudy_input"]["path"]),
        Path(artifacts["bank_channel_input"]["path"]),
        Path(artifacts["electrical_map"]["path"]),
        pipeline_root,
        source_paths={
            label: Path(sources[label])
            for label in ("routing", "placement", "netlist", "architecture", "package_pins")
        },
        grouper=grouper,
        refiner=refiner,
        rudy=rudy,
        assigner=assigner,
        region_count=4,
    )
    validate_chimew_phase6_pipeline(pipeline_root)
    schedule = read_json(Path(artifacts["schedule"]["path"]))
    platform = Platform.load(Path(sources["platform"]))
    position_hints = read_json(pipeline_root / "phase6-adapter" / "position_hints.json")
    baseline = build_pin_plan(
        schedule,
        platform,
        position_hints,
        executable=pin_planner,
    )
    baseline_path = output_dir / "baseline_pin_plan.json"
    write_json(baseline_path, baseline)
    chimew_plan = read_json(pipeline_root / "phase6-adapter" / "pin_plan.json")
    crossings = read_json(Path(artifacts["crossings"]["path"]))
    positions = read_json(Path(artifacts["positions"]["path"]))
    metric_fields = ("objective", "crossing_bits", "position_sse", "pin_distance")
    baseline_metrics = {field: baseline["metrics"][field] for field in metric_fields}
    chimew_metrics = {field: chimew_plan["metrics"][field] for field in metric_fields}
    baseline_metrics.update(_routed_group_metrics(baseline, crossings, positions))
    chimew_metrics.update(_routed_group_metrics(chimew_plan, crossings, positions))
    comparison_fields = (
        *metric_fields,
        "routed_sll_crossing_bits",
        "routed_source_pairwise_y",
    )
    improvements = {
        f"{field}_improvement_percent": (
            100.0 * (baseline_metrics[field] - chimew_metrics[field]) / baseline_metrics[field]
            if baseline_metrics[field]
            else 0.0
        )
        for field in comparison_fields
    }
    report = {
        "schema": EDA2023_CONTEST_CHIMEW_AB_SCHEMA,
        "status": "pass",
        "provider": EDA2023_CONTEST_CHIMEW_PROVIDER,
        "qualification": EDA2023_CONTEST_CHIMEW_QUALIFICATION,
        "claim_boundary": materialization["claim_boundary"],
        "design": materialization["design"],
        "platform": materialization["platform"],
        "metrics": {
            **materialization["metrics"],
            "baseline": baseline_metrics,
            "chimew": chimew_metrics,
            **improvements,
        },
        "artifacts": {
            "materialization_report": str(
                output_dir / "materialized" / "materialization_report.json"
            ),
            "pipeline_report": str(pipeline_root / "pipeline_report.json"),
            "baseline_pin_plan": str(baseline_path),
            "chimew_pin_plan": str(pipeline_root / "phase6-adapter" / "pin_plan.json"),
        },
        "pipeline_qualification_sha256": pipeline["qualification_sha256"],
    }
    write_json(output_dir / "ab_report.json", report)
    return report
