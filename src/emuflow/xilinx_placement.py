"""Exact site legalization for packed UltraScale+ clusters."""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .architecture import ArchitectureDB
from .errors import ValidationError
from .io import read_json, write_json
from .xilinx_packing import PACKED_SITE_NETLIST_SCHEMA


XILINX_PLACEMENT_SCHEMA = "emuflow.xilinx-placement/v1"
XILINX_GUIDANCE_SCHEMA = "emuflow.xilinx-global-placement-guidance/v1"
XILINX_CONSTRAINTS_SCHEMA = "emuflow.xilinx-placement-constraints/v1"
_SITE_XY_RE = re.compile(r"^(?P<kind>[A-Z0-9_]+)_X(?P<x>\d+)Y(?P<y>\d+)$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _nonempty(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{context}: expected a non-empty string")
    return value


def _physical_site_coordinate(name: str) -> Tuple[str, int, int]:
    match = _SITE_XY_RE.fullmatch(name)
    if match is None:
        raise ValidationError(
            f"site {name!r} does not expose an exact physical X/Y identity"
        )
    return match.group("kind"), int(match.group("x")), int(match.group("y"))


def _load_guidance(
    path: Optional[Path], cluster_ids: Set[str]
) -> Tuple[Dict[str, Tuple[float, float]], Optional[str]]:
    if path is None:
        return {}, None
    value = read_json(path)
    if not isinstance(value, dict) or value.get("schema") != XILINX_GUIDANCE_SCHEMA:
        raise ValidationError("Xilinx placement guidance header is invalid")
    entries = value.get("clusters")
    if not isinstance(entries, list):
        raise ValidationError("Xilinx placement guidance clusters are invalid")
    result: Dict[str, Tuple[float, float]] = {}
    for index, entry in enumerate(entries):
        context = f"guidance.clusters[{index}]"
        if not isinstance(entry, dict):
            raise ValidationError(f"{context}: expected an object")
        cluster_id = _nonempty(entry.get("cluster"), f"{context}.cluster")
        if cluster_id not in cluster_ids:
            raise ValidationError(f"{context}: unknown cluster {cluster_id!r}")
        if cluster_id in result:
            raise ValidationError(f"{context}: duplicate cluster {cluster_id!r}")
        x, y = entry.get("x"), entry.get("y")
        if (
            isinstance(x, bool) or not isinstance(x, (int, float))
            or isinstance(y, bool) or not isinstance(y, (int, float))
        ):
            raise ValidationError(f"{context}: x/y must be numeric")
        result[cluster_id] = (float(x), float(y))
    return result, _sha256(path)


def _load_constraints(
    path: Optional[Path], cluster_ids: Set[str]
) -> Tuple[Dict[str, Dict[str, str]], Optional[str]]:
    if path is None:
        return {}, None
    value = read_json(path)
    if not isinstance(value, dict) or value.get("schema") != XILINX_CONSTRAINTS_SCHEMA:
        raise ValidationError("Xilinx placement constraints header is invalid")
    entries = value.get("clusters")
    if not isinstance(entries, list):
        raise ValidationError("Xilinx placement constraints clusters are invalid")
    result: Dict[str, Dict[str, str]] = {}
    for index, entry in enumerate(entries):
        context = f"constraints.clusters[{index}]"
        if not isinstance(entry, dict):
            raise ValidationError(f"{context}: expected an object")
        cluster_id = _nonempty(entry.get("cluster"), f"{context}.cluster")
        if cluster_id not in cluster_ids:
            raise ValidationError(f"{context}: unknown cluster {cluster_id!r}")
        if cluster_id in result:
            raise ValidationError(f"{context}: duplicate cluster {cluster_id!r}")
        contract = {}
        for key in ("site", "slr", "clock_region"):
            if entry.get(key) is not None:
                contract[key] = _nonempty(entry[key], f"{context}.{key}")
        if not contract:
            raise ValidationError(f"{context}: empty constraint")
        result[cluster_id] = contract
    return result, _sha256(path)


