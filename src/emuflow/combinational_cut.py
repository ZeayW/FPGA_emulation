"""Characterization and semantic contracts for static exact cuts.

The read-only characterization path reconstructs the combinational instance
graph from EmuIR and identifies a conservative potential-cut set.  The opt-in
Phase 3 path additionally uses the same independent graph model to build the
versioned depth-1/depth-2 semantic contract consumed by routing, scheduling,
macro-cycle equivalence, and routed deadline qualification.  The production
``sequential-only`` policy remains unchanged.
"""

from __future__ import annotations

import hashlib
import heapq
import json
from collections import defaultdict, deque
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set, Tuple

from .errors import ValidationError
from .ir import EmuIR
from .resources import RESOURCE_FIELDS


COMBINATIONAL_CUT_CHARACTERIZATION_SCHEMA = (
    "emuflow.combinational-cut-characterization/v1"
)
STATIC_EXACT_COMBINATIONAL_CUT_SCHEMA = (
    "emuflow.static-exact-combinational-cut/v1"
)
GENERALIZED_STATIC_EXACT_COMBINATIONAL_CUT_SCHEMA = (
    "emuflow.static-exact-combinational-cut/v2"
)
STATIC_EXACT_COMBINATIONAL_CUT_SCHEMAS = {
    STATIC_EXACT_COMBINATIONAL_CUT_SCHEMA,
    GENERALIZED_STATIC_EXACT_COMBINATIONAL_CUT_SCHEMA,
}
STATIC_EXACT_CANDIDATE_FRONTIER_V1 = "potential-frontier-depth-v1"
STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2 = "assignment-derived-acyclic-v2"
STATIC_EXACT_DEFAULT_CANDIDATE_POLICY = STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH = 8
STATIC_EXACT_CANDIDATE_POLICIES = {
    STATIC_EXACT_CANDIDATE_FRONTIER_V1,
    STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
}
SEQUENTIAL_TRANSPORTED_CUT_CLASSES = {"register_output", "register_input"}
SEQUENTIAL_LEGAL_CUT_CLASSES = {
    *SEQUENTIAL_TRANSPORTED_CUT_CLASSES,
    "primary_input",
}
REPLICATED_NET_CLASSES = {"clock", "reset", "primary_input"}
SLOT_EDGE_CONVENTION = {
    "id": "fabric-rising-edge-current-slot/v1",
    "tx_sample": (
        "a TX assigned slot S samples its source at the fabric rising edge "
        "for which the pre-edge controller value is S"
    ),
    "rx_capture": (
        "an RX assigned arrival slot A updates its shadow register at the "
        "fabric rising edge for which the pre-edge controller value is A"
    ),
    "settle_budget": (
        "a value captured or launched at edge E with budget B is first "
        "eligible for downstream sampling at edge E+B"
    ),
    "relay_constraint": "next_tx_slot >= arrival_slot + settle_budget_slots",
    "commit": (
        "the virtual DUT commits at the rising edge whose pre-edge slot is "
        "frame_slots-1; capture-ready may equal that commit slot"
    ),
}

_STATE_RESOURCES = {"ff", "bram", "bram18k", "uram288"}
_HARD_COMBINATIONAL_RESOURCES = {"dsp", "carry", "dsp48", "carry8"}
_TRANSPORT_SAFE_SEQUENTIAL_INPUTS = {
    "FDCE": {"D", "CE"},
    "FDPE": {"D", "CE"},
    "FDRE": {"D", "CE", "R"},
    "FDSE": {"D", "CE", "S"},
}


class _UnionFind:
    def __init__(self, items: Iterable[str]):
        self.parent = {item: item for item in items}
        self.rank = {item: 0 for item in items}

    def find(self, item: str) -> str:
        parent = self.parent[item]
        if parent != item:
            self.parent[item] = self.find(parent)
        return self.parent[item]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    # Keep the byte identity of ``json.dumps(..., indent=2, sort_keys=True)``
    # without materializing a second, potentially multi-gigabyte string for a
    # production EmuIR.  JSONEncoder.iterencode preserves the same canonical
    # token stream while bounding the additional hashing memory to one chunk.
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(indent=2, sort_keys=True)
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode("utf-8"))
    digest.update(b"\n")
    return digest.hexdigest()


def semantic_contract_sha256(value: Mapping[str, Any]) -> str:
    """Return the canonical identity propagated across exact-mode stages."""

    return _canonical_sha256(value)


def _instance_class(instance: Mapping[str, Any]) -> str:
    resources = instance["resources"]
    if any(resources.get(field, 0) for field in _STATE_RESOURCES):
        return "architectural-state-or-memory"
    if any(
        resources.get(field, 0) for field in _HARD_COMBINATIONAL_RESOURCES
    ):
        return "hard-combinational-macro"
    if resources.get("lut", 0) > 0 and not any(
        resources.get(field, 0)
        for field in RESOURCE_FIELDS
        if field != "lut"
    ):
        return "supported-soft-combinational"
    return "unsupported-or-opaque"


def _safe_sequential_input(instance_type: str, port: str) -> bool:
    if instance_type.startswith("$_DFF_"):
        return port == "D"
    return port in _TRANSPORT_SAFE_SEQUENTIAL_INPUTS.get(instance_type, set())


def _instance_members(net: Mapping[str, Any]) -> List[str]:
    return sorted(
        {
            endpoint["instance"]
            for field in ("drivers", "sinks")
            for endpoint in net[field]
            if endpoint["instance"] is not None
        }
    )


