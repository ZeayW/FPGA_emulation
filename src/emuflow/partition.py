from __future__ import annotations

import fnmatch
import hashlib
import math
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .errors import ValidationError
from .io import read_json
from .ir import EmuIR
from .platform import Platform
from .resources import RESOURCE_FIELDS, ResourceVector
from .combinational_cut import (
    STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
    STATIC_EXACT_CANDIDATE_FRONTIER_V1,
    STATIC_EXACT_CANDIDATE_POLICIES,
    _build_combinational_cut_candidate_index,
)


CLUSTERS_SCHEMA = "emuflow.clusters/v1"
PARTITION_ASSIGNMENT_SCHEMA = "emuflow.partition-assignment/v1"
PARTITION_CONSTRAINTS_SCHEMA = "emuflow.partition-constraints/v1"
TRANSPORTED_CUT_CLASSES = {"register_output", "register_input"}
LEGAL_CUT_CLASSES = {*TRANSPORTED_CUT_CLASSES, "primary_input"}
REPLICATED_NET_CLASSES = {"clock", "reset", "primary_input"}
CUT_MODE_SEQUENTIAL_ONLY = "sequential-only"
CUT_MODE_STATIC_EXACT = "static-exact-combinational"
HARD_MACRO_RESOURCES = {
    "bram",
    "dsp",
    "carry",
    "bram18k",
    "uram288",
    "dsp48",
    "carry8",
}


class _UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, item: int) -> int:
        parent = self.parent[item]
        while parent != self.parent[parent]:
            parent = self.parent[parent]
        while item != parent:
            next_item = self.parent[item]
            self.parent[item] = parent
            item = next_item
        return parent

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1


def _instance_ids_on_net(net: Mapping[str, Any]) -> List[str]:
    return sorted(
        {
            endpoint["instance"]
            for collection in ("drivers", "sinks")
            for endpoint in net[collection]
            if endpoint["instance"] is not None
        }
    )


def _sum_resources(
    instance_ids: Iterable[str],
    instances: Mapping[str, Mapping[str, Any]],
) -> ResourceVector:
    return ResourceVector.sum(
        ResourceVector.from_mapping(instances[instance_id]["resources"])
        for instance_id in instance_ids
    )


def _expand_instance_patterns(
    patterns: Sequence[str],
    instance_ids: Sequence[str],
    context: str,
) -> List[str]:
    matches: Set[str] = set()
    for pattern_index, pattern in enumerate(patterns):
        if not isinstance(pattern, str) or not pattern:
            raise ValidationError(
                f"{context}[{pattern_index}]: expected a non-empty string"
            )
        pattern_matches = [
            instance_id
            for instance_id in instance_ids
            if fnmatch.fnmatchcase(instance_id, pattern)
        ]
        if not pattern_matches:
            raise ValidationError(
                f"{context}[{pattern_index}]: pattern {pattern!r} matched no instances"
            )
        matches.update(pattern_matches)
    return sorted(matches)


def normalize_partition_constraints(
    value: Optional[Mapping[str, Any]],
    ir: EmuIR,
    platform: Platform,
    min_used_fpgas: Optional[int] = None,
    balance_tolerance: Optional[float] = None,
) -> Dict[str, Any]:
    raw: Mapping[str, Any] = value or {}
    if raw and raw.get("schema") != PARTITION_CONSTRAINTS_SCHEMA:
        raise ValidationError(
            "constraints.schema: expected "
            f"{PARTITION_CONSTRAINTS_SCHEMA!r}, got {raw.get('schema')!r}"
        )

    instance_ids = sorted(instance["id"] for instance in ir.value["instances"])
    instance_set = set(instance_ids)
    fpga_ids = {fpga.id for fpga in platform.fpgas}

    raw_groups = raw.get("groups", [])
    if not isinstance(raw_groups, list):
        raise ValidationError("constraints.groups: expected an array")
    groups: List[Dict[str, Any]] = []
    group_ids: Set[str] = set()
    for index, item in enumerate(raw_groups):
        if not isinstance(item, dict):
            raise ValidationError(f"constraints.groups[{index}]: expected an object")
        group_id = item.get("id")
        if not isinstance(group_id, str) or not group_id:
            raise ValidationError(
                f"constraints.groups[{index}].id: expected a non-empty string"
            )
        if group_id in group_ids:
            raise ValidationError(
                f"constraints.groups[{index}].id: duplicate {group_id!r}"
            )
        group_ids.add(group_id)
        raw_instances = item.get("instances", [])
        raw_patterns = item.get("patterns", [])
        if not isinstance(raw_instances, list) or not all(
            isinstance(instance_id, str) for instance_id in raw_instances
        ):
            raise ValidationError(
                f"constraints.groups[{index}].instances: expected strings"
            )
        if not isinstance(raw_patterns, list):
            raise ValidationError(
                f"constraints.groups[{index}].patterns: expected an array"
            )
        unknown = sorted(set(raw_instances) - instance_set)
        if unknown:
            raise ValidationError(
                f"constraints.groups[{index}].instances: unknown instances {unknown}"
            )
        expanded = set(raw_instances)
        expanded.update(
            _expand_instance_patterns(
                raw_patterns,
                instance_ids,
                f"constraints.groups[{index}].patterns",
            )
        )
        if not expanded:
            raise ValidationError(
                f"constraints.groups[{index}]: expected at least one instance"
            )
        groups.append({"id": group_id, "instances": sorted(expanded)})

    raw_fixed = raw.get("fixed", [])
    if not isinstance(raw_fixed, list):
        raise ValidationError("constraints.fixed: expected an array")
    fixed_by_instance: Dict[str, str] = {}
    fixed: List[Dict[str, str]] = []
    for index, item in enumerate(raw_fixed):
        if not isinstance(item, dict):
            raise ValidationError(f"constraints.fixed[{index}]: expected an object")
        fpga_id = item.get("fpga")
        if fpga_id not in fpga_ids:
            raise ValidationError(
                f"constraints.fixed[{index}].fpga: unknown FPGA {fpga_id!r}"
            )
        raw_instances = item.get("instances", [])
        if "instance" in item:
            raw_instances = list(raw_instances) + [item.get("instance")]
        raw_patterns = item.get("patterns", [])
        if not isinstance(raw_instances, list) or not all(
            isinstance(instance_id, str) for instance_id in raw_instances
        ):
            raise ValidationError(
                f"constraints.fixed[{index}].instances: expected strings"
            )
        if not isinstance(raw_patterns, list):
            raise ValidationError(
                f"constraints.fixed[{index}].patterns: expected an array"
            )
        unknown = sorted(set(raw_instances) - instance_set)
        if unknown:
            raise ValidationError(
                f"constraints.fixed[{index}].instances: unknown instances {unknown}"
            )
        expanded = set(raw_instances)
        expanded.update(
            _expand_instance_patterns(
                raw_patterns,
                instance_ids,
                f"constraints.fixed[{index}].patterns",
            )
        )
        if not expanded:
            raise ValidationError(
                f"constraints.fixed[{index}]: expected at least one instance"
            )
        for instance_id in sorted(expanded):
            previous = fixed_by_instance.get(instance_id)
            if previous is not None and previous != fpga_id:
                raise ValidationError(
                    f"constraints.fixed: instance {instance_id!r} is fixed to "
                    f"both {previous!r} and {fpga_id!r}"
                )
            fixed_by_instance[instance_id] = fpga_id
            fixed.append({"instance": instance_id, "fpga": fpga_id})

    raw_min_used = raw.get("min_used_fpgas", len(platform.fpgas))
    if min_used_fpgas is not None:
        raw_min_used = min_used_fpgas
    if (
        isinstance(raw_min_used, bool)
        or not isinstance(raw_min_used, int)
        or raw_min_used <= 0
        or raw_min_used > len(platform.fpgas)
    ):
        raise ValidationError(
            "constraints.min_used_fpgas: expected an integer between 1 and "
            f"{len(platform.fpgas)}"
        )

    raw_tolerance = raw.get("balance_tolerance", 0.10)
    if balance_tolerance is not None:
        raw_tolerance = balance_tolerance
    if (
        isinstance(raw_tolerance, bool)
        or not isinstance(raw_tolerance, (int, float))
        or float(raw_tolerance) < 0.0
    ):
        raise ValidationError(
            "constraints.balance_tolerance: expected a non-negative number"
        )
    raw_dimension_tolerances = raw.get("balance_tolerance_by_dimension", {})
    if not isinstance(raw_dimension_tolerances, dict):
        raise ValidationError(
            "constraints.balance_tolerance_by_dimension: expected an object"
        )
    valid_dimensions = {"cells", *RESOURCE_FIELDS}
    unknown_dimensions = sorted(
        set(raw_dimension_tolerances) - valid_dimensions
    )
    if unknown_dimensions:
        raise ValidationError(
            "constraints.balance_tolerance_by_dimension: unknown dimensions "
            f"{unknown_dimensions}"
        )
    dimension_tolerances: Dict[str, float] = {}
    for dimension, value in raw_dimension_tolerances.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or float(value) < 0.0
        ):
            raise ValidationError(
                "constraints.balance_tolerance_by_dimension."
                f"{dimension}: expected a non-negative number"
            )
        dimension_tolerances[dimension] = float(value)

    return {
        "schema": PARTITION_CONSTRAINTS_SCHEMA,
        "groups": groups,
        "fixed": sorted(
            fixed, key=lambda item: (item["instance"], item["fpga"])
        ),
        "min_used_fpgas": raw_min_used,
        "balance_tolerance": float(raw_tolerance),
        "balance_tolerance_by_dimension": dict(
            sorted(dimension_tolerances.items())
        ),
    }