def _template_contracts(
    architecture: ArchitectureDB,
) -> Tuple[Dict[str, Dict[str, Dict[str, Any]]], Dict[str, List[str]]]:
    templates = architecture.value.get("site_templates", {})
    contracts: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for base, template in templates.items():
        modes = [base] + list(template.get("alternative_templates", []))
        bels: Dict[str, Dict[str, Any]] = {}
        for mode in modes:
            for bel in templates[mode]["bels"]:
                existing = bels.get(bel["name"])
                candidate = {**bel, "placement_mode": mode}
                if existing is not None and existing != candidate:
                    raise ValidationError(
                        f"site template {base!r} has ambiguous BEL {bel['name']!r}"
                    )
                bels[bel["name"]] = candidate
        contracts[base] = bels

    sites_by_template: Dict[str, List[str]] = defaultdict(list)
    for site in architecture.value["sites"]:
        base = site.get("template")
        if base is None:
            # Explicit-BEL fixtures are represented by a site-local contract.
            base = f"@site:{site['name']}"
            contracts[base] = {
                bel["name"]: {**bel, "placement_mode": site["type"]}
                for bel in site["bels"]
            }
        sites_by_template[base].append(site["name"])
    for names in sites_by_template.values():
        names.sort(key=_physical_site_coordinate)
    return contracts, dict(sites_by_template)


def _resolve_cluster_bels(
    cluster: Mapping[str, Any], bels: Mapping[str, Mapping[str, Any]]
) -> Optional[List[Dict[str, Any]]]:
    declared_modes = set(cluster.get("site_templates", []))
    if not declared_modes:
        raise ValidationError(f"cluster {cluster.get('id')!r} has no site templates")
    resolved = []
    used = set()
    for assignment in cluster.get("assignments", []):
        cell_type = assignment.get("cell_type")
        candidates = assignment.get("bel_candidates")
        if candidates is None:
            candidates = [assignment.get("bel")]
        if (
            not isinstance(candidates, list) or not candidates
            or any(not isinstance(candidate, str) for candidate in candidates)
        ):
            raise ValidationError(
                f"cluster {cluster.get('id')!r} has an invalid BEL contract"
            )
        selected = None
        for candidate_name in sorted(candidates):
            candidate = bels.get(candidate_name)
            if (
                candidate is not None
                and cell_type in candidate.get("compatible_cells", [])
                and candidate.get("placement_mode") in declared_modes
                and candidate_name not in used
            ):
                selected = candidate
                break
        if selected is None:
            return None
        used.add(selected["name"])
        resolved.append({
            "instance": assignment["instance"],
            "cell_type": cell_type,
            "bel": selected["name"],
            "placement_mode": selected["placement_mode"],
        })
    return resolved


def _site_satisfies_constraint(
    site: Mapping[str, Any], constraint: Mapping[str, str]
) -> bool:
    if constraint.get("site") not in {None, site["name"]}:
        return False
    region = site.get("physical_region")
    if not isinstance(region, dict):
        region = {}
    for key in ("slr", "clock_region"):
        if constraint.get(key) is not None and region.get(key) != constraint[key]:
            return False
    return True


def _distance(
    cluster_ids: Sequence[str], sites: Sequence[Mapping[str, Any]],
    guidance: Mapping[str, Tuple[float, float]],
) -> Tuple[float, Tuple[str, ...]]:
    cost = 0.0
    for cluster_id, site in zip(cluster_ids, sites):
        target = guidance.get(cluster_id)
        if target is not None:
            cost += abs(site["x"] - target[0]) + abs(site["y"] - target[1])
    return cost, tuple(site["name"] for site in sites)