def _strongly_connected_components(
    nodes: Sequence[str], adjacency: Mapping[str, Set[str]]
) -> List[List[str]]:
    """Return deterministic SCCs without recursion-depth dependence."""

    ordered_adjacency = {
        node: sorted(adjacency.get(node, set())) for node in nodes
    }
    reverse: Dict[str, List[str]] = {node: [] for node in nodes}
    for source, sinks in ordered_adjacency.items():
        for sink in sinks:
            reverse[sink].append(source)
    for node in reverse:
        reverse[node].sort()

    finish_order: List[str] = []
    seen: Set[str] = set()
    for start in sorted(nodes):
        if start in seen:
            continue
        seen.add(start)
        stack: List[Tuple[str, int]] = [(start, 0)]
        while stack:
            node, neighbor_index = stack[-1]
            neighbors = ordered_adjacency[node]
            if neighbor_index < len(neighbors):
                sink = neighbors[neighbor_index]
                stack[-1] = (node, neighbor_index + 1)
                if sink not in seen:
                    seen.add(sink)
                    stack.append((sink, 0))
            else:
                finish_order.append(node)
                stack.pop()

    components: List[List[str]] = []
    assigned: Set[str] = set()
    for start in reversed(finish_order):
        if start in assigned:
            continue
        assigned.add(start)
        component = []
        stack = [start]
        while stack:
            node = stack.pop()
            component.append(node)
            for source in reversed(reverse[node]):
                if source not in assigned:
                    assigned.add(source)
                    stack.append(source)
        components.append(sorted(component))
    return sorted(components, key=lambda item: item[0])


def _atomic_components(
    ir: EmuIR, released_nets: Set[str]
) -> List[List[str]]:
    instance_ids = sorted(item["id"] for item in ir.value["instances"])
    union_find = _UnionFind(instance_ids)
    for net in ir.value["nets"]:
        members = _instance_members(net)
        if len(members) < 2 or net["id"] in released_nets:
            continue
        if (
            net["cut_class"]
            in SEQUENTIAL_LEGAL_CUT_CLASSES | REPLICATED_NET_CLASSES
        ):
            continue
        for member in members[1:]:
            union_find.union(members[0], member)
    grouped: Dict[str, List[str]] = defaultdict(list)
    for instance_id in instance_ids:
        grouped[union_find.find(instance_id)].append(instance_id)
    return sorted(
        (sorted(members) for members in grouped.values()),
        key=lambda members: members[0],
    )


def _component_summary(components: Sequence[Sequence[str]]) -> Dict[str, Any]:
    sizes = sorted((len(component) for component in components), reverse=True)
    return {
        "components": len(components),
        "maximum_instances": max(sizes, default=0),
        "multi_instance_components": sum(size > 1 for size in sizes),
        "instances_in_multi_instance_components": sum(
            size for size in sizes if size > 1
        ),
    }


def _splittable_instances(
    safe_components: Sequence[Sequence[str]],
    candidate_components: Sequence[Sequence[str]],
) -> int:
    candidate_root = {
        member: component[0]
        for component in candidate_components
        for member in component
    }
    return sum(
        len(component)
        for component in safe_components
        if len(component) > 1
        and len({candidate_root[member] for member in component}) > 1
    )