def load_partition_constraints(
    path: Optional[Path],
    ir: EmuIR,
    platform: Platform,
    min_used_fpgas: Optional[int] = None,
    balance_tolerance: Optional[float] = None,
) -> Dict[str, Any]:
    value = read_json(path) if path is not None else None
    return normalize_partition_constraints(
        value,
        ir,
        platform,
        min_used_fpgas=min_used_fpgas,
        balance_tolerance=balance_tolerance,
    )


def build_clusters(
    ir: EmuIR,
    constraints: Mapping[str, Any],
    cut_mode: str = CUT_MODE_SEQUENTIAL_ONLY,
    max_cross_fpga_dependency_depth: int = 1,
    comb_segment_budget_slots: int = 1,
    frame_slots: int = 2,
    static_exact_candidate_policy: str = STATIC_EXACT_CANDIDATE_FRONTIER_V1,
) -> Dict[str, Any]:
    if cut_mode not in {CUT_MODE_SEQUENTIAL_ONLY, CUT_MODE_STATIC_EXACT}:
        raise ValidationError(
            "partition cut mode must be 'sequential-only' or "
            "'static-exact-combinational'"
        )
    released_combinational_nets: Set[str] = set()
    candidate_index = None
    if cut_mode == CUT_MODE_STATIC_EXACT:
        if len(ir.value["clocks"]) != 1:
            raise ValidationError(
                "static exact combinational cuts require exactly one "
                "virtual DUT clock"
            )
        if static_exact_candidate_policy not in STATIC_EXACT_CANDIDATE_POLICIES:
            raise ValidationError("unknown static exact candidate policy")
        if (
            isinstance(max_cross_fpga_dependency_depth, bool)
            or not isinstance(max_cross_fpga_dependency_depth, int)
            or max_cross_fpga_dependency_depth <= 0
            or (
                static_exact_candidate_policy
                == STATIC_EXACT_CANDIDATE_FRONTIER_V1
                and max_cross_fpga_dependency_depth not in {1, 2}
            )
        ):
            if static_exact_candidate_policy == STATIC_EXACT_CANDIDATE_FRONTIER_V1:
                raise ValidationError(
                    "legacy static exact candidate policy requires "
                    "max_cross_fpga_dependency_depth to be 1 or 2"
                )
            raise ValidationError(
                "max_cross_fpga_dependency_depth must be positive"
            )
        if (
            isinstance(comb_segment_budget_slots, bool)
            or not isinstance(comb_segment_budget_slots, int)
            or comb_segment_budget_slots <= 0
        ):
            raise ValidationError(
                "comb_segment_budget_slots must be a positive integer"
            )
        if (
            isinstance(frame_slots, bool)
            or not isinstance(frame_slots, int)
            or frame_slots < 2
        ):
            raise ValidationError("frame_slots must be an integer at least two")
        candidate_index = _build_combinational_cut_candidate_index(
            ir,
            include_dependency_levels=(
                static_exact_candidate_policy
                == STATIC_EXACT_CANDIDATE_FRONTIER_V1
            ),
            include_source_identity=True,
        )
        if static_exact_candidate_policy == STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2:
            # Candidate depth is not intrinsic to a net.  A net deep in the
            # potential-cut DAG can still be the only transported boundary in
            # its cone and therefore have actual dependency depth one.  V2
            # releases every structurally legal candidate and reconstructs the
            # depth only after the provider supplies an assignment.
            released_combinational_nets = set(candidate_index["eligible_ids"])
        else:
            released_combinational_nets = {
                net_id
                for net_id, level in candidate_index[
                    "dependency_levels"
                ].items()
                if level <= max_cross_fpga_dependency_depth
            }
    instances = {
        instance["id"]: instance for instance in ir.value["instances"]
    }
    instance_ids = sorted(instances)
    index_by_id = {
        instance_id: index for index, instance_id in enumerate(instance_ids)
    }
    union_find = _UnionFind(len(instance_ids))

    for group in constraints["groups"]:
        members = group["instances"]
        for member in members[1:]:
            union_find.union(index_by_id[members[0]], index_by_id[member])

    for net in ir.value["nets"]:
        members = _instance_ids_on_net(net)
        if len(members) < 2:
            continue
        if (
            net["cut_class"] not in LEGAL_CUT_CLASSES | REPLICATED_NET_CLASSES
            and net["id"] not in released_combinational_nets
        ):
            for member in members[1:]:
                union_find.union(index_by_id[members[0]], index_by_id[member])
    members_by_root: Dict[int, List[str]] = defaultdict(list)
    for instance_id in instance_ids:
        members_by_root[union_find.find(index_by_id[instance_id])].append(
            instance_id
        )

    fixed_by_instance = {
        item["instance"]: item["fpga"] for item in constraints["fixed"]
    }
    groups_by_instance: Dict[str, List[str]] = defaultdict(list)
    for group in constraints["groups"]:
        for instance_id in group["instances"]:
            groups_by_instance[instance_id].append(group["id"])

    raw_clusters = sorted(
        (sorted(members) for members in members_by_root.values()),
        key=lambda members: members[0],
    )
    clusters: List[Dict[str, Any]] = []
    for index, members in enumerate(raw_clusters):
        fixed_fpgas = {
            fixed_by_instance[member]
            for member in members
            if member in fixed_by_instance
        }
        if len(fixed_fpgas) > 1:
            raise ValidationError(
                f"cluster containing {members[0]!r} has conflicting fixed FPGA "
                f"constraints {sorted(fixed_fpgas)}"
            )
        clusters.append(
            {
                "id": f"c{index:06d}",
                "instances": members,
                "resources": _sum_resources(members, instances).to_dict(
                    include_zeros=False
                ),
                "fixed_fpga": next(iter(fixed_fpgas), None),
                "groups": sorted(
                    {
                        group_id
                        for member in members
                        for group_id in groups_by_instance.get(member, [])
                    }
                ),
            }
        )

    result = {
        "schema": CLUSTERS_SCHEMA,
        "design": ir.value["design"]["name"],
        "clusters": clusters,
        "instances": len(instance_ids),
        "policy": {
            "legal_cut_classes": sorted(LEGAL_CUT_CLASSES),
            "replicated_net_classes": sorted(REPLICATED_NET_CLASSES),
            "hard_macro_resources": sorted(HARD_MACRO_RESOURCES),
            "hard_macro_granularity": "instance",
        },
    }
    if cut_mode == CUT_MODE_STATIC_EXACT:
        assert candidate_index is not None
        result["policy"].update(
            {
                "cut_mode": cut_mode,
                "max_cross_fpga_dependency_depth": (
                    max_cross_fpga_dependency_depth
                ),
                "comb_segment_budget_slots": comb_segment_budget_slots,
                "frame_slots": frame_slots,
                "eligible_combinational_cut_nets": sorted(
                    released_combinational_nets
                ),
                "transported_cut_classes": sorted(
                    {*TRANSPORTED_CUT_CLASSES, "combinational"}
                ),
                "characterization_source_sha256": candidate_index[
                    "canonical_emuir_sha256"
                ],
                "qualification": "partition-legality-only-provisional",
            }
        )
        if static_exact_candidate_policy == STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2:
            result["policy"]["candidate_selection_policy"] = (
                static_exact_candidate_policy
            )
    return result