def _cascade_cluster_chains(
    packed: Mapping[str, Any], owner: Mapping[str, str]
) -> List[List[str]]:
    chains = []
    occupied: Dict[str, int] = {}
    for chain_index, chain in enumerate(packed.get("cascade_chains", [])):
        clusters = []
        for instance in chain.get("instances", []):
            cluster_id = owner.get(instance)
            if cluster_id is None:
                raise ValidationError("cascade certificate references an unknown cell")
            if not clusters or clusters[-1] != cluster_id:
                clusters.append(cluster_id)
        if len(clusters) <= 1:
            continue
        if len(set(clusters)) != len(clusters):
            raise ValidationError("cascade chain revisits a packed cluster")
        for cluster_id in clusters:
            previous = occupied.get(cluster_id)
            if previous is not None and previous != chain_index:
                raise ValidationError("packed cluster belongs to multiple cascade chains")
            occupied[cluster_id] = chain_index
        chains.append(clusters)
    return chains


def place_xilinx_clusters(
    packed_path: Path,
    architecture_path: Path,
    output_path: Path,
    *,
    guidance_path: Optional[Path] = None,
    constraints_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Place whole packed clusters onto exact, non-overlapping device sites."""

    packed = read_json(packed_path)
    if not isinstance(packed, dict) or packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA:
        raise ValidationError("PackedSiteNetlist header is invalid")
    architecture = ArchitectureDB.load(architecture_path)
    clusters = packed.get("clusters")
    if not isinstance(clusters, list):
        raise ValidationError("PackedSiteNetlist clusters are invalid")
    cluster_by_id = {}
    owner = {}
    for cluster in clusters:
        cluster_id = _nonempty(cluster.get("id"), "packed cluster id")
        if cluster_id in cluster_by_id:
            raise ValidationError(f"duplicate packed cluster {cluster_id!r}")
        cluster_by_id[cluster_id] = cluster
        for assignment in cluster.get("assignments", []):
            instance = assignment.get("instance")
            if instance in owner:
                raise ValidationError(f"cell {instance!r} belongs to multiple clusters")
            owner[instance] = cluster_id
    cluster_ids = set(cluster_by_id)
    guidance, guidance_sha = _load_guidance(guidance_path, cluster_ids)
    constraints, constraints_sha = _load_constraints(constraints_path, cluster_ids)
    contracts, sites_by_template = _template_contracts(architecture)

    sites = {site["name"]: architecture.site_named(site["name"])
             for site in architecture.value["sites"]}
    candidates: Dict[str, List[str]] = {}
    resolved_by_cluster_template: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for cluster_id, cluster in cluster_by_id.items():
        legal_sites = []
        for base, site_names in sites_by_template.items():
            resolved = _resolve_cluster_bels(cluster, contracts[base])
            if resolved is None:
                continue
            resolved_by_cluster_template[(cluster_id, base)] = resolved
            for site_name in site_names:
                if _site_satisfies_constraint(
                    sites[site_name], constraints.get(cluster_id, {})
                ):
                    legal_sites.append(site_name)
        if not legal_sites:
            raise ValidationError(
                f"cluster {cluster_id!r} has no legal site after exact constraints"
            )
        candidates[cluster_id] = sorted(set(legal_sites))

    site_base = {}
    for base, names in sites_by_template.items():
        for name in names:
            site_base[name] = base

    placed: Dict[str, str] = {}
    used_sites: Set[str] = set()
    chains = _cascade_cluster_chains(packed, owner)
    chain_windows = []
    for chain in chains:
        coordinate_maps = []
        for cluster_id in chain:
            coordinates = {
                _physical_site_coordinate(name): name
                for name in candidates[cluster_id]
            }
            coordinate_maps.append(coordinates)
        windows = []
        for coordinate, first_name in coordinate_maps[0].items():
            kind, physical_x, physical_y = coordinate
            names = [first_name]
            for offset, coordinate_map in enumerate(coordinate_maps[1:], start=1):
                name = coordinate_map.get((kind, physical_x, physical_y + offset))
                if name is None:
                    break
                names.append(name)
            if len(names) == len(chain):
                windows.append(names)
        if not windows:
            raise ValidationError(
                "dedicated cascade has no legal contiguous physical-site window: "
                + " -> ".join(chain)
            )
        chain_windows.append((len(windows), chain, windows))

    for _count, chain, windows in sorted(chain_windows, key=lambda item: (item[0], item[1])):
        available = [
            names for names in windows if not used_sites.intersection(names)
        ]
        if not available:
            raise ValidationError(
                "dedicated cascade windows conflict for chain: " + " -> ".join(chain)
            )
        selected = min(
            available,
            key=lambda names: _distance(
                chain, [sites[name] for name in names], guidance
            ),
        )
        for cluster_id, site_name in zip(chain, selected):
            placed[cluster_id] = site_name
            used_sites.add(site_name)

    remaining = sorted(
        (cluster_id for cluster_id in cluster_ids if cluster_id not in placed),
        key=lambda cluster_id: (
            len(candidates[cluster_id]),
            -len(cluster_by_id[cluster_id].get("assignments", [])),
            cluster_id,
        ),
    )
    for cluster_id in remaining:
        available = [
            name for name in candidates[cluster_id] if name not in used_sites
        ]
        if not available:
            raise ValidationError(
                f"no unoccupied legal site remains for cluster {cluster_id!r}"
            )
        selected = min(
            available,
            key=lambda name: _distance(
                [cluster_id], [sites[name]], guidance
            ),
        )
        placed[cluster_id] = selected
        used_sites.add(selected)

    placements = []
    displacement = []
    for cluster_id in sorted(cluster_ids):
        site_name = placed[cluster_id]
        site = sites[site_name]
        base = site_base[site_name]
        assignments = resolved_by_cluster_template[(cluster_id, base)]
        target = guidance.get(cluster_id)
        if target is not None:
            displacement.append(
                abs(site["x"] - target[0]) + abs(site["y"] - target[1])
            )
        placements.append({
            "cluster": cluster_id,
            "site": site_name,
            "site_type": site["type"],
            "x": site["x"],
            "y": site["y"],
            "fixed": "site" in constraints.get(cluster_id, {}),
            "physical_region": site.get("physical_region"),
            "assignments": assignments,
        })

    result = {
        "schema": XILINX_PLACEMENT_SCHEMA,
        "status": "pass",
        "part": architecture.part,
        "provider": "emuflow-xilinx-exact-site-legalizer-v1",
        "source": {
            "packed_sha256": _sha256(packed_path),
            "architecture_sha256": _sha256(architecture_path),
            "guidance_sha256": guidance_sha,
            "constraints_sha256": constraints_sha,
        },
        "clusters": placements,
        "summary": {
            "clusters": len(placements),
            "cells": sum(len(item["assignments"]) for item in placements),
            "fixed_clusters": sum(item["fixed"] for item in placements),
            "cascade_chains": len(chains),
            "mean_guidance_displacement": (
                sum(displacement) / len(displacement) if displacement else None
            ),
            "max_guidance_displacement": max(displacement) if displacement else None,
            "site_types": dict(sorted(Counter(
                item["site_type"] for item in placements
            ).items())),
        },
    }
    write_json(output_path, result, compact=True)
    validate_xilinx_placement(
        packed_path,
        architecture_path,
        output_path,
        constraints_path=constraints_path,
    )
    return result


def validate_xilinx_placement(
    packed_path: Path,
    architecture_path: Path,
    placement_path: Path,
    *,
    constraints_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Independently re-check cluster ownership, sites, BELs, and cascades."""

    packed = read_json(packed_path)
    placement = read_json(placement_path)
    architecture = ArchitectureDB.load(architecture_path)
    if not isinstance(packed, dict) or packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA:
        raise ValidationError("PackedSiteNetlist header is invalid")
    if not isinstance(placement, dict) or placement.get("schema") != XILINX_PLACEMENT_SCHEMA:
        raise ValidationError("Xilinx placement header is invalid")
    if placement.get("status") != "pass" or placement.get("part") != architecture.part:
        raise ValidationError("Xilinx placement identity is invalid")
    source = placement.get("source", {})
    if source.get("packed_sha256") != _sha256(packed_path):
        raise ValidationError("Xilinx placement packed digest is invalid")
    if source.get("architecture_sha256") != _sha256(architecture_path):
        raise ValidationError("Xilinx placement architecture digest is invalid")

    cluster_by_id = {cluster["id"]: cluster for cluster in packed.get("clusters", [])}
    constraints, constraints_sha = _load_constraints(
        constraints_path, set(cluster_by_id)
    )
    if source.get("constraints_sha256") != constraints_sha:
        raise ValidationError("Xilinx placement constraint digest is invalid")
    contracts, _sites_by_template = _template_contracts(architecture)
    site_base = {}
    for site in architecture.value["sites"]:
        base = site.get("template", f"@site:{site['name']}")
        site_base[site["name"]] = base

    entries = placement.get("clusters")
    if not isinstance(entries, list):
        raise ValidationError("Xilinx placement clusters are invalid")
    placed = {}
    occupied = set()
    cell_owners = {}
    for index, entry in enumerate(entries):
        context = f"placement.clusters[{index}]"
        if not isinstance(entry, dict):
            raise ValidationError(f"{context}: expected an object")
        cluster_id = entry.get("cluster")
        cluster = cluster_by_id.get(cluster_id)
        if cluster is None or cluster_id in placed:
            raise ValidationError(f"{context}: unknown or duplicate cluster")
        site_name = entry.get("site")
        site = architecture.site_named(site_name) if isinstance(site_name, str) else None
        if site is None:
            raise ValidationError(f"{context}: unknown site")
        if site_name in occupied:
            raise ValidationError(f"{context}: physical site overlap")
        occupied.add(site_name)
        if (entry.get("x"), entry.get("y")) != (site["x"], site["y"]):
            raise ValidationError(f"{context}: coordinates do not match site")
        if not _site_satisfies_constraint(site, constraints.get(cluster_id, {})):
            raise ValidationError(f"{context}: placement constraint is violated")
        resolved = _resolve_cluster_bels(cluster, contracts[site_base[site_name]])
        if resolved is None or entry.get("assignments") != resolved:
            raise ValidationError(f"{context}: exact BEL assignment is invalid")
        for assignment in resolved:
            instance = assignment["instance"]
            if instance in cell_owners:
                raise ValidationError(f"cell {instance!r} is placed more than once")
            cell_owners[instance] = cluster_id
        placed[cluster_id] = site_name
    if set(placed) != set(cluster_by_id):
        raise ValidationError("Xilinx placement cluster ownership is incomplete")

    packed_cells = {
        assignment["instance"]
        for cluster in cluster_by_id.values()
        for assignment in cluster.get("assignments", [])
    }
    if set(cell_owners) != packed_cells:
        raise ValidationError("Xilinx placement cell ownership is incomplete")
    owner = {instance: cluster_id for instance, cluster_id in cell_owners.items()}
    chains = _cascade_cluster_chains(packed, owner)
    for chain in chains:
        coordinates = [_physical_site_coordinate(placed[cluster]) for cluster in chain]
        first_kind, first_x, first_y = coordinates[0]
        expected = [
            (first_kind, first_x, first_y + offset)
            for offset in range(len(chain))
        ]
        if coordinates != expected:
            raise ValidationError(
                "dedicated cascade placement is not physically contiguous: "
                + " -> ".join(chain)
            )
    return {
        "status": "pass",
        "schema": "emuflow.xilinx-placement-validation/v1",
        "clusters": len(placed),
        "cells": len(cell_owners),
        "cascade_chains": len(chains),
        "placement_sha256": _sha256(placement_path),
    }