def _build_combinational_cut_candidate_index(
    ir: EmuIR,
    *,
    include_dependency_levels: bool,
    include_source_identity: bool,
) -> Dict[str, Any]:
    """Build the compact candidate index used by Phase 3 hot paths.

    Full characterization also computes per-net audit records, atomic-component
    summaries for every requested depth, and large human-facing metric tables.
    Partition construction and semantic-contract reconstruction need only the
    structurally eligible net IDs (plus legacy frontier levels).  Keeping that
    production query separate avoids repeatedly materializing a full analysis
    report for a real synthesized design while preserving the explicit
    characterization command unchanged.
    """

    instances = {item["id"]: item for item in ir.value["instances"]}
    classes = {
        instance_id: _instance_class(instance)
        for instance_id, instance in instances.items()
    }
    combinational_nodes = sorted(
        instance_id
        for instance_id, classification in classes.items()
        if classification != "architectural-state-or-memory"
    )
    combinational_set = set(combinational_nodes)
    adjacency: Dict[str, Set[str]] = defaultdict(set)
    self_loops: Set[str] = set()
    incoming_by_instance: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for net in ir.value["nets"]:
        if include_dependency_levels:
            for endpoint in net["sinks"]:
                if endpoint["instance"] is not None:
                    incoming_by_instance[endpoint["instance"]].append(net)
        driver_instances = sorted(
            {
                endpoint["instance"]
                for endpoint in net["drivers"]
                if endpoint["instance"] is not None
            }
        )
        sink_instances = sorted(
            {
                endpoint["instance"]
                for endpoint in net["sinks"]
                if endpoint["instance"] is not None
            }
        )
        if len(driver_instances) != 1 or net["cut_class"] in {"clock", "reset"}:
            continue
        source = driver_instances[0]
        if source not in combinational_set:
            continue
        for sink in sink_instances:
            if sink not in combinational_set:
                continue
            adjacency[source].add(sink)
            if source == sink:
                self_loops.add(source)

    components = _strongly_connected_components(combinational_nodes, adjacency)
    cyclic_components = [
        component
        for component in components
        if len(component) > 1 or component[0] in self_loops
    ]
    cyclic_instances = {
        member
        for component in cyclic_components
        for member in component
    }

    eligible_ids: Set[str] = set()
    ineligible: List[Dict[str, Any]] = []
    for net in ir.value["nets"]:
        if net["cut_class"] != "combinational":
            continue
        reasons: Set[str] = set()
        drivers = net["drivers"]
        instance_drivers = [
            endpoint for endpoint in drivers if endpoint["instance"] is not None
        ]
        if len(drivers) != 1 or len(instance_drivers) != 1:
            reasons.add("not-single-instance-driver")
        source = (
            instance_drivers[0]["instance"]
            if len(instance_drivers) == 1
            else None
        )
        if source is not None:
            if classes[source] != "supported-soft-combinational":
                reasons.add("driver-not-supported-soft-logic")
            if source in cyclic_instances:
                reasons.add("driver-in-combinational-cycle")
        if not net["sinks"]:
            reasons.add("no-sinks")
        for endpoint in net["sinks"]:
            sink = endpoint["instance"]
            if sink is None:
                continue
            classification = classes[sink]
            if classification == "supported-soft-combinational":
                if sink in cyclic_instances:
                    reasons.add("sink-in-combinational-cycle")
            elif classification == "architectural-state-or-memory":
                if not _safe_sequential_input(
                    instances[sink]["type"], endpoint["port"]
                ):
                    reasons.add("unsupported-sequential-control-or-memory-sink")
            else:
                reasons.add("sink-not-supported-soft-or-sequential-logic")
        if reasons:
            ineligible.append({"net": net["id"], "reasons": sorted(reasons)})
        else:
            eligible_ids.add(net["id"])

    levels: Dict[str, int] = {}
    dependencies: Dict[str, Set[str]] = {}
    if include_dependency_levels:
        supported_nodes = sorted(
            instance_id
            for instance_id, classification in classes.items()
            if classification == "supported-soft-combinational"
            and instance_id not in cyclic_instances
        )
        supported_set = set(supported_nodes)
        supported_successors: Dict[str, Set[str]] = defaultdict(set)
        supported_indegree = {node: 0 for node in supported_nodes}
        for source in supported_nodes:
            for sink in adjacency.get(source, set()):
                if sink in supported_set and sink not in supported_successors[source]:
                    supported_successors[source].add(sink)
                    supported_indegree[sink] += 1
        supported_queue = deque(
            sorted(
                node for node, degree in supported_indegree.items() if degree == 0
            )
        )
        frontier_by_instance: Dict[str, Set[str]] = {}
        while supported_queue:
            instance_id = supported_queue.popleft()
            frontier: Set[str] = set()
            for incoming in incoming_by_instance.get(instance_id, []):
                if incoming["id"] in eligible_ids:
                    frontier.add(incoming["id"])
                    continue
                for endpoint in incoming["drivers"]:
                    source = endpoint["instance"]
                    if source in supported_set:
                        frontier.update(frontier_by_instance.get(source, set()))
            frontier_by_instance[instance_id] = frontier
            for sink in sorted(supported_successors.get(instance_id, set())):
                supported_indegree[sink] -= 1
                if supported_indegree[sink] == 0:
                    supported_queue.append(sink)
        if len(frontier_by_instance) != len(supported_nodes):
            raise ValidationError(
                "supported soft-logic graph unexpectedly contains a cycle"
            )

        net_by_id = {net["id"]: net for net in ir.value["nets"]}
        for net_id in sorted(eligible_ids):
            driver = next(
                endpoint["instance"]
                for endpoint in net_by_id[net_id]["drivers"]
                if endpoint["instance"] is not None
            )
            dependencies[net_id] = set(frontier_by_instance.get(driver, set()))
        successors: Dict[str, Set[str]] = defaultdict(set)
        indegree = {
            net_id: len(items) for net_id, items in dependencies.items()
        }
        for sink, sources in dependencies.items():
            for source in sources:
                successors[source].add(sink)
        queue = deque(
            sorted(net_id for net_id, value in indegree.items() if value == 0)
        )
        while queue:
            net_id = queue.popleft()
            levels[net_id] = 1 + max(
                (levels[source] for source in dependencies[net_id]), default=0
            )
            for sink in sorted(successors.get(net_id, set())):
                indegree[sink] -= 1
                if indegree[sink] == 0:
                    queue.append(sink)
        if len(levels) != len(eligible_ids):
            raise ValidationError(
                "eligible combinational-cut dependency graph is cyclic"
            )

    result: Dict[str, Any] = {
        "instances": instances,
        "classes": classes,
        "combinational_nodes": combinational_nodes,
        "adjacency": adjacency,
        "self_loops": self_loops,
        "cyclic_components": cyclic_components,
        "cyclic_instances": cyclic_instances,
        "eligible_ids": eligible_ids,
        "ineligible": ineligible,
        "net_by_id": {net["id"]: net for net in ir.value["nets"]},
        "dependencies": dependencies,
        "dependency_levels": levels,
    }
    if include_source_identity:
        result["canonical_emuir_sha256"] = _canonical_sha256(ir.to_dict())
    return result


