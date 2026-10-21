"""Characterization and structural contracts for combinational cuts.

The read-only characterization path reconstructs the combinational instance
graph from EmuIR and identifies a conservative potential-cut set. Phase 3 uses
the same independent graph model to build the provider-neutral structural
contract consumed by routing, unified TDM assignment, macro-cycle equivalence,
and routed deadline qualification. It does not encode a scheduler decision.
"""

from __future__ import annotations

import hashlib
import json
import mmap
import sqlite3
import struct
import subprocess
import tempfile
from array import array
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Sequence, Set, Tuple

from .errors import ValidationError
from .ir import EmuIR
from .native_tools import resolve_native_executable
from .phase3_exact_records import (
    StaticExactRecordSequence,
    build_static_exact_capture_stream,
    build_static_exact_cut_node_stream,
    build_static_exact_record_streams,
    validate_static_exact_capture_bit,
)
from .resources import RESOURCE_FIELDS


COMBINATIONAL_CUT_CHARACTERIZATION_SCHEMA = (
    "emuflow.combinational-cut-characterization/v1"
)
STATIC_EXACT_STRUCTURAL_CONTRACT_SCHEMA = (
    "emuflow.static-exact-combinational-cut/v3"
)
STATIC_EXACT_COMBINATIONAL_CUT_SCHEMAS = {
    STATIC_EXACT_STRUCTURAL_CONTRACT_SCHEMA,
}
STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2 = "assignment-derived-acyclic-v2"
STATIC_EXACT_DEFAULT_CANDIDATE_POLICY = STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
# Zero disables the policy guard.  Dependency acyclicity remains mandatory;
# schedule feasibility and final timing, rather than an arbitrary graph-depth
# threshold, decide whether a structurally legal cut can be implemented.
STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH = 0
STATIC_EXACT_CANDIDATE_POLICIES = {
    STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
}
STATIC_EXACT_NATIVE_MIN_INSTANCES = 100_000
_STATIC_EXACT_NATIVE_INPUT_MAGIC = 0x31584553464D5545
_STATIC_EXACT_NATIVE_OUTPUT_MAGIC = 0x315845534F4D5545
_STATIC_EXACT_NATIVE_PROTOCOL_VERSION = 3
SEQUENTIAL_TRANSPORTED_CUT_CLASSES = {"register_output", "register_input"}
SEQUENTIAL_LEGAL_CUT_CLASSES = {
    *SEQUENTIAL_TRANSPORTED_CUT_CLASSES,
    "primary_input",
}
REPLICATED_NET_CLASSES = {"clock", "reset", "primary_input"}
SLOT_EDGE_CONVENTION = {
    "id": "tx-sample-before-rx-shadow-update-v1",
    "tx_sample": (
        "a TX assigned slot S samples its source at the fabric rising edge "
        "for which the pre-edge controller value is S"
    ),
    "rx_capture": (
        "an RX assigned arrival slot A updates its shadow register at the "
        "fabric rising edge for which the pre-edge controller value is A; "
        "a TX sampling on that edge observes the pre-update shadow value"
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


def static_exact_analysis_engine(instance_count: int) -> str:
    if (
        isinstance(instance_count, bool)
        or not isinstance(instance_count, int)
        or instance_count < 0
    ):
        raise ValueError("Static Exact instance count is invalid")
    return (
        "native-cpp-v1"
        if instance_count >= STATIC_EXACT_NATIVE_MIN_INSTANCES
        else "python-reference-v1"
    )


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


def _bounded_emuir_identity(ir: EmuIR) -> Dict[str, Any]:
    """Return the provenance needed by the semantic contract, not the graph.

    The contract itself contains every cut, dependency, logic segment, and
    capture fact used downstream.  Re-serializing and hashing the complete
    multi-gigabyte EmuIR here duplicated Phase 1 work and made a source seal a
    Phase 3 hot-path operation.  Design/schema/count identity plus the
    independently validated assignment-index relation is the appropriate
    bounded provenance boundary.
    """

    return {
        "schema": ir.value["schema"],
        "design": ir.value["design"]["name"],
        "instances": len(ir.value["instances"]),
        "nets": len(ir.value["nets"]),
        "qualification": "phase1-index-plus-assignment-relation-v1",
    }


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

    # Phase 3 needs only the eligible net identities.  Returning the complete
    # instance/net indices retained a second decoded copy of a production
    # graph while cluster construction began.  The characterization path asks
    # for dependency levels and still receives the full analysis record.
    if not include_dependency_levels and not include_source_identity:
        return {
            "eligible_ids": eligible_ids,
            "dependency_levels": {},
        }

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


def _analyze_static_exact_dependencies(
    ir: EmuIR,
    instance_assignment: Mapping[str, str],
    cut_nets: Sequence[Mapping[str, Any]],
    *,
    max_dependency_depth: int,
    candidate_selection_policy: str = STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
) -> Dict[str, Any]:
    if candidate_selection_policy not in STATIC_EXACT_CANDIDATE_POLICIES:
        raise ValidationError("unknown exact combinational-cut candidate policy")
    if (
        isinstance(max_dependency_depth, bool)
        or not isinstance(max_dependency_depth, int)
        or max_dependency_depth < 0
    ):
        raise ValidationError(
            "exact combinational-cut depth must be non-negative (zero disables "
            "the policy limit)"
        )
    candidate_index = _build_combinational_cut_candidate_index(
        ir,
        include_dependency_levels=False,
        include_source_identity=False,
    )
    eligible = set(candidate_index["eligible_ids"])
    del candidate_index
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
        if cut.get("cut_class") is None:
            cut["cut_class"] = net["cut_class"]
        elif cut.get("cut_class") != net.get("cut_class"):
            raise ValidationError(
                f"exact cut {net_id!r} class disagrees with EmuIR"
            )
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
    if max_dependency_depth and maximum_depth > max_dependency_depth:
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
    return {
        "classes": classes,
        "nets": nets,
        "incoming_by_instance": incoming_by_instance,
        "outgoing_by_instance": outgoing_by_instance,
        "cut_by_net": cut_by_net,
        "dependencies": dependencies,
        "local_launches": local_launches,
        "dependency_level": dependency_level,
        "combinational_depth": combinational_depth,
        "maximum_depth": maximum_depth,
    }


class _NativeStaticExactAnalysisRecords:
    """Compact integer relation returned by the native dependency analyzer.

    The analyzer already numbers cut nets in canonical order.  Retaining that
    relation as CSR arrays avoids expanding every edge into Python strings,
    sets, and dictionaries merely to build the final typed record stream.
    ``finalize`` independently reconstructs dependency levels and rejects
    cycles before the compact relation is consumed by contract publication.
    """

    def __init__(self, count: int) -> None:
        if count < 0 or count >= 2**32:
            raise ValidationError("native Static Exact cut count is invalid")
        self._count = count
        self._offsets = array("Q", [0])
        self._dependencies = array("I")
        self._local_launches = bytearray()
        self._dependency_levels = array("I")
        self._combinational_depths = array("I")
        self.maximum_dependency_level = 0
        self.maximum_combinational_depth = 0

    def __len__(self) -> int:
        return self._count

    @property
    def dependency_edge_count(self) -> int:
        return len(self._dependencies)

    def append(
        self, local_launch: bool, dependency_indices: Sequence[int]
    ) -> None:
        if len(self._local_launches) >= self._count:
            raise ValidationError("native Static Exact output has excess cuts")
        self._local_launches.append(int(local_launch))
        self._dependencies.extend(dependency_indices)
        self._offsets.append(len(self._dependencies))

    def dependency_count(self, index: int) -> int:
        return self._offsets[index + 1] - self._offsets[index]

    def dependency_indices(self, index: int) -> Iterator[int]:
        start = self._offsets[index]
        end = self._offsets[index + 1]
        for ordinal in range(start, end):
            yield self._dependencies[ordinal]

    def local_launch(self, index: int) -> bool:
        return bool(self._local_launches[index])

    def dependency_level(self, index: int) -> int:
        return self._dependency_levels[index]

    def combinational_depth(self, index: int) -> int:
        return self._combinational_depths[index]

    def finalize(self, combinational: bytearray) -> None:
        if (
            len(self._local_launches) != self._count
            or len(self._offsets) != self._count + 1
            or len(combinational) != self._count
            or any(value not in {0, 1} for value in combinational)
        ):
            raise ValidationError("native Static Exact output is incomplete")

        # Build a compact transient successor CSR to topologically evaluate
        # the predecessor relation.  It is released before capture identity or
        # cut-node records are materialized.  Compared with dict[str, set[str]],
        # this uses fixed-width integers and no per-edge Python objects.
        successor_counts = array("I", [0]) * self._count
        for source in self._dependencies:
            if successor_counts[source] == 0xFFFFFFFF:
                raise ValidationError("native Static Exact dependency is invalid")
            successor_counts[source] += 1
        successor_offsets = array("Q", [0])
        for count in successor_counts:
            successor_offsets.append(successor_offsets[-1] + count)
        cursors = array("Q", successor_offsets[:-1])
        successors = array("I", [0]) * len(self._dependencies)
        for sink in range(self._count):
            for source in self.dependency_indices(sink):
                offset = cursors[source]
                successors[offset] = sink
                cursors[source] += 1
        del cursors
        del successor_counts

        indegree = array(
            "I", (self.dependency_count(index) for index in range(self._count))
        )
        queue = array(
            "I", (index for index, degree in enumerate(indegree) if degree == 0)
        )
        self._dependency_levels = array("I", [0]) * self._count
        self._combinational_depths = array("I", [0]) * self._count
        queue_index = 0
        visited = 0
        while queue_index < len(queue):
            index = queue[queue_index]
            queue_index += 1
            dependency_level = 1
            combinational_depth = 0
            for source in self.dependency_indices(index):
                dependency_level = max(
                    dependency_level, self._dependency_levels[source] + 1
                )
                combinational_depth = max(
                    combinational_depth,
                    self._combinational_depths[source],
                )
            combinational_depth += combinational[index]
            if dependency_level >= 2**32 or combinational_depth >= 2**32:
                raise ValidationError(
                    "native Static Exact dependency depth is out of range"
                )
            self._dependency_levels[index] = dependency_level
            self._combinational_depths[index] = combinational_depth
            self.maximum_dependency_level = max(
                self.maximum_dependency_level, dependency_level
            )
            self.maximum_combinational_depth = max(
                self.maximum_combinational_depth, combinational_depth
            )
            visited += 1
            start = successor_offsets[index]
            end = successor_offsets[index + 1]
            for ordinal in range(start, end):
                sink = successors[ordinal]
                if indegree[sink] == 0:
                    raise ValidationError(
                        "native Static Exact dependency is invalid"
                    )
                indegree[sink] -= 1
                if indegree[sink] == 0:
                    queue.append(sink)
        if visited != self._count:
            raise ValidationError("exact cut dependency graph contains a cycle")


class _NativeStaticExactCaptureRequests:
    """Endpoint-ordered native capture requests in fixed-width columns.

    Protocol v2 orders capture triples by endpoint token, cut ordinal, and
    sink-part ordinal. Keeping those fields in three ``uint32`` arrays avoids
    recreating a Python tuple plus three Python integers for every capture and
    removes the Python-side whole-population sort before endpoint replay.
    """

    def __init__(self) -> None:
        self._endpoint_tokens = array("I")
        self._cut_indices = array("I")
        self._sink_part_indices = array("I")
        self._previous: tuple[int, int, int] | None = None

    def __len__(self) -> int:
        return len(self._endpoint_tokens)

    @property
    def payload_bytes(self) -> int:
        return (
            self._endpoint_tokens.itemsize * len(self._endpoint_tokens)
            + self._cut_indices.itemsize * len(self._cut_indices)
            + self._sink_part_indices.itemsize
            * len(self._sink_part_indices)
        )

    def append(
        self, endpoint_token: int, cut_index: int, sink_part_index: int
    ) -> None:
        current = (endpoint_token, cut_index, sink_part_index)
        if self._previous is not None and current <= self._previous:
            raise ValidationError("native Static Exact capture is invalid")
        self._endpoint_tokens.append(endpoint_token)
        self._cut_indices.append(cut_index)
        self._sink_part_indices.append(sink_part_index)
        self._previous = current

    def __iter__(self) -> Iterator[Tuple[int, int, int]]:
        return zip(
            self._endpoint_tokens,
            self._cut_indices,
            self._sink_part_indices,
        )

    def clear(self) -> None:
        self._endpoint_tokens = array("I")
        self._cut_indices = array("I")
        self._sink_part_indices = array("I")
        self._previous = None


def _resolve_legacy_static_exact_cut_net_ordinals(
    nets: Sequence[Mapping[str, Any]], ordered_cut_ids: Sequence[str]
) -> array:
    """Resolve name-only historical callers without changing their contract."""

    cut_position_by_name = {
        net_id: index for index, net_id in enumerate(ordered_cut_ids)
    }
    resolved = array("I", [0]) * len(ordered_cut_ids)
    matched = bytearray(len(ordered_cut_ids))
    for net_ordinal, net in enumerate(nets):
        cut_index = cut_position_by_name.get(net["id"])
        if cut_index is not None:
            resolved[cut_index] = net_ordinal
            matched[cut_index] = 1
    if 0 in matched:
        unknown = ordered_cut_ids[matched.index(0)]
        raise ValidationError(
            f"exact semantic contract has unknown net {unknown!r}"
        )
    return resolved


def _analyze_static_exact_dependencies_native(
    ir: EmuIR,
    instance_assignment: Mapping[str, str],
    cut_nets: Sequence[Mapping[str, Any]],
    *,
    max_dependency_depth: int,
    candidate_selection_policy: str,
    cut_net_ordinals: Sequence[int] | None = None,
) -> Dict[str, Any]:
    """Run graph-sized exact-cut traversal in the first-party C++ analyzer."""

    if candidate_selection_policy not in STATIC_EXACT_CANDIDATE_POLICIES:
        raise ValidationError("unknown exact combinational-cut candidate policy")
    if (
        isinstance(max_dependency_depth, bool)
        or not isinstance(max_dependency_depth, int)
        or max_dependency_depth < 0
    ):
        raise ValidationError(
            "exact combinational-cut depth must be non-negative (zero disables "
            "the policy limit)"
        )
    candidate_index = _build_combinational_cut_candidate_index(
        ir,
        include_dependency_levels=False,
        include_source_identity=False,
    )
    eligible = set(candidate_index["eligible_ids"])
    del candidate_index
    instances = ir.value["instances"]
    instance_index = {
        instance["id"]: index
        for index, instance in enumerate(instances)
    }
    nets = ir.value["nets"]
    already_ordered = all(
        cut_nets[index - 1]["net"] < cut_nets[index]["net"]
        for index in range(1, len(cut_nets))
    )
    if already_ordered:
        ordered_positions = None
        ordered_cuts: Sequence[Mapping[str, Any]] = cut_nets
    else:
        ordered_positions = sorted(
            range(len(cut_nets)), key=lambda index: cut_nets[index]["net"]
        )
        ordered_cuts = [cut_nets[index] for index in ordered_positions]
    ordered_cut_ids = [cut["net"] for cut in ordered_cuts]
    if any(
        ordered_cut_ids[index - 1] == ordered_cut_ids[index]
        for index in range(1, len(ordered_cut_ids))
    ):
        raise ValidationError("exact semantic contract cut nets are not unique")

    # New Phase 3 construction carries the authoritative net ordinal beside
    # each transient cut record. This avoids three cut-name hash containers
    # (lookup, matched identities, and net coordinates) in native input
    # construction. Name-only callers remain readable through the bounded
    # compatibility lookup below; every coordinate is checked back against
    # authoritative EmuIR before it reaches C++.
    if cut_net_ordinals is not None:
        if len(cut_net_ordinals) != len(cut_nets):
            raise ValidationError("exact semantic contract net ordinals are invalid")
        positions: Iterable[int] = (
            range(len(cut_nets))
            if ordered_positions is None
            else ordered_positions
        )
        reordered_cut_ordinals = (
            None if ordered_positions is None else array("I")
        )
        for position in positions:
            net_ordinal = cut_net_ordinals[position]
            if (
                isinstance(net_ordinal, bool)
                or not isinstance(net_ordinal, int)
                or net_ordinal < 0
                or net_ordinal >= len(nets)
                or nets[net_ordinal]["id"] != cut_nets[position]["net"]
            ):
                raise ValidationError(
                    "exact semantic contract net ordinals are invalid"
                )
            if reordered_cut_ordinals is not None:
                reordered_cut_ordinals.append(net_ordinal)
        resolved_cut_ordinals = (
            cut_net_ordinals
            if reordered_cut_ordinals is None
            else reordered_cut_ordinals
        )
    else:
        resolved_cut_ordinals = _resolve_legacy_static_exact_cut_net_ordinals(
            nets, ordered_cut_ids
        )

    normalized_cuts: List[Mapping[str, Any]] | None = None
    for cut_index, cut in enumerate(ordered_cuts):
        net_id = ordered_cut_ids[cut_index]
        net = nets[resolved_cut_ordinals[cut_index]]
        if len(cut.get("source_fpgas", [])) != 1:
            raise ValidationError(f"exact cut {net_id!r} must have one source FPGA")
        if cut.get("cut_class") is None:
            cut = {**cut, "cut_class": net["cut_class"]}
            if normalized_cuts is None:
                normalized_cuts = list(ordered_cuts)
            normalized_cuts[cut_index] = cut
        elif cut.get("cut_class") != net.get("cut_class"):
            raise ValidationError(
                f"exact cut {net_id!r} class disagrees with EmuIR"
            )
        if net["cut_class"] == "combinational" and net_id not in eligible:
            raise ValidationError(
                f"combinational cut {net_id!r} is not in the independently "
                "reconstructed eligible set"
            )
    if normalized_cuts is not None:
        ordered_cuts = normalized_cuts
    del ordered_positions
    del eligible

    part_ids = sorted(
        set(instance_assignment.values())
        | {
            fpga
            for cut in ordered_cuts
            for fpga in (
                list(cut.get("source_fpgas", []))
                + list(cut.get("sink_fpgas", []))
            )
            if isinstance(fpga, str)
        }
    )
    if not part_ids:
        raise ValidationError("exact semantic contract has no FPGA parts")
    part_index = {part: index for index, part in enumerate(part_ids)}
    if any(
        identity not in instance_assignment
        or instance_assignment[identity] not in part_index
        for identity in instance_index
    ):
        raise ValidationError("exact semantic contract assignment is incomplete")

    resolved = resolve_native_executable("emuflow_static_exact_analyzer")
    with tempfile.TemporaryDirectory(prefix="emuflow-static-exact-") as temporary:
        root = Path(temporary)
        native_input = root / "static-exact.in"
        native_output = root / "static-exact.out"
        with native_input.open("wb") as stream:
            pending = bytearray()

            def emit(format_string: str, *values: Any) -> None:
                pending.extend(struct.pack(format_string, *values))
                if len(pending) >= 4 * 1024 * 1024:
                    stream.write(pending)
                    pending.clear()

            emit(
                "<Q6I",
                _STATIC_EXACT_NATIVE_INPUT_MAGIC,
                _STATIC_EXACT_NATIVE_PROTOCOL_VERSION,
                len(instances),
                len(nets),
                len(ordered_cut_ids),
                len(part_ids),
                sum(len(net["sinks"]) for net in nets),
            )
            for instance in instances:
                emit(
                    "<BI",
                    int(
                        _instance_class(instance)
                        == "architectural-state-or-memory"
                    ),
                    part_index[instance_assignment[instance["id"]]],
                )
            for net in nets:
                emit("<I", len(net["drivers"]))
                for endpoint in net["drivers"]:
                    raw_instance = endpoint["instance"]
                    emit(
                        "<i",
                        -1
                        if raw_instance is None
                        else instance_index[raw_instance],
                    )
                emit("<I", len(net["sinks"]))
                for endpoint in net["sinks"]:
                    raw_instance = endpoint["instance"]
                    sink_instance = (
                        -1
                        if raw_instance is None
                        else instance_index[raw_instance]
                    )
                    emit("<i", sink_instance)
            for cut_index, net_id in enumerate(ordered_cut_ids):
                cut = ordered_cuts[cut_index]
                sources = cut["source_fpgas"]
                sinks = cut.get("sink_fpgas")
                if (
                    sources[0] not in part_index
                    or not isinstance(sinks, list)
                    or any(sink not in part_index for sink in sinks)
                ):
                    raise ValidationError(
                        f"exact cut {net_id!r} has an invalid FPGA endpoint"
                    )
                ordered_sinks = sorted(set(sinks))
                emit(
                    "<III",
                    resolved_cut_ordinals[cut_index],
                    part_index[sources[0]],
                    len(ordered_sinks),
                )
                for sink in ordered_sinks:
                    emit("<I", part_index[sink])
            if pending:
                stream.write(pending)
        # Protocol v3 contains only fixed-width integer graph coordinates. It
        # preserves original instance/net order, resolves the cut subset while
        # scanning those arrays, and returns only capture coordinates. Python
        # binds C returned records back to authoritative EmuIR; native code
        # never receives names, ports, or potentially unbounded Python bits.
        del resolved_cut_ordinals
        completed = subprocess.run(
            [resolved, str(native_input), str(native_output)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ValidationError(
                "native Static Exact analysis failed with exit code "
                f"{completed.returncode}: {detail}"
            )
        with native_output.open("rb") as stream:
            with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
                offset = 0

                def take(format_string: str) -> Tuple[Any, ...]:
                    nonlocal offset
                    size = struct.calcsize(format_string)
                    if offset + size > len(data):
                        raise ValidationError(
                            "native Static Exact output is truncated"
                        )
                    values = struct.unpack_from(format_string, data, offset)
                    offset += size
                    return values

                magic, version, output_cut_count, capture_count = take("<QIIQ")
                if (
                    magic != _STATIC_EXACT_NATIVE_OUTPUT_MAGIC
                    or version != _STATIC_EXACT_NATIVE_PROTOCOL_VERSION
                    or output_cut_count != len(ordered_cut_ids)
                ):
                    raise ValidationError(
                        "native Static Exact output header is invalid"
                    )
                analysis_records = _NativeStaticExactAnalysisRecords(
                    len(ordered_cut_ids)
                )
                for _net_id in ordered_cut_ids:
                    local_launch, dependency_count = take("<BI")
                    if local_launch not in {0, 1}:
                        raise ValidationError(
                            "native Static Exact local-launch flag is invalid"
                        )
                    if dependency_count > min(
                        len(ordered_cut_ids), (len(data) - offset) // 4
                    ):
                        raise ValidationError(
                            "native Static Exact dependency count is invalid"
                        )
                    dependency_indices = []
                    previous_dependency = -1
                    for _ in range(dependency_count):
                        (dependency_index,) = take("<I")
                        if (
                            dependency_index >= len(ordered_cut_ids)
                            or dependency_index <= previous_dependency
                        ):
                            raise ValidationError(
                                "native Static Exact dependency is invalid"
                            )
                        dependency_indices.append(dependency_index)
                        previous_dependency = dependency_index
                    analysis_records.append(
                        bool(local_launch), dependency_indices
                    )

                if capture_count > (len(data) - offset) // 16:
                    raise ValidationError(
                        "native Static Exact capture count is invalid"
                    )

                def capture_coordinates() -> Iterator[Tuple[int, int, int, int]]:
                    for _ in range(capture_count):
                        yield take("<IIII")

                capture_stream = _resolve_native_coordinate_capture_stream(
                    instances,
                    nets,
                    instance_index,
                    capture_coordinates(),
                    ordered_cut_ids,
                    part_ids,
                    ordered_cuts,
                    instance_assignment,
                    root / "capture-relation.sqlite3",
                )
                if offset != len(data):
                    raise ValidationError(
                        "native Static Exact output has trailing bytes"
                    )
        del instance_index

    analysis_records.finalize(
        bytearray(
            cut["cut_class"] == "combinational" for cut in ordered_cuts
        )
    )
    maximum_depth = analysis_records.maximum_combinational_depth
    if max_dependency_depth and maximum_depth > max_dependency_depth:
        critical = min(
            ordered_cut_ids[index]
            for index in range(len(analysis_records))
            if analysis_records.combinational_depth(index) == maximum_depth
        )
        raise ValidationError(
            "exact combinational-cut dependency depth exceeds the configured "
            f"limit: net {critical!r} has depth {maximum_depth}, limit "
            f"{max_dependency_depth}"
        )
    return {
        "ordered_cuts": ordered_cuts,
        "analysis_records": analysis_records,
        "maximum_depth": maximum_depth,
        "capture_stream": capture_stream,
        "analysis_engine": "native-cpp-v1",
    }


def _iter_native_capture_requests(
    ordered_nets: Sequence[Mapping[str, Any]],
    capture_requests: Sequence[Tuple[int, int, int]]
    | _NativeStaticExactCaptureRequests,
    ordered_cut_ids: Sequence[str],
    part_ids: Sequence[str],
) -> Iterator[Tuple[str, str, str, str, Any, Any]]:
    """Resolve only capture endpoints returned by the native analyzer.

    Native endpoint tokens are ordinals in the ordered net/sink stream. The
    endpoint replay already avoids an identity tuple for every sink. Protocol
    v2 also keeps the returned capture subset in endpoint order and fixed-width
    columns, so replay needs neither per-capture Python tuples nor a second
    whole-population sort.
    """

    if not capture_requests:
        return
    if isinstance(capture_requests, _NativeStaticExactCaptureRequests):
        requests = iter(capture_requests)
    else:
        # Compatibility and diagnostic callers may still pass an arbitrary
        # tuple sequence. Production analyzer output is already canonical.
        requests = iter(sorted(capture_requests, key=lambda item: item[0]))
    current = next(requests, None)
    endpoint_token = 0
    for net in ordered_nets:
        for endpoint in net["sinks"]:
            if current is not None and current[0] < endpoint_token:
                raise ValidationError("native Static Exact capture is invalid")
            while current is not None and current[0] == endpoint_token:
                _, cut_index, sink_part_index = current
                raw_instance = endpoint["instance"]
                if raw_instance is None:
                    kind = "top-output"
                    top_bit = validate_static_exact_capture_bit(
                        endpoint["bit"]
                    )
                    identity = f"top:{endpoint['port']}[{top_bit}]"
                    port = None
                    bit = None
                else:
                    kind = "architectural-state"
                    identity = raw_instance
                    port = endpoint["port"]
                    bit = endpoint["bit"]
                yield (
                    ordered_cut_ids[cut_index],
                    part_ids[sink_part_index],
                    kind,
                    identity,
                    port,
                    bit,
                )
                current = next(requests, None)
            endpoint_token += 1
    if current is not None:
        raise ValidationError("native Static Exact capture is invalid")


def _normalize_native_capture_stream(
    capture_relation: Iterable[Tuple[str, str, str, str, Any, Any]],
    sorter_path: Path,
    *,
    release: Any = None,
) -> StaticExactRecordSequence:
    """Canonicalize a capture subset without a Python tuple population."""

    def normalized_rows() -> Iterator[
        Tuple[str, str, str, str, str, int, bytes]
    ]:
        for cut_net, fpga, kind, endpoint, port, bit in capture_relation:
            if (
                not all(
                    isinstance(value, str) and value
                    for value in (cut_net, fpga, kind, endpoint)
                )
                or kind not in {"top-output", "architectural-state"}
                or (
                    kind == "top-output"
                    and (port is not None or bit is not None)
                )
                or (
                    kind == "architectural-state"
                    and (
                        not isinstance(port, str)
                        or not port
                    )
                )
            ):
                raise ValidationError("Static Exact capture relation is invalid")
            if kind == "architectural-state":
                bit = validate_static_exact_capture_bit(bit)
            try:
                encoded_fields = [
                    value.encode("utf-8")
                    for value in (cut_net, fpga, endpoint, port or "")
                ]
            except UnicodeEncodeError as error:
                raise ValidationError(
                    "Static Exact capture identity is invalid"
                ) from error
            if any(len(value) >= 2**32 for value in encoded_fields):
                raise ValidationError("Static Exact capture identity is too large")
            bit_key = (
                b""
                if bit is None
                else bit.to_bytes(max(1, (bit.bit_length() + 7) // 8), "big")
            )
            yield (
                cut_net,
                fpga,
                kind,
                endpoint,
                "" if port is None else port,
                -1 if bit is None else len(bit_key),
                bit_key,
            )

    connection = sqlite3.connect(sorter_path)
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute(
            """
            CREATE TABLE capture_relation (
                cut_net TEXT NOT NULL,
                fpga TEXT NOT NULL,
                kind TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                port_key TEXT NOT NULL,
                bit_length INTEGER NOT NULL,
                bit_key BLOB NOT NULL,
                PRIMARY KEY (
                    cut_net, fpga, kind, endpoint, port_key,
                    bit_length, bit_key
                )
            ) WITHOUT ROWID
            """
        )
        connection.executemany(
            """
            INSERT OR IGNORE INTO capture_relation (
                cut_net, fpga, kind, endpoint, port_key,
                bit_length, bit_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            normalized_rows(),
        )
        if release is not None:
            release()

        def ordered_relation() -> Iterator[Tuple[str, str, str, str, Any, Any]]:
            cursor = connection.execute(
                """
                SELECT cut_net, fpga, kind, endpoint, port_key, bit_key
                FROM capture_relation
                ORDER BY cut_net, fpga, kind, endpoint, port_key,
                         bit_length, bit_key
                """
            )
            for cut_net, fpga, kind, endpoint, port_key, bit_key in cursor:
                yield (
                    cut_net,
                    fpga,
                    kind,
                    endpoint,
                    None if kind == "top-output" else port_key,
                    (
                        None
                        if kind == "top-output"
                        else int.from_bytes(bit_key, "big")
                    ),
                )

        return build_static_exact_capture_stream(ordered_relation())
    finally:
        connection.close()


def _resolve_native_capture_stream(
    ordered_nets: Sequence[Mapping[str, Any]],
    capture_requests: List[Tuple[int, int, int]]
    | _NativeStaticExactCaptureRequests,
    ordered_cut_ids: Sequence[str],
    part_ids: Sequence[str],
    sorter_path: Path,
) -> StaticExactRecordSequence:
    """Resolve the legacy v2 token stream for focused compatibility tests."""

    return _normalize_native_capture_stream(
        _iter_native_capture_requests(
            ordered_nets, capture_requests, ordered_cut_ids, part_ids
        ),
        sorter_path,
        release=capture_requests.clear,
    )


def _resolve_native_coordinate_capture_stream(
    instances: Sequence[Mapping[str, Any]],
    nets: Sequence[Mapping[str, Any]],
    instance_index: Mapping[str, int],
    capture_coordinates: Iterable[Tuple[int, int, int, int]],
    ordered_cut_ids: Sequence[str],
    part_ids: Sequence[str],
    ordered_cuts: Sequence[Mapping[str, Any]],
    instance_assignment: Mapping[str, str],
    sorter_path: Path,
) -> StaticExactRecordSequence:
    """Bind native v3 coordinates back to authoritative EmuIR records."""

    def capture_relation() -> Iterator[
        Tuple[str, str, str, str, Any, Any]
    ]:
        previous: Tuple[int, int, int, int] | None = None
        for cut_index, part_index, net_index, sink_index in capture_coordinates:
            canonical = (net_index, sink_index, cut_index, part_index)
            if previous is not None and canonical <= previous:
                raise ValidationError("native Static Exact capture is invalid")
            previous = canonical
            if (
                cut_index >= len(ordered_cut_ids)
                or part_index >= len(part_ids)
                or net_index >= len(nets)
                or sink_index >= len(nets[net_index]["sinks"])
            ):
                raise ValidationError("native Static Exact capture is invalid")
            cut_net = ordered_cut_ids[cut_index]
            fpga = part_ids[part_index]
            if fpga not in ordered_cuts[cut_index].get("sink_fpgas", []):
                raise ValidationError("native Static Exact capture is invalid")
            endpoint = nets[net_index]["sinks"][sink_index]
            raw_instance = endpoint["instance"]
            if raw_instance is None:
                kind = "top-output"
                top_bit = validate_static_exact_capture_bit(endpoint["bit"])
                identity = f"top:{endpoint['port']}[{top_bit}]"
                port = None
                bit = None
            else:
                source_index = instance_index.get(raw_instance)
                if (
                    source_index is None
                    or source_index >= len(instances)
                    or instances[source_index]["id"] != raw_instance
                    or _instance_class(instances[source_index])
                    != "architectural-state-or-memory"
                    or instance_assignment.get(raw_instance)
                    != fpga
                ):
                    raise ValidationError("native Static Exact capture is invalid")
                kind = "architectural-state"
                identity = raw_instance
                port = endpoint["port"]
                bit = endpoint["bit"]
            yield (
                cut_net,
                fpga,
                kind,
                identity,
                port,
                bit,
            )

    return _normalize_native_capture_stream(capture_relation(), sorter_path)


def _resolve_native_capture_requests(
    ordered_nets: Sequence[Mapping[str, Any]],
    capture_requests: Sequence[Tuple[int, int, int]],
    ordered_cut_ids: Sequence[str],
    part_ids: Sequence[str],
) -> List[Tuple[str, str, str, str, Any, Any]]:
    """Materialized compatibility helper for focused diagnostic tests."""

    return list(
        _iter_native_capture_requests(
            ordered_nets,
            list(capture_requests),
            ordered_cut_ids,
            part_ids,
        )
    )


def build_static_exact_semantic_contract(
    ir: EmuIR,
    platform: Mapping[str, Any],
    instance_assignment: Mapping[str, str],
    cut_nets: Sequence[Mapping[str, Any]],
    *,
    max_dependency_depth: int,
    candidate_selection_policy: str = STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    stream_records: bool = False,
    cut_net_ordinals: Sequence[int] | None = None,
) -> Dict[str, Any]:
    """Build the provisional Phase-3 exact-cut semantic contract.

    This establishes structural legality only.  Schedule readiness,
    macro-cycle equivalence, and physical segment deadlines remain downstream
    gates and are intentionally named as pending in the returned contract.
    """

    use_native_analyzer = (
        static_exact_analysis_engine(len(ir.value["instances"]))
        == "native-cpp-v1"
    )
    analyzer = (
        _analyze_static_exact_dependencies_native
        if use_native_analyzer
        else _analyze_static_exact_dependencies
    )
    analysis = analyzer(
        ir,
        instance_assignment,
        cut_nets,
        max_dependency_depth=max_dependency_depth,
        candidate_selection_policy=candidate_selection_policy,
        **(
            {"cut_net_ordinals": cut_net_ordinals}
            if use_native_analyzer
            else {}
        ),
    )
    classes = analysis.get("classes")
    nets = analysis.get("nets")
    outgoing_by_instance = analysis.get("outgoing_by_instance")
    cut_by_net = analysis.get("cut_by_net")
    ordered_cuts = analysis.get("ordered_cuts")
    analysis_records = analysis.get("analysis_records")
    dependencies = analysis.get("dependencies")
    local_launches = analysis.get("local_launches")
    dependency_level = analysis.get("dependency_level")
    combinational_depth = analysis.get("combinational_depth")
    maximum_depth = analysis["maximum_depth"]
    captures_are_normalized = analysis.get("analysis_engine") == "native-cpp-v1"
    if captures_are_normalized:
        if (
            not isinstance(analysis_records, _NativeStaticExactAnalysisRecords)
            or not isinstance(ordered_cuts, Sequence)
        ):
            raise ValidationError("native Static Exact analysis is incomplete")
    elif not all(
        isinstance(value, Mapping)
        for value in (
            dependencies,
            local_launches,
            dependency_level,
            combinational_depth,
        )
    ):
        raise ValidationError("Static Exact reference analysis is incomplete")

    capture_records: List[Dict[str, Any]] = analysis.get("capture_records", [])
    capture_stream = analysis.get("capture_stream")
    del analysis
    if outgoing_by_instance is not None:
        if (
            not isinstance(classes, Mapping)
            or not isinstance(nets, Mapping)
            or not isinstance(cut_by_net, Mapping)
        ):
            raise ValidationError("Static Exact reference analysis is incomplete")
        for net_id, cut in sorted(cut_by_net.items()):
            net = nets[net_id]
            for sink_fpga in sorted(cut["sink_fpgas"]):
                work = [
                    endpoint
                    for endpoint in net["sinks"]
                    if endpoint["instance"] is not None
                    and instance_assignment.get(endpoint["instance"])
                    == sink_fpga
                ]
                for endpoint in net["sinks"]:
                    if endpoint["instance"] is None:
                        top_bit = validate_static_exact_capture_bit(
                            endpoint["bit"]
                        )
                        capture_records.append(
                            {
                                "cut_net": net_id,
                                "fpga": sink_fpga,
                                "kind": "top-output",
                                "endpoint": (
                                    f"top:{endpoint['port']}[{top_bit}]"
                                ),
                            }
                        )
                visited: Set[str] = set()
                while work:
                    endpoint = work.pop()
                    instance_id = endpoint["instance"]
                    if instance_id is None:
                        raise ValidationError(
                            "exact capture endpoint is unexpectedly top-level"
                        )
                    classification = classes[instance_id]
                    if classification == "architectural-state-or-memory":
                        capture_bit = validate_static_exact_capture_bit(
                            endpoint["bit"]
                        )
                        capture_records.append(
                            {
                                "cut_net": net_id,
                                "fpga": sink_fpga,
                                "kind": "architectural-state",
                                "endpoint": instance_id,
                                "port": endpoint["port"],
                                "bit": capture_bit,
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
                            if (
                                downstream_cut is not None
                                and downstream_cut["source_fpgas"][0]
                                == sink_fpga
                                and sink is not None
                                and instance_assignment.get(sink) != sink_fpga
                            ):
                                continue
                            if sink is None:
                                top_bit = validate_static_exact_capture_bit(
                                    endpoint["bit"]
                                )
                                capture_records.append(
                                    {
                                        "cut_net": net_id,
                                        "fpga": sink_fpga,
                                        "kind": "top-output",
                                        "endpoint": (
                                            f"top:{endpoint['port']}"
                                            f"[{top_bit}]"
                                        ),
                                    }
                                )
                            elif instance_assignment.get(sink) == sink_fpga:
                                work.append(endpoint)
    del classes
    del nets
    del outgoing_by_instance

    if captures_are_normalized:
        if not isinstance(capture_stream, StaticExactRecordSequence):
            raise ValidationError("native Static Exact captures are incomplete")
        if stream_records:
            capture_records = capture_stream
        else:
            capture_records = list(capture_stream)
            del capture_stream
    else:
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
        if stream_records:
            capture_records = build_static_exact_capture_stream(
                unique_capture_records
            )
        else:
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
                for index, (
                    cut_net,
                    fpga,
                    kind,
                    endpoint,
                    port,
                    bit,
                ) in enumerate(unique_capture_records)
            ]
        del unique_capture_records

    if ordered_cuts is None:
        if not isinstance(cut_by_net, Mapping):
            raise ValidationError("Static Exact reference analysis is incomplete")
        ordered_cut_ids = sorted(cut_by_net)
        ordered_cuts = [cut_by_net.pop(net_id) for net_id in ordered_cut_ids]
    else:
        ordered_cut_ids = [cut["net"] for cut in ordered_cuts]
    del cut_by_net
    if analysis_records is not None:
        if len(analysis_records) != len(ordered_cut_ids):
            raise ValidationError("native Static Exact analysis is incomplete")
        dependency_edge_count = analysis_records.dependency_edge_count
        maximum_dependency_level = analysis_records.maximum_dependency_level
    else:
        dependency_edge_count = sum(
            len(items) for items in dependencies.values()
        )
        maximum_dependency_level = max(dependency_level.values(), default=0)
        # Each predecessor set is already final after the analyzer's cycle and
        # depth checks.  Convert it in place to the ordered list owned by the
        # corresponding cut-node record instead of retaining a set while a
        # second graph-sized list population is built.
        for net_id in ordered_cut_ids:
            dependencies[net_id] = sorted(dependencies[net_id])

    launch_flags = bytearray()
    launch_segment_count = 0
    logic_segments: List[Dict[str, Any]] = []
    for cut_index, net_id in enumerate(ordered_cut_ids):
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
        if analysis_records is None:
            local_launch = local_launches[net_id]
            predecessor_count = len(dependencies[net_id])
        else:
            local_launch = analysis_records.local_launch(cut_index)
            predecessor_count = analysis_records.dependency_count(cut_index)
        has_launch = local_launch or predecessor_count == 0
        launch_flag = 1 if local_launch else (2 if has_launch else 0)
        launch_flags.append(launch_flag)
        if launch_flag:
            segment_id = f"segment{launch_segment_count:06d}"
            launch_segment_count += 1
            if not stream_records:
                logic_segments.append(
                    {
                        "id": segment_id,
                        "kind": "launch_to_tx",
                        "fpga": ordered_cuts[cut_index]["source_fpgas"][0],
                        "sink_cut_net": net_id,
                        **(
                            {
                                "source_semantics": (
                                    "configuration-stable-constant"
                                )
                            }
                            if launch_flag == 2
                            else {}
                        ),
                    }
                )
    capture_segment_start = launch_segment_count + dependency_edge_count
    dependency_edges: List[Dict[str, Any]] = []
    def generate_cut_nodes() -> Iterator[Dict[str, Any]]:
        launch_segment_number = 0
        dependency_segment_number = launch_segment_count
        capture_index = 0
        capture_iterator = iter(capture_records)
        next_capture = next(capture_iterator, None)
        for cut_index, sink in enumerate(ordered_cut_ids):
            cut = ordered_cuts[cut_index]
            if analysis_records is None:
                predecessor_cut_nets = dependencies.pop(sink)
                local_launch = local_launches.pop(sink)
                cut_dependency_level = dependency_level.pop(sink)
                cut_combinational_depth = combinational_depth.pop(sink)
            else:
                predecessor_cut_nets = [
                    ordered_cut_ids[source]
                    for source in analysis_records.dependency_indices(cut_index)
                ]
                local_launch = analysis_records.local_launch(cut_index)
                cut_dependency_level = analysis_records.dependency_level(
                    cut_index
                )
                cut_combinational_depth = analysis_records.combinational_depth(
                    cut_index
                )
            source_segments = []
            if local_launch or not predecessor_cut_nets:
                source_segments.append(
                    f"segment{launch_segment_number:06d}"
                )
                launch_segment_number += 1
            for source in predecessor_cut_nets:
                segment_id = f"segment{dependency_segment_number:06d}"
                if not stream_records:
                    logic_segments.append(
                        {
                            "id": segment_id,
                            "kind": "rx_to_tx",
                            "fpga": cut["source_fpgas"][0],
                            "source_cut_net": source,
                            "sink_cut_net": sink,
                        }
                    )
                    dependency_edges.append(
                        {"from": source, "to": sink, "segment": segment_id}
                    )
                source_segments.append(
                    f"segment{dependency_segment_number:06d}"
                )
                dependency_segment_number += 1
            capture_segments = []
            while (
                next_capture is not None
                and next_capture["cut_net"] == sink
            ):
                capture_segments.append(
                    f"segment{capture_segment_start + capture_index:06d}"
                )
                capture_index += 1
                next_capture = next(capture_iterator, None)
            # Preserve the v3 canonical string ordering even after segment IDs
            # grow beyond the six-digit minimum width.
            capture_segments.sort()
            yield {
                "net": sink,
                "cut_class": cut["cut_class"],
                "source_fpgas": list(cut["source_fpgas"]),
                "sink_fpgas": list(cut["sink_fpgas"]),
                "dependency_level": cut_dependency_level,
                "combinational_dependency_depth": cut_combinational_depth,
                "predecessor_cut_nets": predecessor_cut_nets,
                "source_segment_ids": source_segments,
                "capture_segment_ids": capture_segments,
            }
        if next_capture is not None or capture_index != len(capture_records):
            raise ValidationError("Static Exact capture ordering is inconsistent")
        if (
            launch_segment_number != launch_segment_count
            or dependency_segment_number != capture_segment_start
        ):
            raise ValidationError("Static Exact segment numbering is inconsistent")
        if not stream_records and len(logic_segments) != capture_segment_start:
            raise ValidationError(
                "Static Exact dependency numbering is inconsistent"
            )

    if stream_records:
        cut_nodes = build_static_exact_cut_node_stream(generate_cut_nodes())
        record_streams = build_static_exact_record_streams(
            cut_nodes, capture_records, launch_flags
        )
        dependency_edges = record_streams["dependency_edges"]
        logic_segments = record_streams["logic_segments"]
    else:
        cut_nodes = list(generate_cut_nodes())
        for capture in capture_records:
            segment_id = f"segment{len(logic_segments):06d}"
            logic_segments.append(
                {
                    "id": segment_id,
                    "kind": "rx_to_capture",
                    "fpga": capture["fpga"],
                    "source_cut_net": capture["cut_net"],
                    "capture_requirement": capture["id"],
                }
            )
    if (
        len(cut_nodes) != len(ordered_cut_ids)
        or len(logic_segments) != capture_segment_start + len(capture_records)
        or len(dependency_edges) != dependency_edge_count
    ):
        raise ValidationError("Static Exact segment numbering is inconsistent")

    platform_value = dict(platform)
    result = {
        "schema": STATIC_EXACT_STRUCTURAL_CONTRACT_SCHEMA,
        "mode": "static-exact-combinational",
        "qualification": "structural-partition-legality",
        "max_cross_fpga_dependency_depth": max_dependency_depth,
        "candidate_selection_policy": candidate_selection_policy,
        "cut_nodes": cut_nodes,
        "dependency_edges": dependency_edges,
        "logic_segments": logic_segments,
        "capture_requirements": capture_records,
        "metrics": {
            "transported_cut_nets": len(cut_nodes),
            "combinational_cut_nets": sum(
                item["cut_class"] == "combinational" for item in cut_nodes
            ),
            "dependency_edges": dependency_edge_count,
            "maximum_dependency_level": maximum_dependency_level,
            "maximum_combinational_dependency_depth": maximum_depth,
            "logic_segments": len(logic_segments),
            "capture_requirements": len(capture_records),
        },
        "source_identity": {
            "emuir": _bounded_emuir_identity(ir),
            "canonical_platform_sha256": _canonical_sha256(platform_value),
            "instance_assignment_sha256": _canonical_sha256(
                {"instance_assignment": instance_assignment}
            ),
            "timing_source_sha256": None,
        },
    }
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
