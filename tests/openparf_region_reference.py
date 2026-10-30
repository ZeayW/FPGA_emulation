"""Independent reference for the maintained full-device region policy."""
from collections import Counter
import math
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple, Dict
from emuflow_tools.platform.architecture import ArchitectureDB
from emuflow.common.errors import ValidationError
from emuflow_tools.document_io import read_json
_ACTIVE_REGION_RESOURCE_HEADROOM=1.5

def _cluster_resource(cluster: Mapping[str, Any]) -> str:
    kind = cluster.get("kind")
    if kind in {"slice", "carry"}:
        return "slice"
    types = {item.get("cell_type") for item in cluster.get("assignments", [])}
    if types == {"DSP48E2"}:
        return "dsp"
    if types and all(str(cell_type).startswith("RAMB") for cell_type in types):
        return "bram"
    if types == {"URAM288"}:
        return "uram"
    raise ValidationError(f"packed cluster {cluster.get('id')!r} has no region resource")


def _site_resource(site_type: str) -> Optional[str]:
    upper = site_type.upper()
    if upper.startswith("SLICE"):
        return "slice"
    if upper.startswith("DSP"):
        return "dsp"
    if upper.startswith("RAMB"):
        return "bram"
    if upper.startswith("URAM"):
        return "uram"
    return None


def _select_xilinx_slr_window(
    packed_path: Path,
    architecture_path: Path,
    *,
    packed_value: Optional[Mapping[str, Any]] = None,
    architecture: Optional[ArchitectureDB] = None,
) -> Tuple[str, ...]:
    """Use every SLR while retaining a fail-closed capacity admission check.

    The per-FPGA resource contract limits use to 25% of the complete device;
    it does not define a 25%-area pblock.  Confining a large partition to the
    smallest capacity-feasible SLR window turns otherwise available routing
    fabric into an artificial bottleneck.  High-fanout transport nets are
    buffered before placement, so the production default spreads placement
    over the complete device and lets OpenPARF trade wirelength for routability.

    The input is the final post-Phase-6 packed/atomic netlist, including
    transport logic.  Keep the 1.5x resource-headroom check as an admission
    guard, but do not use it to shrink the legal placement region.
    """

    packed = read_json(packed_path) if packed_value is None else packed_value
    architecture = (
        ArchitectureDB.load(architecture_path)
        if architecture is None else architecture
    )
    clusters = packed.get("clusters")
    if not isinstance(clusters, Sequence) or not clusters:
        raise ValidationError("packed partition has no clusters")
    if packed.get("schema") == "emuflow.openparf-atomic-source/v1":
        atoms = Counter(
            assignment.get("cell_type")
            for cluster in clusters
            for assignment in cluster.get("assignments", [])
        )
        demand = Counter({
            "slice": max(
                math.ceil(sum(
                    count for cell_type, count in atoms.items()
                    if isinstance(cell_type, str)
                    and (cell_type.startswith("LUT") or cell_type == "LUT6_2")
                ) / 8),
                math.ceil(sum(
                    count for cell_type, count in atoms.items()
                    if isinstance(cell_type, str) and cell_type.startswith("FD")
                ) / 16),
                atoms["CARRY8"],
                math.ceil(atoms["MUXF7"] / 4),
                math.ceil(atoms["MUXF8"] / 2),
                atoms["MUXF9"],
            ),
            "dsp": atoms["DSP48E2"],
            "bram": atoms["RAMB18E2"] + atoms["RAMB36E2"],
            "uram": atoms["URAM288"],
        })
        demand += Counter()
    else:
        demand = Counter(_cluster_resource(cluster) for cluster in clusters)
    capacities: Dict[str, Counter] = {}
    rows: Dict[str, list[int]] = {}
    for site in architecture.value["sites"]:
        region = site.get("physical_region")
        if not isinstance(region, dict) or not isinstance(region.get("slr"), str):
            continue
        slr = region["slr"]
        resource = _site_resource(site["type"])
        if resource is not None:
            capacities.setdefault(slr, Counter())[resource] += 1
        tile = site.get("tile")
        row = tile.get("grid_row") if isinstance(tile, dict) else site.get("y")
        if isinstance(row, int):
            rows.setdefault(slr, []).append(row)
    if not capacities or set(capacities) != set(rows):
        raise ValidationError("ArchitectureDB has no complete physical SLR inventory")
    ordered = sorted(capacities, key=lambda name: (sum(rows[name]) / len(rows[name]), name))
    required = Counter({
        resource: math.ceil(count * _ACTIVE_REGION_RESOURCE_HEADROOM)
        for resource, count in demand.items()
        if count
    })
    device_capacity = sum((capacities[name] for name in ordered), Counter())
    if any(required[key] > device_capacity[key] for key in required):
        raise ValidationError(
            "packed partition exceeds the complete Xilinx device capacity "
            f"with {_ACTIVE_REGION_RESOURCE_HEADROOM:g}x placement headroom"
        )
    return tuple(ordered)