def characterize_combinational_cuts(
    ir: EmuIR, depth_limits: Sequence[int] = (1, 2)
) -> Dict[str, Any]:
    limits = sorted(set(depth_limits))
    if not limits or any(
        isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
        for limit in limits
    ):
        raise ValidationError("depth limits must be positive integers")

    candidate_index = _build_combinational_cut_candidate_index(
        ir,
        include_dependency_levels=True,
        include_source_identity=False,
    )
    instances = candidate_index["instances"]
    classes = candidate_index["classes"]
    combinational_nodes = candidate_index["combinational_nodes"]
    self_loops = candidate_index["self_loops"]
    cyclic_components = candidate_index["cyclic_components"]
    cyclic_instances = candidate_index["cyclic_instances"]
    eligible_ids = candidate_index["eligible_ids"]
    ineligible = candidate_index["ineligible"]
    net_by_id = candidate_index["net_by_id"]
    dependencies = candidate_index["dependencies"]
    levels = candidate_index["dependency_levels"]

    eligible = []
    for net_id in sorted(eligible_ids):
        net = net_by_id[net_id]
        eligible.append(
            {
                "net": net_id,
                "driver": next(
                    endpoint["instance"]
                    for endpoint in net["drivers"]
                    if endpoint["instance"] is not None
                ),
                "sink_instances": sorted(
                    {
                        endpoint["instance"]
                        for endpoint in net["sinks"]
                        if endpoint["instance"] is not None
                    }
                ),
                "top_output_sinks": sum(
                    endpoint["instance"] is None for endpoint in net["sinks"]
                ),
                "dependency_level": levels[net_id],
                "predecessor_cut_nets": sorted(dependencies[net_id]),
            }
        )

    safe_components = _atomic_components(ir, set())
    limit_records = []
    for limit in limits:
        released = {
            net_id for net_id, level in levels.items() if level <= limit
        }
        candidate_components = _atomic_components(ir, released)
        splittable = _splittable_instances(safe_components, candidate_components)
        limit_records.append(
            {
                "max_dependency_depth": limit,
                "potential_cut_nets": len(released),
                "potential_cut_fraction": (
                    len(released) / len(eligible_ids) if eligible_ids else 0.0
                ),
                "theoretically_splittable_instances": splittable,
                "theoretically_splittable_instance_fraction": (
                    splittable / len(instances) if instances else 0.0
                ),
                "atomic_components": _component_summary(candidate_components),
            }
        )

    depth_histogram: Dict[str, int] = defaultdict(int)
    for level in levels.values():
        depth_histogram[str(level)] += 1
    return {
        "schema": COMBINATIONAL_CUT_CHARACTERIZATION_SCHEMA,
        "status": "characterized",
        "qualification": "analysis-only-no-partition-or-equivalence-claim",
        "design": ir.value["design"]["name"],
        "behavior_change": False,
        "source_identity": {
            "emuir_schema": ir.value["schema"],
            "canonical_emuir_sha256": _canonical_sha256(ir.to_dict()),
        },
        "slot_edge_convention": dict(SLOT_EDGE_CONVENTION),
        "instance_classification": [
            {"instance": instance_id, "classification": classes[instance_id]}
            for instance_id in sorted(instances)
        ],
        "combinational_sccs": [
            {
                "id": f"scc{index:06d}",
                "instances": component,
                "self_loop": len(component) == 1 and component[0] in self_loops,
                "cut_policy": "atomic-uncuttable",
            }
            for index, component in enumerate(cyclic_components)
        ],
        "eligible_cuts": eligible,
        "ineligible_combinational_cuts": sorted(
            ineligible, key=lambda item: item["net"]
        ),
        "dependency_edges": [
            {"from": source, "to": sink}
            for sink in sorted(dependencies)
            for source in sorted(dependencies[sink])
        ],
        "depth_limits": limit_records,
        "current_sequential_only_atomic_components": _component_summary(
            safe_components
        ),
        "metrics": {
            "instances": len(instances),
            "combinational_instances": len(combinational_nodes),
            "supported_soft_combinational_instances": sum(
                value == "supported-soft-combinational"
                for value in classes.values()
            ),
            "cyclic_combinational_sccs": len(cyclic_components),
            "cyclic_combinational_instances": len(cyclic_instances),
            "potential_eligible_cut_nets": len(eligible),
            "ineligible_combinational_cut_nets": len(ineligible),
            "maximum_potential_dependency_depth": max(levels.values(), default=0),
            "potential_cut_depth_histogram": dict(sorted(depth_histogram.items())),
        },
    }