def transported_cut_classes_for_clusters(
    clusters_artifact: Mapping[str, Any],
) -> Set[str]:
    raw = clusters_artifact.get("policy", {}).get("transported_cut_classes")
    if raw is None:
        return set(TRANSPORTED_CUT_CLASSES)
    if not isinstance(raw, list) or not all(
        isinstance(item, str) for item in raw
    ):
        raise ValidationError(
            "clusters.policy.transported_cut_classes: expected strings"
        )
    result = set(raw)
    allowed = {*TRANSPORTED_CUT_CLASSES, "combinational"}
    if not set(TRANSPORTED_CUT_CLASSES).issubset(result) or not result <= allowed:
        raise ValidationError(
            "clusters.policy.transported_cut_classes is not a supported policy"
        )
    return result


def _cluster_adjacency(
    ir: EmuIR,
    cluster_by_instance: Mapping[str, str],
    transported_cut_classes: Set[str],
) -> Dict[str, Dict[str, int]]:
    adjacency: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for net in ir.value["nets"]:
        if net["cut_class"] not in transported_cut_classes:
            continue
        driver_clusters = {
            cluster_by_instance[endpoint["instance"]]
            for endpoint in net["drivers"]
            if endpoint["instance"] is not None
        }
        sink_clusters = {
            cluster_by_instance[endpoint["instance"]]
            for endpoint in net["sinks"]
            if endpoint["instance"] is not None
        }
        for driver_cluster in driver_clusters:
            for sink_cluster in sink_clusters:
                if driver_cluster == sink_cluster:
                    continue
                adjacency[driver_cluster][sink_cluster] += 1
                adjacency[sink_cluster][driver_cluster] += 1
    return adjacency


def _cluster_transport_arcs(
    ir: EmuIR,
    cluster_by_instance: Mapping[str, str],
    transported_cut_classes: Set[str],
) -> Dict[str, Set[Tuple[str, bool]]]:
    """Return neighbor arcs and whether the keyed cluster drives the arc."""
    arcs: Dict[str, Set[Tuple[str, bool]]] = defaultdict(set)
    for net in ir.value["nets"]:
        if net["cut_class"] not in transported_cut_classes:
            continue
        driver_clusters = {
            cluster_by_instance[endpoint["instance"]]
            for endpoint in net["drivers"]
            if endpoint["instance"] is not None
        }
        sink_clusters = {
            cluster_by_instance[endpoint["instance"]]
            for endpoint in net["sinks"]
            if endpoint["instance"] is not None
        }
        for driver_cluster in driver_clusters:
            for sink_cluster in sink_clusters:
                if driver_cluster == sink_cluster:
                    continue
                arcs[driver_cluster].add((sink_cluster, True))
                arcs[sink_cluster].add((driver_cluster, False))
    return arcs


def _resource_add(
    left: Mapping[str, int], right: Mapping[str, int]
) -> Dict[str, int]:
    return {
        field: left.get(field, 0) + right.get(field, 0)
        for field in RESOURCE_FIELDS
    }


def _fits(resources: Mapping[str, int], capacity: Mapping[str, int]) -> bool:
    return all(resources.get(field, 0) <= limit for field, limit in capacity.items())


def _seeded_tie(seed: int, cluster_id: str) -> str:
    return hashlib.sha256(f"{seed}:{cluster_id}".encode("utf-8")).hexdigest()


def _partition_hop_distances(
    platform: Platform,
    route_constraints: Mapping[str, Any],
) -> Dict[str, Dict[str, Optional[int]]]:
    # Import locally because routing owns the canonical directed BoardDB graph
    # and imports the partition artifact schema at module initialization.
    from .routing import (
        SYSTEM_ROUTE_CONSTRAINTS_SCHEMA,
        build_directed_graph,
        normalize_route_constraints,
    )

    raw_constraints = dict(route_constraints)
    raw_constraints.setdefault("schema", SYSTEM_ROUTE_CONSTRAINTS_SCHEMA)
    normalized = normalize_route_constraints(raw_constraints, platform)
    adjacency, _, _ = build_directed_graph(platform, normalized)
    result: Dict[str, Dict[str, Optional[int]]] = {}
    for source in sorted(adjacency):
        distance = {source: 0}
        queue = deque([source])
        while queue:
            node = queue.popleft()
            for arc in adjacency[node]:
                neighbor = arc["to"]
                if neighbor in distance:
                    continue
                distance[neighbor] = distance[node] + 1
                queue.append(neighbor)
        result[source] = {
            sink: distance.get(sink) for sink in sorted(adjacency)
        }
    return result


