"""Normal TritonPart partitioning for the fixed asynchronous ULX3S pair.

The provider consumes a resource-only view, NOT a fixed-latency BoardDB.
No UART delay, slot schedule or pre-partition dependency guard is invented.
"""
from dataclasses import dataclass
from pathlib import Path

from .board_ulx3s import PART, ulx3s_pair_profile
from .ecp5_qualification import ECP5_85F_CAPACITY
from .equivalence import _lut_definition
from .errors import ValidationError
from .partition import (build_clusters, normalize_partition_constraints,
                        validate_partition_artifacts_online)
from .platform import FpgaNode
from .resources import ResourceVector
from .snapshot_pair import bind_snapshot_reset_inputs
from .tritonpart import run_tritonpart


@dataclass(frozen=True)
class SnapshotPartitionResources:
    """Only the Phase 3 consumer interface; deliberately no timing/BoardDB API."""
    name: str
    fpgas: tuple


def ulx3s_partition_resources():
    profile = ulx3s_pair_profile()
    return SnapshotPartitionResources(profile["id"], tuple(
        FpgaNode(b["id"], PART, b["utilization_limit"],
                 {"lut": ECP5_85F_CAPACITY["TRELLIS_COMB"],
                  "ff": ECP5_85F_CAPACITY["TRELLIS_FF"]})
        for b in profile["boards"]))


def partition_snapshot_pair(ir, *, output_dir: Path, executable: str,
                            seed: int = 1, reset_data_ports=()):
    """Solve the generalized graph with native TritonPart, then validate it.

    Both fixed boards are enabled. Balance, capacity and structural clustering
    apply; no instance placement is forced. The final physical gate must count
    DUT plus transport against the SAME 75% limit, not just this DUT estimate.
    """
    if type(seed) is not int or seed < 0:
        raise ValidationError("snapshot partition seed must be a nonnegative integer")
    ir = bind_snapshot_reset_inputs(ir, reset_data_ports)
    for cell in ir.value["instances"]:
        kind = cell["type"]
        count = ResourceVector.from_mapping(cell.get("resources", {})).to_dict(include_zeros=False)
        if kind.startswith("LUT") or kind in {"$lut", "$_LUT_"}:
            width, _, _, _ = _lut_definition(cell)
            if not 1 <= width <= 4 or count != {"lut": 1}:
                raise ValidationError("ULX3S partition input requires one LUT4-equivalent resource per LUT")
        elif kind == "$_DFF_P_":
            if count != {"ff": 1}:
                raise ValidationError("ULX3S partition FF resource accounting is inconsistent")
        else:
            raise ValidationError(f"unsupported ULX3S partition primitive: {kind}")
    resources = ulx3s_partition_resources()
    constraints = normalize_partition_constraints(None, ir, resources)
    clusters = build_clusters(ir, constraints, cut_mode="static-exact-combinational")
    result = run_tritonpart(ir, resources, clusters, constraints, Path(output_dir),
                           seed, executable=executable, timeout_seconds=300,
                           persist_input_manifest=False, defer_semantic_contract=True)
    validation = validate_partition_artifacts_online(resources, clusters, result)
    # The fixed pair has a directly wired channel in both directions. Every
    # cross-board assignment is reachable in one hop, independently of latency.
    if set(result["instance_assignment"].values()) != {"board0", "board1"}:
        raise ValidationError("fixed two-board partition must use both boards")
    result["snapshot_platform_scope"] = {
        "kind": "resource-and-reachability-only", "maximum_route_hops": 1,
        "fixed_latency_model": False, "combined_physical_capacity_qualified": False,
    }
    return result, validation