def build_static_exact_semantic_contract(
    ir: EmuIR,
    platform: Mapping[str, Any],
    instance_assignment: Mapping[str, str],
    cut_nets: Sequence[Mapping[str, Any]],
    *,
    max_dependency_depth: int,
    comb_segment_budget_slots: int,
    frame_slots: int,
    candidate_selection_policy: str = STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
) -> Dict[str, Any]:
    """Build the provisional Phase-3 exact-cut semantic contract.

    This establishes structural legality only.  Schedule readiness,
    macro-cycle equivalence, and physical segment deadlines remain downstream
    gates and are intentionally named as pending in the returned contract.
    """

    if candidate_selection_policy not in STATIC_EXACT_CANDIDATE_POLICIES:
        raise ValidationError("unknown exact combinational-cut candidate policy")
    if (
        isinstance(max_dependency_depth, bool)
        or not isinstance(max_dependency_depth, int)
        or max_dependency_depth <= 0
        or (
            candidate_selection_policy == STATIC_EXACT_CANDIDATE_FRONTIER_V1
            and max_dependency_depth not in {1, 2}
        )
    ):
        if candidate_selection_policy == STATIC_EXACT_CANDIDATE_FRONTIER_V1:
            raise ValidationError("legacy exact combinational-cut depth must be 1 or 2")
        raise ValidationError("exact combinational-cut depth must be positive")
    if (
        isinstance(comb_segment_budget_slots, bool)
        or not isinstance(comb_segment_budget_slots, int)
        or comb_segment_budget_slots <= 0
    ):
        raise ValidationError("comb segment budget slots must be positive")
    if (
        isinstance(frame_slots, bool)
        or not isinstance(frame_slots, int)
        or frame_slots < 2
    ):
        raise ValidationError(
            "exact combinational-cut frame slots must be at least two"
        )

    candidate_index = _build_combinational_cut_candidate_index(
        ir,
        include_dependency_levels=False,
        include_source_identity=False,
    )
    eligible = set(candidate_index["eligible_ids"])
    instances = {item["id"]: item for item in ir.value["instances"]}
    classes = {
        instance_id: _instance_class(instance)
        for instance_id, instance in instances.items()
    }
    nets = {item["id"]: item for item in ir.value["nets"]}
    incoming_by_instance: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    outgoing_by_instance: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for net in ir.value["nets"]:
        for endpoint in net["sinks"]:
            if endpoint["instance"] is not None:
                incoming_by_instance[endpoint["instance"]].append(net)
        for endpoint in net["drivers"]:
            if endpoint["instance"] is not None:
                outgoing_by_instance[endpoint["instance"]].append(net)

    cut_by_net = {item["net"]: dict(item) for item in cut_nets}
    if len(cut_by_net) != len(cut_nets):
        raise ValidationError("exact semantic contract cut nets are not unique")
    for net_id, cut in cut_by_net.items():
        net = nets.get(net_id)
        if net is None:
            raise ValidationError(f"exact semantic contract has unknown net {net_id!r}")
        if len(cut.get("source_fpgas", [])) != 1:
            raise ValidationError(f"exact cut {net_id!r} must have one source FPGA")
        if net["cut_class"] == "combinational" and net_id not in eligible:
            raise ValidationError(
                f"combinational cut {net_id!r} is not in the independently "
                "reconstructed eligible set"
            )

    dependencies: Dict[str, Set[str]] = {}
    local_launches: Dict[str, bool] = {}
    for net_id, cut in sorted(cut_by_net.items()):
        source_fpga = cut["source_fpgas"][0]
        driver_instances = sorted(
            {
                endpoint["instance"]
                for endpoint in nets[net_id]["drivers"]
                if endpoint["instance"] is not None
            }
        )
        if len(driver_instances) != 1:
            raise ValidationError(f"exact cut {net_id!r} lacks one logic driver")
        predecessors: Set[str] = set()
        has_local_launch = False
        work = list(driver_instances)
        visited: Set[str] = set()
        while work:
            instance_id = work.pop()
            if instance_id in visited:
                continue
            visited.add(instance_id)
            if instance_assignment.get(instance_id) != source_fpga:
                raise ValidationError(
                    f"exact cut {net_id!r} source cone crosses an unmodelled boundary"
                )
            if classes[instance_id] == "architectural-state-or-memory":
                has_local_launch = True
                continue
            for incoming in incoming_by_instance.get(instance_id, []):
                predecessor = cut_by_net.get(incoming["id"])
                if (
                    predecessor is not None
                    and source_fpga in predecessor.get("sink_fpgas", [])
                ):
                    predecessors.add(incoming["id"])
                    continue
                for endpoint in incoming["drivers"]:
                    upstream = endpoint["instance"]
                    if upstream is None:
                        has_local_launch = True
                    if (
                        upstream is not None
                        and instance_assignment.get(upstream) == source_fpga
                    ):
                        work.append(upstream)
        dependencies[net_id] = predecessors
        local_launches[net_id] = has_local_launch

    successors: Dict[str, Set[str]] = defaultdict(set)
    indegree = {net_id: len(items) for net_id, items in dependencies.items()}
    for sink, sources in dependencies.items():
        for source in sources:
            successors[source].add(sink)
    queue = deque(sorted(net_id for net_id, degree in indegree.items() if degree == 0))
    dependency_level: Dict[str, int] = {}
    combinational_depth: Dict[str, int] = {}
    while queue:
        net_id = queue.popleft()
        dependency_level[net_id] = 1 + max(
            (dependency_level[source] for source in dependencies[net_id]),
            default=0,
        )
        prior_depth = max(
            (combinational_depth[source] for source in dependencies[net_id]),
            default=0,
        )
        combinational_depth[net_id] = prior_depth + (
            1 if nets[net_id]["cut_class"] == "combinational" else 0
        )
        for sink in sorted(successors.get(net_id, set())):
            indegree[sink] -= 1
            if indegree[sink] == 0:
                queue.append(sink)
    if len(dependency_level) != len(cut_by_net):
        raise ValidationError("exact cut dependency graph contains a cycle")
    maximum_depth = max(combinational_depth.values(), default=0)
    if maximum_depth > max_dependency_depth:
        critical = min(
            net_id
            for net_id, depth in combinational_depth.items()
            if depth == maximum_depth
        )
        raise ValidationError(
            "exact combinational-cut dependency depth exceeds the configured "
            f"limit: net {critical!r} has depth {maximum_depth}, limit "
            f"{max_dependency_depth}"
        )

    capture_records: List[Dict[str, Any]] = []
    for net_id, cut in sorted(cut_by_net.items()):
        net = nets[net_id]
        for sink_fpga in sorted(cut["sink_fpgas"]):
            work = [
                endpoint
                for endpoint in net["sinks"]
                if endpoint["instance"] is not None
                and instance_assignment.get(endpoint["instance"]) == sink_fpga
            ]
            for endpoint in net["sinks"]:
                if endpoint["instance"] is None:
                    capture_records.append(
                        {
                            "cut_net": net_id,
                            "fpga": sink_fpga,
                            "kind": "top-output",
                            "endpoint": f"top:{endpoint['port']}[{endpoint['bit']}]",
                        }
                    )
            visited: Set[str] = set()
            while work:
                endpoint = work.pop()
                instance_id = endpoint["instance"]
                if instance_id is None:
                    raise ValidationError("exact capture endpoint is unexpectedly top-level")
                classification = classes[instance_id]
                if classification == "architectural-state-or-memory":
                    capture_records.append(
                        {
                            "cut_net": net_id,
                            "fpga": sink_fpga,
                            "kind": "architectural-state",
                            "endpoint": instance_id,
                            # A memory macro has many independent state
                            # inputs.  Preserve the original reached pin, not
                            # merely its instance, so Phase 7 can qualify the
                            # exact lowered VPR/Vivado endpoint.
                            "port": endpoint["port"],
                            "bit": endpoint["bit"],
                        }
                    )
                    continue
                if instance_id in visited:
                    continue
                visited.add(instance_id)
                for outgoing in outgoing_by_instance.get(instance_id, []):
                    downstream_cut = cut_by_net.get(outgoing["id"])
                    for endpoint in outgoing["sinks"]:
                        sink = endpoint["instance"]
                        # A transported downstream net can still have local
                        # fanout on its source FPGA.  The downstream cut
                        # contract owns only the remote branches; suppressing
                        # the whole net drops real local architectural
                        # captures and leaves Phase 7 unable to bind their
                        # physical RX-to-capture segments.  Stop only at the
                        # endpoints that actually leave ``sink_fpga``.
                        if (
                            downstream_cut is not None
                            and downstream_cut["source_fpgas"][0]
                            == sink_fpga
                            and sink is not None
                            and instance_assignment.get(sink) != sink_fpga
                        ):
                            continue
                        if sink is None:
                            capture_records.append(
                                {
                                    "cut_net": net_id,
                                    "fpga": sink_fpga,
                                    "kind": "top-output",
                                    "endpoint": (
                                        f"top:{endpoint['port']}[{endpoint['bit']}]"
                                    ),
                                }
                            )
                        elif instance_assignment.get(sink) == sink_fpga:
                            work.append(endpoint)

    unique_capture_records = sorted(
        {
            (
                item["cut_net"],
                item["fpga"],
                item["kind"],
                item["endpoint"],
                item.get("port"),
                item.get("bit"),
            )
            for item in capture_records
        }
    )
    capture_records = [
        {
            "id": f"capture{index:06d}",
            "cut_net": cut_net,
            "fpga": fpga,
            "kind": kind,
            "endpoint": endpoint,
            **(
                {"port": port, "bit": bit}
                if kind == "architectural-state"
                else {}
            ),
        }
        for index, (cut_net, fpga, kind, endpoint, port, bit) in enumerate(
            unique_capture_records
        )
    ]

    logic_segments: List[Dict[str, Any]] = []
    source_segment_by_net: Dict[str, str] = {}
    dependency_segment: Dict[Tuple[str, str], str] = {}
    capture_segment: Dict[str, str] = {}
    for net_id in sorted(cut_by_net):
        # A reconvergent source cone can be fed both by predecessor cuts and
        # by a local architectural launch (register/memory/top input).  Those
        # are distinct timing branches whose readiness must all be covered;
        # predecessor presence therefore does not suppress launch-to-TX.
        # A dependency-free cone without an architectural launch is stable
        # from configuration rather than launched by the virtual DUT clock.
        # Preserve it in the causal contract, but identify it explicitly so
        # downstream physical qualification does not demand a fictitious
        # launch endpoint.  It is ready at slot zero and therefore consumes
        # no dynamic settle budget.
        if local_launches[net_id] or not dependencies[net_id]:
            configuration_stable_constant = (
                not local_launches[net_id] and not dependencies[net_id]
            )
            segment_id = f"segment{len(logic_segments):06d}"
            source_segment_by_net[net_id] = segment_id
            logic_segments.append(
                {
                    "id": segment_id,
                    "kind": "launch_to_tx",
                    "fpga": cut_by_net[net_id]["source_fpgas"][0],
                    "sink_cut_net": net_id,
                    # A register output is an architectural launch, not a
                    # value that is already stable at fabric slot zero.  The
                    # virtual-DUT edge occurs at the prior frame commit, so
                    # its routed clock-to-Q, local net, and TX-input delay
                    # must consume the same explicit settle budget as an
                    # ordinary local launch cone.  Giving register outputs a
                    # zero budget can schedule their TX in slot zero and
                    # falsely pass Phase 5 before routed Phase 7 rejects the
                    # physical launch-to-TX segment.
                    "budget_slots": (
                        0
                        if configuration_stable_constant
                        else comb_segment_budget_slots
                    ),
                    "evidence": (
                        "structurally-proven-configuration-stable-constant"
                        if configuration_stable_constant
                        else "contract-budget-provisional"
                    ),
                    **(
                        {
                            "source_semantics": (
                                "configuration-stable-constant"
                            )
                        }
                        if configuration_stable_constant
                        else {}
                    ),
                }
            )
    for sink in sorted(dependencies):
        for source in sorted(dependencies[sink]):
            segment_id = f"segment{len(logic_segments):06d}"
            dependency_segment[(source, sink)] = segment_id
            logic_segments.append(
                {
                    "id": segment_id,
                    "kind": "rx_to_tx",
                    "fpga": cut_by_net[sink]["source_fpgas"][0],
                    "source_cut_net": source,
                    "sink_cut_net": sink,
                    "budget_slots": comb_segment_budget_slots,
                    "evidence": "contract-budget-provisional",
                }
            )
    for capture in capture_records:
        segment_id = f"segment{len(logic_segments):06d}"
        capture_segment[capture["id"]] = segment_id
        logic_segments.append(
            {
                "id": segment_id,
                "kind": "rx_to_capture",
                "fpga": capture["fpga"],
                "source_cut_net": capture["cut_net"],
                "capture_requirement": capture["id"],
                "budget_slots": comb_segment_budget_slots,
                "evidence": "contract-budget-provisional",
            }
        )

    capture_by_cut: Dict[str, List[str]] = defaultdict(list)
    for capture in capture_records:
        capture_by_cut[capture["cut_net"]].append(
            capture_segment[capture["id"]]
        )
    cut_nodes = []
    for net_id, cut in sorted(cut_by_net.items()):
        source_segments = []
        if net_id in source_segment_by_net:
            source_segments.append(source_segment_by_net[net_id])
        source_segments.extend(
            dependency_segment[(source, net_id)]
            for source in sorted(dependencies[net_id])
        )
        cut_nodes.append(
            {
                "net": net_id,
                "cut_class": nets[net_id]["cut_class"],
                "source_fpgas": list(cut["source_fpgas"]),
                "sink_fpgas": list(cut["sink_fpgas"]),
                "dependency_level": dependency_level[net_id],
                "combinational_dependency_depth": combinational_depth[net_id],
                "predecessor_cut_nets": sorted(dependencies[net_id]),
                "source_segment_ids": source_segments,
                "capture_segment_ids": sorted(capture_by_cut.get(net_id, [])),
            }
        )

    uncongested_lower_bound = None
    if candidate_selection_policy == STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2:
        # Reject assignments that cannot possibly fit even before congestion
        # and lane sharing are considered.  The concrete Phase-4 route and
        # Phase-5 list schedule remain authoritative; this is a necessary,
        # independently reconstructible lower bound that lets Phase 3 screen
        # impossible generalized-cut candidates without pretending routing is
        # already known.
        fpga_ids = {
            item.get("id")
            for item in platform.get("fpgas", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        adjacency: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
        for raw_link in platform.get("links", []):
            if not isinstance(raw_link, dict):
                raise ValidationError("exact-cut platform link is invalid")
            endpoints = raw_link.get("endpoints")
            latency = raw_link.get("latency_cycles")
            if (
                not isinstance(endpoints, list)
                or len(endpoints) != 2
                or not all(isinstance(item, str) for item in endpoints)
                or isinstance(latency, bool)
                or not isinstance(latency, int)
                or latency < 0
            ):
                raise ValidationError("exact-cut platform link timing is invalid")
            left, right = endpoints
            # Every additional tree hop needs one forwarding slot after the
            # preceding arrival.  Adding latency+1 per arc and subtracting one
            # at the destination exactly models the uncongested earliest
            # arrival of the concrete list scheduler.
            adjacency[left].append((right, latency + 1))
            if raw_link.get("direction") in {"full_duplex", "half_duplex"}:
                adjacency[right].append((left, latency + 1))

        shortest_cache: Dict[str, Dict[str, int]] = {}

        def earliest_offset(source: str, sink: str) -> int:
            if source == sink:
                return 0
            if source not in shortest_cache:
                distance = {source: 0}
                queue = [(0, source)]
                while queue:
                    current, node = heapq.heappop(queue)
                    if current != distance[node]:
                        continue
                    for neighbor, weight in adjacency.get(node, []):
                        candidate = current + weight
                        if candidate < distance.get(neighbor, 1 << 60):
                            distance[neighbor] = candidate
                            heapq.heappush(queue, (candidate, neighbor))
                shortest_cache[source] = distance
            raw = shortest_cache[source].get(sink)
            if raw is None:
                raise ValidationError(
                    "generalized exact-cut assignment has no board path from "
                    f"{source!r} to {sink!r}"
                )
            return raw - 1

        arrivals: Dict[Tuple[str, str], int] = {}
        lower_bound_nodes = []
        maximum_arrival = 0
        for net_id in sorted(
            cut_by_net,
            key=lambda item: (dependency_level[item], item),
        ):
            source_fpga = cut_by_net[net_id]["source_fpgas"][0]
            if source_fpga not in fpga_ids:
                raise ValidationError("exact-cut source FPGA is absent from platform")
            readiness = []
            if local_launches[net_id] or not dependencies[net_id]:
                readiness.append(
                    0
                    if not local_launches[net_id] and not dependencies[net_id]
                    else comb_segment_budget_slots
                )
            for predecessor in sorted(dependencies[net_id]):
                key = (predecessor, source_fpga)
                if key not in arrivals:
                    raise ValidationError(
                        "generalized exact-cut lower bound lacks predecessor arrival"
                    )
                readiness.append(arrivals[key] + comb_segment_budget_slots)
            if not readiness:
                raise ValidationError(
                    f"generalized exact cut {net_id!r} has no readiness source"
                )
            ready = max(readiness)
            sink_arrivals = {}
            for sink_fpga in sorted(cut_by_net[net_id]["sink_fpgas"]):
                arrival = ready + earliest_offset(source_fpga, sink_fpga)
                arrivals[(net_id, sink_fpga)] = arrival
                sink_arrivals[sink_fpga] = arrival
                maximum_arrival = max(maximum_arrival, arrival)
            lower_bound_nodes.append(
                {
                    "net": net_id,
                    "source_ready_slot": ready,
                    "sink_arrival_slots": sink_arrivals,
                }
            )
        capture_slacks = []
        for capture in capture_records:
            arrival = arrivals.get((capture["cut_net"], capture["fpga"]))
            if arrival is None:
                raise ValidationError(
                    "generalized exact-cut lower bound lacks capture arrival"
                )
            capture_slacks.append(
                frame_slots
                - 1
                - (arrival + comb_segment_budget_slots)
            )
        minimum_slack = min(capture_slacks, default=frame_slots - 1)
        if minimum_slack < 0:
            raise ValidationError(
                "generalized exact-cut assignment is infeasible even under an "
                "uncongested minimum-latency board schedule: minimum capture "
                f"slack is {minimum_slack} slots"
            )
        uncongested_lower_bound = {
            "provider": "board-minimum-latency-dag-lower-bound-v1",
            "qualification": "necessary-not-sufficient-before-routing",
            "maximum_arrival_slot": maximum_arrival,
            "minimum_capture_slack_slots": minimum_slack,
            "cut_readiness": lower_bound_nodes,
        }

    platform_value = dict(platform)
    result = {
        "schema": (
            STATIC_EXACT_COMBINATIONAL_CUT_SCHEMA
            if candidate_selection_policy == STATIC_EXACT_CANDIDATE_FRONTIER_V1
            else GENERALIZED_STATIC_EXACT_COMBINATIONAL_CUT_SCHEMA
        ),
        "mode": "static-exact-combinational",
        "qualification": "partition-legality-only-provisional",
        "max_cross_fpga_dependency_depth": max_dependency_depth,
        "comb_segment_budget_slots": comb_segment_budget_slots,
        "frame_slots": frame_slots,
        "commit_slot": frame_slots - 1,
        "slot_edge_convention": dict(SLOT_EDGE_CONVENTION),
        "cut_nodes": cut_nodes,
        "dependency_edges": [
            {
                "from": source,
                "to": sink,
                "segment": dependency_segment[(source, sink)],
            }
            for sink in sorted(dependencies)
            for source in sorted(dependencies[sink])
        ],
        "logic_segments": logic_segments,
        "capture_requirements": capture_records,
        "metrics": {
            "transported_cut_nets": len(cut_nodes),
            "combinational_cut_nets": sum(
                item["cut_class"] == "combinational" for item in cut_nodes
            ),
            "dependency_edges": sum(len(items) for items in dependencies.values()),
            "maximum_dependency_level": max(dependency_level.values(), default=0),
            "maximum_combinational_dependency_depth": maximum_depth,
            "logic_segments": len(logic_segments),
            "capture_requirements": len(capture_records),
        },
        "source_identity": {
            "canonical_emuir_sha256": _canonical_sha256(ir.to_dict()),
            "canonical_platform_sha256": _canonical_sha256(platform_value),
            "instance_assignment_sha256": _canonical_sha256(
                {"instance_assignment": dict(sorted(instance_assignment.items()))}
            ),
            "timing_source_sha256": None,
        },
        "downstream_gates": {
            "dependency_aware_schedule": "pending",
            "macro_cycle_equivalence": "pending",
            "physical_segment_deadlines": "pending",
            "global_target_and_runtime_timing": "pending",
        },
    }
    if candidate_selection_policy == STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2:
        result["candidate_selection_policy"] = candidate_selection_policy
        result["uncongested_schedule_lower_bound"] = uncongested_lower_bound
    return result


def validate_combinational_cut_characterization(
    ir: EmuIR,
    report: Mapping[str, Any],
) -> Dict[str, Any]:
    if report.get("schema") != COMBINATIONAL_CUT_CHARACTERIZATION_SCHEMA:
        raise ValidationError(
            "characterization.schema: expected "
            f"{COMBINATIONAL_CUT_CHARACTERIZATION_SCHEMA!r}"
        )
    raw_limits = report.get("depth_limits")
    if not isinstance(raw_limits, list):
        raise ValidationError("characterization.depth_limits: expected an array")
    limits = []
    for index, item in enumerate(raw_limits):
        if not isinstance(item, dict):
            raise ValidationError(
                f"characterization.depth_limits[{index}]: expected an object"
            )
        limits.append(item.get("max_dependency_depth"))
    expected = characterize_combinational_cuts(ir, limits)
    if dict(report) != expected:
        raise ValidationError(
            "combinational-cut characterization does not match independent "
            "EmuIR reconstruction"
        )
    return {
        "status": "pass",
        "qualification": expected["qualification"],
        "eligible_cut_nets": expected["metrics"]["potential_eligible_cut_nets"],
        "maximum_potential_dependency_depth": expected["metrics"][
            "maximum_potential_dependency_depth"
        ],
        "cyclic_combinational_sccs": expected["metrics"][
            "cyclic_combinational_sccs"
        ],
    }