def assign_clusters(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    seed: int = 0,
    route_constraints: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValidationError("partition seed: expected a non-negative integer")

    clusters = {
        cluster["id"]: cluster for cluster in clusters_artifact["clusters"]
    }
    if len(clusters) < constraints["min_used_fpgas"]:
        raise ValidationError(
            f"partitioning has {len(clusters)} atomic clusters but "
            f"{constraints['min_used_fpgas']} FPGAs must be used"
        )
    cluster_by_instance = {
        instance_id: cluster_id
        for cluster_id, cluster in clusters.items()
        for instance_id in cluster["instances"]
    }
    transported_cut_classes = transported_cut_classes_for_clusters(
        clusters_artifact
    )
    adjacency = _cluster_adjacency(
        ir, cluster_by_instance, transported_cut_classes
    )
    transport_arcs = _cluster_transport_arcs(
        ir, cluster_by_instance, transported_cut_classes
    )
    hop_limit = (
        route_constraints.get("max_route_hops")
        if route_constraints is not None
        else None
    )
    hop_distances = (
        _partition_hop_distances(platform, route_constraints)
        if hop_limit is not None
        else None
    )
    fpga_ids = [fpga.id for fpga in platform.fpgas]
    effective_capacity = {
        fpga.id: fpga.effective_capacity for fpga in platform.fpgas
    }
    loads = {
        fpga_id: {field: 0 for field in RESOURCE_FIELDS}
        for fpga_id in fpga_ids
    }

    total_resources = ResourceVector.sum(
        ResourceVector.from_mapping(cluster["resources"])
        for cluster in clusters.values()
    ).to_dict()
    soft_caps: Dict[str, Dict[str, int]] = {
        fpga_id: {} for fpga_id in fpga_ids
    }
    tolerance = constraints["balance_tolerance"]
    dimension_tolerances = constraints.get(
        "balance_tolerance_by_dimension", {}
    )
    for field in RESOURCE_FIELDS:
        total = total_resources[field]
        capacities = {
            fpga_id: effective_capacity[fpga_id].get(field, 0)
            for fpga_id in fpga_ids
        }
        capacity_total = sum(capacities.values())
        if total == 0 or capacity_total == 0:
            continue
        for fpga_id in fpga_ids:
            proportional = total * capacities[fpga_id] / capacity_total
            field_tolerance = dimension_tolerances.get(field, tolerance)
            soft_caps[fpga_id][field] = min(
                capacities[fpga_id],
                math.floor(
                    proportional * (1.0 + field_tolerance) + 1e-9
                ),
            )

    def dominant_size(cluster: Mapping[str, Any]) -> float:
        resources = cluster["resources"]
        ratios = []
        for fpga_id in fpga_ids:
            capacity = effective_capacity[fpga_id]
            ratios.extend(
                resources.get(field, 0) / capacity[field]
                for field in capacity
                if capacity[field] > 0
            )
        return max(ratios, default=0.0)

    order = sorted(
        clusters,
        key=lambda cluster_id: (
            -sum(adjacency.get(cluster_id, {}).values())
            if hop_limit is not None
            else 0,
            -dominant_size(clusters[cluster_id]),
            -len(clusters[cluster_id]["instances"]),
            _seeded_tie(seed, cluster_id),
            cluster_id,
        ),
    )
    assignment: Dict[str, str] = {}

    def hop_legal(cluster_id: str, fpga_id: str) -> bool:
        if hop_limit is None or hop_distances is None:
            return True
        for neighbor, cluster_is_driver in transport_arcs.get(
            cluster_id, set()
        ):
            if neighbor not in assignment:
                continue
            if cluster_is_driver:
                distance = hop_distances[fpga_id][assignment[neighbor]]
            else:
                distance = hop_distances[assignment[neighbor]][fpga_id]
            if distance is None or distance > hop_limit:
                return False
        return True

    def place(cluster_id: str, fpga_id: str) -> None:
        cluster_resources = ResourceVector.from_mapping(
            clusters[cluster_id]["resources"]
        ).to_dict()
        projected = _resource_add(loads[fpga_id], cluster_resources)
        if not _fits(projected, effective_capacity[fpga_id]):
            raise ValidationError(
                f"cluster {cluster_id!r} does not fit FPGA {fpga_id!r}"
            )
        assignment[cluster_id] = fpga_id
        loads[fpga_id] = projected

    for cluster_id in order:
        fixed_fpga = clusters[cluster_id]["fixed_fpga"]
        if fixed_fpga is not None:
            place(cluster_id, fixed_fpga)

    populated = {fpga_id for fpga_id in assignment.values()}
    for fpga_id in fpga_ids:
        if len(populated) >= constraints["min_used_fpgas"]:
            break
        if fpga_id in populated:
            continue
        candidate = next(
            (
                cluster_id
                for cluster_id in order
                if cluster_id not in assignment
                and hop_legal(cluster_id, fpga_id)
                and _fits(
                    _resource_add(
                        loads[fpga_id],
                        ResourceVector.from_mapping(
                            clusters[cluster_id]["resources"]
                        ).to_dict(),
                    ),
                    effective_capacity[fpga_id],
                )
            ),
            None,
        )
        if candidate is None and hop_limit is not None:
            candidate = next(
                (
                    cluster_id
                    for cluster_id in order
                    if cluster_id not in assignment
                    and _fits(
                        _resource_add(
                            loads[fpga_id],
                            ResourceVector.from_mapping(
                                clusters[cluster_id]["resources"]
                            ).to_dict(),
                        ),
                        effective_capacity[fpga_id],
                    )
                ),
                None,
            )
        if candidate is None:
            raise ValidationError(
                f"cannot populate required FPGA {fpga_id!r} within capacity"
            )
        place(candidate, fpga_id)
        populated.add(fpga_id)

    for cluster_id in order:
        if cluster_id in assignment:
            continue
        resources = ResourceVector.from_mapping(
            clusters[cluster_id]["resources"]
        ).to_dict()
        capacity_candidates = [
            fpga_id
            for fpga_id in fpga_ids
            if _fits(
                _resource_add(loads[fpga_id], resources),
                effective_capacity[fpga_id],
            )
        ]
        if not capacity_candidates:
            raise ValidationError(
                f"cluster {cluster_id!r} cannot fit any FPGA; resources "
                f"{clusters[cluster_id]['resources']}"
            )
        topology_candidates = [
            fpga_id
            for fpga_id in capacity_candidates
            if hop_legal(cluster_id, fpga_id)
        ]
        actual_candidates = topology_candidates or capacity_candidates
        balanced_candidates = [
            fpga_id
            for fpga_id in actual_candidates
            if _fits(
                _resource_add(loads[fpga_id], resources),
                soft_caps[fpga_id],
            )
        ]
        candidates = balanced_candidates or actual_candidates

        def score(fpga_id: str) -> Tuple[float, float, float, str]:
            cut_cost = sum(
                weight
                for neighbor, weight in adjacency.get(cluster_id, {}).items()
                if neighbor in assignment and assignment[neighbor] != fpga_id
            )
            projected = _resource_add(loads[fpga_id], resources)
            ratios = [
                projected[field] / effective_capacity[fpga_id][field]
                for field in effective_capacity[fpga_id]
                if effective_capacity[fpga_id][field] > 0
            ]
            return (
                float(cut_cost),
                max(ratios, default=0.0),
                sum(ratios),
                fpga_id,
            )

        place(cluster_id, min(candidates, key=score))

    return build_partition_assignment(
        ir,
        platform,
        clusters_artifact,
        constraints,
        assignment,
        provider="deterministic-multiresource-greedy-v1",
        seed=seed,
    )


def build_partition_assignment(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    cluster_assignment: Mapping[str, str],
    provider: str,
    seed: int,
    provider_metadata: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the common Phase 3 assignment artifact for any provider."""

    clusters = {
        cluster["id"]: cluster for cluster in clusters_artifact["clusters"]
    }
    if set(cluster_assignment) != set(clusters):
        missing = sorted(set(clusters) - set(cluster_assignment))
        extra = sorted(set(cluster_assignment) - set(clusters))
        raise ValidationError(
            "cluster assignment exact coverage failed; "
            f"missing={missing[:8]}, extra={extra[:8]}"
        )
    fpga_ids = {fpga.id for fpga in platform.fpgas}
    unknown_fpgas = sorted(set(cluster_assignment.values()) - fpga_ids)
    if unknown_fpgas:
        raise ValidationError(
            f"cluster assignment references unknown FPGAs {unknown_fpgas}"
        )

    instance_assignment = {
        instance_id: cluster_assignment[cluster_id]
        for cluster_id, cluster in clusters.items()
        for instance_id in cluster["instances"]
    }
    cut_nets, cut_metrics = compute_cut_nets(ir, instance_assignment)
    semantic_contract = None
    cut_policy = clusters_artifact.get("policy", {})
    if cut_policy.get("cut_mode") == CUT_MODE_STATIC_EXACT:
        from .combinational_cut import build_static_exact_semantic_contract

        semantic_contract = build_static_exact_semantic_contract(
            ir,
            platform.to_dict(),
            instance_assignment,
            cut_nets,
            max_dependency_depth=cut_policy[
                "max_cross_fpga_dependency_depth"
            ],
            comb_segment_budget_slots=cut_policy[
                "comb_segment_budget_slots"
            ],
            frame_slots=cut_policy["frame_slots"],
            candidate_selection_policy=cut_policy.get(
                "candidate_selection_policy",
                STATIC_EXACT_CANDIDATE_FRONTIER_V1,
            ),
        )
        contract_nodes = {
            item["net"]: item for item in semantic_contract["cut_nodes"]
        }
        for cut in cut_nets:
            if cut["cut_class"] != "combinational":
                continue
            node = contract_nodes[cut["net"]]
            cut.update(
                {
                    "dependency_level": node["dependency_level"],
                    "combinational_dependency_depth": node[
                        "combinational_dependency_depth"
                    ],
                    "predecessor_cut_nets": node[
                        "predecessor_cut_nets"
                    ],
                }
            )
        cut_metrics.update(
            {
                "combinational_cut_nets": semantic_contract["metrics"][
                    "combinational_cut_nets"
                ],
                "maximum_combinational_dependency_depth": (
                    semantic_contract["metrics"][
                        "maximum_combinational_dependency_depth"
                    ]
                ),
            }
        )
    partition_records = []
    for fpga in platform.fpgas:
        cluster_ids = sorted(
            cluster_id
            for cluster_id, assigned_fpga in cluster_assignment.items()
            if assigned_fpga == fpga.id
        )
        instance_count = sum(
            len(clusters[cluster_id]["instances"]) for cluster_id in cluster_ids
        )
        resources = ResourceVector.sum(
            ResourceVector.from_mapping(clusters[cluster_id]["resources"])
            for cluster_id in cluster_ids
        ).to_dict(include_zeros=False)
        if not ResourceVector.from_mapping(resources).fits_capacity(
            fpga.effective_capacity
        ):
            raise ValidationError(
                f"provider assignment exceeds effective capacity of {fpga.id!r}"
            )
        utilization = {
            field: resources.get(field, 0) / fpga.effective_capacity[field]
            for field in fpga.effective_capacity
            if fpga.effective_capacity[field] > 0
        }
        partition_records.append(
            {
                "fpga": fpga.id,
                "clusters": cluster_ids,
                "cluster_count": len(cluster_ids),
                "instance_count": instance_count,
                "resources": resources,
                "effective_capacity": dict(
                    sorted(fpga.effective_capacity.items())
                ),
                "utilization": dict(sorted(utilization.items())),
            }
        )

    result: Dict[str, Any] = {
        "schema": PARTITION_ASSIGNMENT_SCHEMA,
        "design": ir.value["design"]["name"],
        "platform": platform.name,
        "provider": provider,
        "seed": seed,
        "constraints": dict(constraints),
        "cluster_assignment": dict(sorted(cluster_assignment.items())),
        "instance_assignment": dict(sorted(instance_assignment.items())),
        "partitions": partition_records,
        "cut_nets": cut_nets,
        "metrics": {
            "instances": len(instance_assignment),
            "clusters": len(clusters),
            "used_fpgas": sum(
                1 for record in partition_records if record["instance_count"]
            ),
            **cut_metrics,
        },
    }
    if provider_metadata is not None:
        result["provider_metadata"] = dict(provider_metadata)
    if semantic_contract is not None:
        result["semantic_contract"] = semantic_contract
    return result


def compute_cut_nets(
    ir: EmuIR,
    instance_assignment: Mapping[str, str],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    cut_nets: List[Dict[str, Any]] = []
    replicated_primary_inputs = 0
    global_nets = 0
    cut_sink_endpoints = 0
    for net in ir.value["nets"]:
        driver_fpgas = sorted(
            {
                instance_assignment[endpoint["instance"]]
                for endpoint in net["drivers"]
                if endpoint["instance"] is not None
            }
        )
        sink_endpoints_by_fpga: Dict[str, int] = defaultdict(int)
        for endpoint in net["sinks"]:
            if endpoint["instance"] is not None:
                sink_endpoints_by_fpga[
                    instance_assignment[endpoint["instance"]]
                ] += 1
        sink_fpgas = sorted(sink_endpoints_by_fpga)
        instance_fpgas = set(driver_fpgas) | set(sink_fpgas)

        if net["cut_class"] in {"clock", "reset"}:
            if len(instance_fpgas) > 1:
                global_nets += 1
            continue
        if not driver_fpgas and net["cut_class"] == "primary_input":
            if len(sink_fpgas) > 1:
                replicated_primary_inputs += 1
            continue

        remote_sink_fpgas = sorted(
            fpga_id for fpga_id in sink_fpgas if fpga_id not in driver_fpgas
        )
        if not remote_sink_fpgas:
            continue
        remote_endpoints = sum(
            sink_endpoints_by_fpga[fpga_id] for fpga_id in remote_sink_fpgas
        )
        cut_sink_endpoints += remote_endpoints
        cut = {
            "net": net["id"],
            "cut_class": net["cut_class"],
            "source_fpgas": driver_fpgas,
            "sink_fpgas": remote_sink_fpgas,
            "sink_endpoints": remote_endpoints,
        }
        if net["cut_class"] == "register_input":
            # Round 0 distributes stable FF Q values. Round 1 starts only
            # after every round-0 arrival plus one fabric settle cycle, so a
            # D/CE source can safely depend on any remote FF without copying
            # an enormous per-net transitive dependency list.
            cut["transport_round"] = 1
        cut_nets.append(cut)

    metrics = {
        "cut_nets": len(cut_nets),
        "cut_sink_endpoints": cut_sink_endpoints,
        "replicated_primary_inputs": replicated_primary_inputs,
        "global_nets": global_nets,
    }
    register_input_cuts = sum(
        cut["cut_class"] == "register_input" for cut in cut_nets
    )
    if register_input_cuts:
        metrics.update(
            {
                "register_input_cut_nets": register_input_cuts,
                "transport_rounds": 2,
                "round_barriers": 1,
            }
        )
    return sorted(cut_nets, key=lambda item: item["net"]), metrics


def validate_cluster_assignment_balance(
    platform: Platform,
    clusters: Sequence[Mapping[str, Any]],
    cluster_assignment: Mapping[str, str],
    requested_tolerance: float,
    requested_tolerance_by_dimension: Optional[Mapping[str, float]] = None,
) -> Dict[str, Any]:
    fpga_ids = [fpga.id for fpga in platform.fpgas]
    dimensions = ["cells"]
    dimensions.extend(
        field
        for field in RESOURCE_FIELDS
        if any(cluster["resources"].get(field, 0) for cluster in clusters)
        and all(
            fpga.effective_capacity.get(field, 0) > 0
            for fpga in platform.fpgas
        )
    )
    weights = {
        cluster["id"]: {
            "cells": len(cluster["instances"]),
            **{
                field: cluster["resources"].get(field, 0)
                for field in dimensions
                if field != "cells"
            },
        }
        for cluster in clusters
    }
    totals = {
        dimension: sum(item[dimension] for item in weights.values())
        for dimension in dimensions
    }
    base_balance: Dict[str, Dict[str, float]] = {}
    for dimension in dimensions:
        if dimension == "cells":
            shares = {
                fpga_id: 1.0 / len(fpga_ids) for fpga_id in fpga_ids
            }
        else:
            capacity_total = sum(
                fpga.effective_capacity[dimension]
                for fpga in platform.fpgas
            )
            shares = {
                fpga.id: (
                    fpga.effective_capacity[dimension] / capacity_total
                )
                for fpga in platform.fpgas
            }
        base_balance[dimension] = shares

    required_ratio = {dimension: 0.0 for dimension in dimensions}
    for cluster in clusters:
        for dimension in dimensions:
            total = totals[dimension]
            if not total:
                continue
            fixed_fpga = cluster.get("fixed_fpga")
            target_share = (
                base_balance[dimension][fixed_fpga]
                if fixed_fpga is not None
                else max(base_balance[dimension].values())
            )
            required_ratio[dimension] = max(
                required_ratio[dimension],
                weights[cluster["id"]][dimension]
                / total
                / target_share
                - 1.0,
            )
    fixed_loads = {
        fpga_id: {dimension: 0 for dimension in dimensions}
        for fpga_id in fpga_ids
    }
    for cluster in clusters:
        fixed_fpga = cluster.get("fixed_fpga")
        if fixed_fpga is None:
            continue
        for dimension in dimensions:
            fixed_loads[fixed_fpga][dimension] += weights[
                cluster["id"]
            ][dimension]
    for fpga_id in fpga_ids:
        for dimension in dimensions:
            total = totals[dimension]
            if total:
                required_ratio[dimension] = max(
                    required_ratio[dimension],
                    fixed_loads[fpga_id][dimension]
                    / total
                    / base_balance[dimension][fpga_id]
                    - 1.0,
                )

    tolerance_overrides = requested_tolerance_by_dimension or {}
    requested_percent_by_dimension = {
        dimension: float(
            tolerance_overrides.get(dimension, requested_tolerance)
        )
        * 100.0
        for dimension in dimensions
    }
    if tolerance_overrides:
        effective_percent_by_dimension = {
            dimension: max(
                requested_percent_by_dimension[dimension],
                max(0.0, required_ratio[dimension] * 100.0) + 0.01,
            )
            for dimension in dimensions
        }
    else:
        # Preserve the v1 contract: without an explicit per-dimension policy,
        # an indivisible cluster in any resource relaxes the shared tolerance
        # for every dimension.
        shared_effective_percent = max(
            requested_tolerance * 100.0,
            max(0.0, max(required_ratio.values(), default=0.0) * 100.0)
            + 0.01,
        )
        effective_percent_by_dimension = {
            dimension: shared_effective_percent for dimension in dimensions
        }
    loads = {
        fpga_id: {dimension: 0 for dimension in dimensions}
        for fpga_id in fpga_ids
    }
    for cluster_id, fpga_id in cluster_assignment.items():
        for dimension in dimensions:
            loads[fpga_id][dimension] += weights[cluster_id][dimension]

    violations = []
    maximum_ratio = 0.0
    allowed_loads = {
        fpga_id: {dimension: 0.0 for dimension in dimensions}
        for fpga_id in fpga_ids
    }
    for fpga_id in fpga_ids:
        for dimension in dimensions:
            total = totals[dimension]
            if not total:
                continue
            actual_share = loads[fpga_id][dimension] / total
            target_share = base_balance[dimension][fpga_id]
            allowed_share = target_share * (
                1.0 + effective_percent_by_dimension[dimension] / 100.0
            )
            allowed_loads[fpga_id][dimension] = total * allowed_share
            ratio = actual_share / allowed_share
            maximum_ratio = max(maximum_ratio, ratio)
            if ratio > 1.0 + 1e-9:
                violations.append(
                    {
                        "fpga": fpga_id,
                        "dimension": dimension,
                        "bound": "upper",
                        "load": loads[fpga_id][dimension],
                        "total": total,
                        "actual_share": actual_share,
                        "allowed_share": allowed_share,
                    }
                )
    if violations:
        raise ValidationError(
            "assignment violates effective multi-resource balance: "
            f"{violations[:8]}"
        )
    return {
        "requested_balance_percent": requested_tolerance * 100.0,
        "effective_balance_percent": max(
            effective_percent_by_dimension.values(), default=0.0
        ),
        "requested_balance_percent_by_dimension": (
            requested_percent_by_dimension
        ),
        "effective_balance_percent_by_dimension": (
            effective_percent_by_dimension
        ),
        "balance_auto_relaxed": any(
            effective_percent_by_dimension[dimension]
            > requested_percent_by_dimension[dimension] + 1e-9
            for dimension in dimensions
        ),
        "balance_dimensions": dimensions,
        "balance_loads": loads,
        "balance_allowed_loads": allowed_loads,
        "max_balance_limit_ratio": maximum_ratio,
        "balance_violations": 0,
    }


def validate_partition_artifacts(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    assignment_artifact: Mapping[str, Any],
) -> Dict[str, Any]:
    if clusters_artifact.get("schema") != CLUSTERS_SCHEMA:
        raise ValidationError(
            f"clusters.schema: expected {CLUSTERS_SCHEMA!r}, "
            f"got {clusters_artifact.get('schema')!r}"
        )
    if assignment_artifact.get("schema") != PARTITION_ASSIGNMENT_SCHEMA:
        raise ValidationError(
            f"assignment.schema: expected {PARTITION_ASSIGNMENT_SCHEMA!r}, "
            f"got {assignment_artifact.get('schema')!r}"
        )

    cut_policy = clusters_artifact.get("policy", {})
    cut_mode = cut_policy.get("cut_mode", CUT_MODE_SEQUENTIAL_ONLY)
    if cut_mode not in {CUT_MODE_SEQUENTIAL_ONLY, CUT_MODE_STATIC_EXACT}:
        raise ValidationError(f"clusters.policy.cut_mode: unknown {cut_mode!r}")
    if cut_mode == CUT_MODE_SEQUENTIAL_ONLY and any(
        key in cut_policy
        for key in (
            "eligible_combinational_cut_nets",
            "semantic_contract",
            "max_cross_fpga_dependency_depth",
        )
    ):
        raise ValidationError(
            "sequential-only cluster policy contains exact-cut fields"
        )

    instance_ids = {instance["id"] for instance in ir.value["instances"]}
    instances = {
        instance["id"]: instance for instance in ir.value["instances"]
    }
    fpga_by_id = {fpga.id: fpga for fpga in platform.fpgas}
    raw_assignment = assignment_artifact.get("instance_assignment")
    if not isinstance(raw_assignment, dict):
        raise ValidationError("assignment.instance_assignment: expected an object")
    assigned_ids = set(raw_assignment)
    missing = sorted(instance_ids - assigned_ids)
    extra = sorted(assigned_ids - instance_ids)
    if missing or extra:
        raise ValidationError(
            "assignment.instance_assignment: exact coverage failed; "
            f"missing={missing[:8]}, extra={extra[:8]}"
        )
    unknown_fpgas = sorted(set(raw_assignment.values()) - set(fpga_by_id))
    if unknown_fpgas:
        raise ValidationError(
            f"assignment.instance_assignment: unknown FPGAs {unknown_fpgas}"
        )

    raw_clusters = clusters_artifact.get("clusters")
    if not isinstance(raw_clusters, list):
        raise ValidationError("clusters.clusters: expected an array")
    cluster_ids: Set[str] = set()
    cluster_members: Set[str] = set()
    for index, cluster in enumerate(raw_clusters):
        if not isinstance(cluster, dict):
            raise ValidationError(f"clusters[{index}]: expected an object")
        cluster_id = cluster.get("id")
        members = cluster.get("instances")
        if not isinstance(cluster_id, str) or not cluster_id:
            raise ValidationError(
                f"clusters[{index}].id: expected a non-empty string"
            )
        if cluster_id in cluster_ids:
            raise ValidationError(f"clusters[{index}].id: duplicate {cluster_id!r}")
        cluster_ids.add(cluster_id)
        if not isinstance(members, list) or not all(
            isinstance(member, str) for member in members
        ):
            raise ValidationError(
                f"clusters[{index}].instances: expected an array of strings"
            )
        duplicate_members = cluster_members & set(members)
        if duplicate_members:
            raise ValidationError(
                f"clusters[{index}].instances: duplicate coverage "
                f"{sorted(duplicate_members)[:8]}"
            )
        cluster_members.update(members)
        assigned_fpgas = {raw_assignment[member] for member in members}
        if len(assigned_fpgas) != 1:
            raise ValidationError(
                f"cluster {cluster_id!r} spans FPGAs {sorted(assigned_fpgas)}"
            )
        expected_resources = _sum_resources(members, instances).to_dict(
            include_zeros=False
        )
        if cluster.get("resources") != expected_resources:
            raise ValidationError(
                f"cluster {cluster_id!r} resource summary does not match EmuIR"
            )
    if cluster_members != instance_ids:
        raise ValidationError(
            "clusters: exact instance coverage failed; "
            f"missing={sorted(instance_ids - cluster_members)[:8]}, "
            f"extra={sorted(cluster_members - instance_ids)[:8]}"
        )

    constraints = normalize_partition_constraints(
        assignment_artifact.get("constraints"),
        ir,
        platform,
    )
    if cut_mode == CUT_MODE_STATIC_EXACT:
        required_policy = {
            "max_cross_fpga_dependency_depth",
            "comb_segment_budget_slots",
            "frame_slots",
            "eligible_combinational_cut_nets",
            "transported_cut_classes",
            "characterization_source_sha256",
            "qualification",
        }
        missing_policy = sorted(required_policy - set(cut_policy))
        if missing_policy:
            raise ValidationError(
                "static exact cluster policy is incomplete: "
                f"{missing_policy}"
            )
        expected_clusters = build_clusters(
            ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=cut_policy[
                "max_cross_fpga_dependency_depth"
            ],
            comb_segment_budget_slots=cut_policy[
                "comb_segment_budget_slots"
            ],
            frame_slots=cut_policy["frame_slots"],
            static_exact_candidate_policy=cut_policy.get(
                "candidate_selection_policy",
                STATIC_EXACT_CANDIDATE_FRONTIER_V1,
            ),
        )
        if clusters_artifact != expected_clusters:
            raise ValidationError(
                "static exact clusters do not match independent reconstruction"
            )
    for group in constraints["groups"]:
        assigned_fpgas = {
            raw_assignment[instance_id] for instance_id in group["instances"]
        }
        if len(assigned_fpgas) != 1:
            raise ValidationError(
                f"group {group['id']!r} spans FPGAs {sorted(assigned_fpgas)}"
            )
    for fixed in constraints["fixed"]:
        actual = raw_assignment[fixed["instance"]]
        if actual != fixed["fpga"]:
            raise ValidationError(
                f"fixed instance {fixed['instance']!r}: expected "
                f"{fixed['fpga']!r}, got {actual!r}"
            )

    primary_resources_by_fpga = {
        fpga_id: ResourceVector.sum(
            ResourceVector.from_mapping(instances[instance_id]["resources"])
            for instance_id, assigned_fpga in raw_assignment.items()
            if assigned_fpga == fpga_id
        )
        for fpga_id in fpga_by_id
    }
    replication_validation = None
    if assignment_artifact.get("replication") is not None:
        if cut_mode == CUT_MODE_STATIC_EXACT:
            raise ValidationError(
                "replication is not qualified with static exact cuts"
            )
        from .replication import validate_replication_artifact

        replication_validation = validate_replication_artifact(
            ir,
            platform,
            clusters_artifact,
            assignment_artifact,
        )
        resources_by_fpga = replication_validation["resources_by_fpga"]
    else:
        resources_by_fpga = primary_resources_by_fpga
    for fpga_id, resources in resources_by_fpga.items():
        if not resources.fits_capacity(fpga_by_id[fpga_id].effective_capacity):
            raise ValidationError(
                f"FPGA {fpga_id!r} exceeds effective capacity: "
                f"{resources.to_dict(include_zeros=False)}"
            )
    used_fpgas = sum(
        1
        for fpga_id in fpga_by_id
        if any(assigned_fpga == fpga_id for assigned_fpga in raw_assignment.values())
    )
    if used_fpgas < constraints["min_used_fpgas"]:
        raise ValidationError(
            f"assignment uses {used_fpgas} FPGAs; "
            f"{constraints['min_used_fpgas']} required"
        )
    raw_cluster_assignment = assignment_artifact.get("cluster_assignment")
    if not isinstance(raw_cluster_assignment, dict):
        raise ValidationError("assignment.cluster_assignment: expected an object")
    if set(raw_cluster_assignment) != cluster_ids:
        raise ValidationError(
            "assignment.cluster_assignment: exact cluster coverage failed"
        )
    expected_cluster_assignment = {
        cluster["id"]: raw_assignment[cluster["instances"][0]]
        for cluster in raw_clusters
    }
    if raw_cluster_assignment != expected_cluster_assignment:
        raise ValidationError(
            "assignment.cluster_assignment does not match instance assignment"
        )
    balance_validation = validate_cluster_assignment_balance(
        platform,
        raw_clusters,
        raw_cluster_assignment,
        constraints["balance_tolerance"],
        constraints.get("balance_tolerance_by_dimension", {}),
    )

    legal_cut_classes = set(LEGAL_CUT_CLASSES)
    eligible_combinational_nets: Set[str] = set()
    if cut_mode == CUT_MODE_STATIC_EXACT:
        transported = transported_cut_classes_for_clusters(clusters_artifact)
        if transported != {*TRANSPORTED_CUT_CLASSES, "combinational"}:
            raise ValidationError(
                "static exact mode requires combinational transport"
            )
        eligible_combinational_nets = set(
            cut_policy["eligible_combinational_cut_nets"]
        )
        legal_cut_classes.add("combinational")
    illegal_cuts: List[str] = []
    for net in ir.value["nets"]:
        fpga_ids = {
            raw_assignment[instance_id]
            for instance_id in _instance_ids_on_net(net)
        }
        if (
            len(fpga_ids) > 1
            and net["cut_class"]
            not in legal_cut_classes | REPLICATED_NET_CLASSES
        ):
            illegal_cuts.append(net["id"])
        if (
            len(fpga_ids) > 1
            and net["cut_class"] == "combinational"
            and net["id"] not in eligible_combinational_nets
        ):
            illegal_cuts.append(net["id"])
    if illegal_cuts:
        raise ValidationError(
            "assignment contains forbidden combinational cuts: "
            f"{illegal_cuts[:8]}"
        )

    if replication_validation is None:
        expected_cuts, expected_metrics = compute_cut_nets(
            ir, raw_assignment
        )
    else:
        expected_cuts = replication_validation["cut_nets"]
        expected_metrics = replication_validation["metrics"]
    expected_contract = None
    if cut_mode == CUT_MODE_STATIC_EXACT:
        from .combinational_cut import build_static_exact_semantic_contract

        expected_contract = build_static_exact_semantic_contract(
            ir,
            platform.to_dict(),
            raw_assignment,
            expected_cuts,
            max_dependency_depth=cut_policy[
                "max_cross_fpga_dependency_depth"
            ],
            comb_segment_budget_slots=cut_policy[
                "comb_segment_budget_slots"
            ],
            frame_slots=cut_policy["frame_slots"],
            candidate_selection_policy=cut_policy.get(
                "candidate_selection_policy",
                STATIC_EXACT_CANDIDATE_FRONTIER_V1,
            ),
        )
        contract_nodes = {
            item["net"]: item for item in expected_contract["cut_nodes"]
        }
        for cut in expected_cuts:
            if cut["cut_class"] != "combinational":
                continue
            node = contract_nodes[cut["net"]]
            cut.update(
                {
                    "dependency_level": node["dependency_level"],
                    "combinational_dependency_depth": node[
                        "combinational_dependency_depth"
                    ],
                    "predecessor_cut_nets": node[
                        "predecessor_cut_nets"
                    ],
                }
            )
        expected_metrics.update(
            {
                "combinational_cut_nets": expected_contract["metrics"][
                    "combinational_cut_nets"
                ],
                "maximum_combinational_dependency_depth": (
                    expected_contract["metrics"][
                        "maximum_combinational_dependency_depth"
                    ]
                ),
            }
        )
        if assignment_artifact.get("semantic_contract") != expected_contract:
            raise ValidationError(
                "assignment.semantic_contract does not match independent "
                "reconstruction"
            )
    elif "semantic_contract" in assignment_artifact:
        raise ValidationError(
            "sequential-only assignment may not contain a semantic contract"
        )
    if assignment_artifact.get("cut_nets") != expected_cuts:
        raise ValidationError("assignment.cut_nets does not match recomputed cuts")
    metrics = assignment_artifact.get("metrics")
    if not isinstance(metrics, dict):
        raise ValidationError("assignment.metrics: expected an object")
    for key, expected in expected_metrics.items():
        if metrics.get(key) != expected:
            raise ValidationError(
                f"assignment.metrics.{key}: expected {expected}, "
                f"got {metrics.get(key)!r}"
            )

    result = {
        "status": "pass",
        "instances": len(instance_ids),
        "clusters": len(raw_clusters),
        "used_fpgas": used_fpgas,
        "illegal_cuts": 0,
        **expected_metrics,
        **balance_validation,
        "resources_by_fpga": {
            fpga_id: resources.to_dict(include_zeros=False)
            for fpga_id, resources in resources_by_fpga.items()
        },
    }
    if expected_contract is not None:
        result.update(
            {
                "cut_mode": CUT_MODE_STATIC_EXACT,
                "qualification": "partition-legality-only-provisional",
                "semantic_contract": {
                    "status": "pass",
                    **expected_contract["metrics"],
                },
            }
        )
    if replication_validation is not None:
        replication_metrics = replication_validation["artifact"]["metrics"]
        for key, expected in replication_metrics.items():
            if metrics.get(key) != expected:
                raise ValidationError(
                    f"assignment.metrics.{key}: expected {expected}, "
                    f"got {metrics.get(key)!r}"
                )
        result["replication"] = {
            "status": "pass",
            **replication_metrics,
        }
        result["primary_resources_by_fpga"] = {
            fpga_id: resources.to_dict(include_zeros=False)
            for fpga_id, resources in primary_resources_by_fpga.items()
        }
    return result
