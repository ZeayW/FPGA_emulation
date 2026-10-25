"""Independent site/BEL/cascade validation for the OpenPARF handoff."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .architecture import ArchitectureDB
from .errors import ValidationError
from .io import file_sha256, read_json
from .xilinx_packing import PACKED_SITE_NETLIST_SCHEMA
from .xilinx_native_device_constraints import (
    load_xilinx_native_device_constraints,
)


XILINX_PLACEMENT_SCHEMA = "emuflow.xilinx-placement/v1"
XILINX_CONSTRAINTS_SCHEMA = "emuflow.xilinx-placement-constraints/v1"
XILINX_EXACT_SITE_LEGALIZER_PROVIDER = (
    "emuflow-xilinx-exact-site-legalizer-v3-physical-grid"
)
XILINX_OPENPARF_ATOMIC_BRIDGE_PROVIDER = (
    "openparf-native-mcf-direct-lg-ism-atomic-bridge-v1"
)
XILINX_ROUTE_A_SITE_UTILIZATION_LIMIT = 1.0
_SITE_XY_RE = re.compile(r"^(?P<kind>[A-Z0-9_]+)_X(?P<x>\d+)Y(?P<y>\d+)$")


def _sha256(path: Path) -> str:
    return file_sha256(path)


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


def _placer_coordinate(site: Mapping[str, Any]) -> Tuple[int, int]:
    """Return physical tile geometry instead of the unique site database key."""

    tile = site.get("tile")
    if isinstance(tile, Mapping):
        col, row = tile.get("grid_col"), tile.get("grid_row")
        if (
            isinstance(col, int) and not isinstance(col, bool) and col >= 0
            and isinstance(row, int) and not isinstance(row, bool) and row >= 0
        ):
            return col, row
    return int(site["x"]), int(site["y"])


def _clock_region_site_key(site: Mapping[str, Any]) -> Optional[Tuple[str, str, str]]:
    """Return the local-capacity bucket owned by ArchitectureDB.

    Route A reserves capacity independently for each physical site type in a
    clock region. Older synthetic fixtures without physical-region metadata
    remain placeable, but real Xilinx ArchitectureDBs always carry this key.
    """

    region = site.get("physical_region")
    if not isinstance(region, Mapping):
        return None
    clock_region = region.get("clock_region")
    if not isinstance(clock_region, str) or not clock_region:
        return None
    slr = region.get("slr")
    if not isinstance(slr, str) or not slr:
        slr = "@unspecified-slr"
    site_type = site.get("type")
    if not isinstance(site_type, str) or not site_type:
        raise ValidationError("ArchitectureDB site type is invalid")
    return slr, clock_region, site_type


def _local_site_limits(
    sites: Mapping[str, Mapping[str, Any]], utilization_limit: float
) -> Tuple[Counter, Dict[Tuple[str, str, str], int]]:
    if (
        isinstance(utilization_limit, bool)
        or not isinstance(utilization_limit, (int, float))
        or not 0.0 < float(utilization_limit) <= 1.0
    ):
        raise ValidationError("Xilinx local site utilization limit is invalid")
    capacity = Counter(
        key
        for site in sites.values()
        if (key := _clock_region_site_key(site)) is not None
    )
    limits = {
        key: max(1, int(math.ceil(count * float(utilization_limit))))
        for key, count in capacity.items()
    }
    return capacity, limits


def _materialize_assignment_sites(
    anchor_site: str, assignments: Sequence[Mapping[str, Any]],
    *, native_bram_groups: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Expand an Interchange BRAM tile anchor into RapidWright site names."""

    native_group = (
        native_bram_groups.get(anchor_site)
        if native_bram_groups is not None else None
    )
    result: List[Dict[str, Any]] = []
    for assignment in assignments:
        physical_site = anchor_site
        if native_group is not None:
            bel = assignment.get("bel")
            cell_type = assignment.get("cell_type")
            role = (
                "lower" if cell_type == "RAMB18E2" and bel == "RAMB18E2_L"
                else "upper" if cell_type == "RAMB18E2" and bel == "RAMB18E2_U"
                else "whole" if cell_type == "RAMB36E2" and bel == "RAMB36E2"
                else None
            )
            view = native_group.get(role) if role is not None else None
            if not isinstance(view, Mapping) or view.get("bel") != bel:
                raise ValidationError(
                    f"BRAM anchor {anchor_site!r} has no certified view for "
                    f"{cell_type!r} on {bel!r}"
                )
            physical_site = view.get("site")
            if not isinstance(physical_site, str) or not physical_site:
                raise ValidationError("certified BRAM view has no physical site")
        elif assignment.get("cell_type") in {"RAMB18E2", "RAMB36E2"}:
            # An unambiguous direct-site certificate may already name its
            # physical site.  Split/overlapping BRAM modes require the native
            # tile group; never reconstruct a half or whole site from an
            # anchor coordinate or a naming convention.
            physical_site = assignment.get("site")
            if physical_site != anchor_site:
                raise ValidationError(
                    f"BRAM anchor {anchor_site!r} requires a source-sealed native "
                    "BRAM tile group; coordinate/name inference is forbidden"
                )
        result.append({**assignment, "site": physical_site})
    return result


def _load_constraints(
    path: Optional[Path], cluster_ids: Set[str]
) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
    if path is None:
        return {}, None
    value = read_json(path)
    if not isinstance(value, dict) or value.get("schema") != XILINX_CONSTRAINTS_SCHEMA:
        raise ValidationError("Xilinx placement constraints header is invalid")
    entries = value.get("clusters", [])
    if not isinstance(entries, list):
        raise ValidationError("Xilinx placement constraints clusters are invalid")
    result: Dict[str, Dict[str, Any]] = {}
    global_entry = value.get("global")
    if global_entry is not None:
        if not isinstance(global_entry, dict):
            raise ValidationError("Xilinx placement global constraint is invalid")
        allowed_slrs = global_entry.get("allowed_slrs")
        if (
            not isinstance(allowed_slrs, list) or not allowed_slrs
            or any(not isinstance(item, str) or not item for item in allowed_slrs)
            or len(set(allowed_slrs)) != len(allowed_slrs)
        ):
            raise ValidationError(
                "Xilinx placement global allowed_slrs constraint is invalid"
            )
        if set(global_entry) != {"allowed_slrs"}:
            raise ValidationError("Xilinx placement global constraint has unknown fields")
        result["@global"] = {"allowed_slrs": tuple(sorted(allowed_slrs))}
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


def _cluster_constraint(
    constraints: Mapping[str, Mapping[str, Any]], cluster_id: str
) -> Dict[str, Any]:
    result = dict(constraints.get("@global", {}))
    result.update(constraints.get(cluster_id, {}))
    return result


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
    site: Mapping[str, Any], constraint: Mapping[str, Any]
) -> bool:
    if constraint.get("site") not in {None, site["name"]}:
        return False
    region = site.get("physical_region")
    if not isinstance(region, dict):
        region = {}
    allowed_slrs = constraint.get("allowed_slrs")
    if allowed_slrs is not None and region.get("slr") not in allowed_slrs:
        return False
    for key in ("slr", "clock_region"):
        if constraint.get(key) is not None and region.get(key) != constraint[key]:
            return False
    return True


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


def validate_xilinx_placement(
    packed_path: Path,
    architecture_path: Path,
    placement_path: Path,
    *,
    constraints_path: Optional[Path] = None,
    native_constraints_path: Optional[Path] = None,
    provider_manifest_path: Optional[Path] = None,
    architecture: Optional[ArchitectureDB] = None,
    packed_value: Optional[Mapping[str, Any]] = None,
    placement_value: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Independently re-check cluster ownership, sites, BELs, and cascades."""

    packed = read_json(packed_path) if packed_value is None else packed_value
    placement = (
        read_json(placement_path)
        if placement_value is None else placement_value
    )
    architecture = (
        ArchitectureDB.load(architecture_path)
        if architecture is None else architecture
    )
    if not isinstance(packed, dict) or packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA:
        raise ValidationError("PackedSiteNetlist header is invalid")
    if not isinstance(placement, dict) or placement.get("schema") != XILINX_PLACEMENT_SCHEMA:
        raise ValidationError("Xilinx placement header is invalid")
    if placement.get("status") != "pass" or placement.get("part") != architecture.part:
        raise ValidationError("Xilinx placement identity is invalid")
    provider = placement.get("provider")
    if provider not in {
        XILINX_EXACT_SITE_LEGALIZER_PROVIDER,
        XILINX_OPENPARF_ATOMIC_BRIDGE_PROVIDER,
    }:
        raise ValidationError("Xilinx placement provider is invalid")
    expected_policy = {
        "clock_region_site_utilization_limit": XILINX_ROUTE_A_SITE_UTILIZATION_LIMIT,
        "capacity_rounding": "ceil-with-one-site-minimum",
    }
    if provider == XILINX_OPENPARF_ATOMIC_BRIDGE_PROVIDER:
        expected_policy.update({
            "packing": "native-openparf-atomic-site-groups-v1",
            "placement_certificate": "emuflow.openparf-atomic-placement/v2",
            "site_pin_legalization": (
                "ultrascaleplus-slice-site-pin-legalizer-v1"
            ),
        })
    if placement.get("policy") != expected_policy:
        raise ValidationError("Xilinx placement routability policy is invalid")
    source = placement.get("source", {})
    if source.get("packed_sha256") != _sha256(packed_path):
        raise ValidationError("Xilinx placement packed digest is invalid")
    if source.get("architecture_sha256") != _sha256(architecture_path):
        raise ValidationError("Xilinx placement architecture digest is invalid")

    native = None
    native_bram_groups: Dict[str, Mapping[str, Any]] = {}
    has_native_seal = any(
        key in source
        for key in ("native_constraints_sha256", "provider_manifest_sha256")
    )
    if provider == XILINX_OPENPARF_ATOMIC_BRIDGE_PROVIDER and has_native_seal:
        if native_constraints_path is None or provider_manifest_path is None:
            raise ValidationError(
                "OpenPARF native placement validation requires native "
                "constraints and provider manifest"
            )
        if (
            source.get("native_constraints_sha256")
            != _sha256(native_constraints_path)
            or source.get("provider_manifest_sha256")
            != _sha256(provider_manifest_path)
        ):
            raise ValidationError(
                "OpenPARF native placement source identity is invalid"
            )
        native, _native_report = load_xilinx_native_device_constraints(
            native_constraints_path,
            architecture_path=architecture_path,
            provider_manifest_path=provider_manifest_path,
        )
        native_bram_groups = {
            group["anchor"]: group
            for group in native["payload"].get("bram_tile_groups", [])
        }

    cluster_by_id = {cluster["id"]: cluster for cluster in packed.get("clusters", [])}
    constraints, constraints_sha = _load_constraints(
        constraints_path, set(cluster_by_id)
    )
    if source.get("constraints_sha256") != constraints_sha:
        raise ValidationError("Xilinx placement constraint digest is invalid")
    contracts, _sites_by_template = _template_contracts(architecture)
    architecture_sites = {
        site["name"]: architecture.site_named(site["name"])
        for site in architecture.value["sites"]
    }
    local_capacity, local_limits = _local_site_limits(
        architecture_sites, XILINX_ROUTE_A_SITE_UTILIZATION_LIMIT
    )
    local_usage: Counter = Counter()
    site_base = {}
    for site in architecture.value["sites"]:
        base = site.get("template", f"@site:{site['name']}")
        site_base[site["name"]] = base

    entries = placement.get("clusters")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
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
        local_key = _clock_region_site_key(site)
        if local_key is not None:
            local_usage[local_key] += 1
            if local_usage[local_key] > local_limits[local_key]:
                raise ValidationError(
                    f"{context}: clock-region site utilization limit is violated"
                )
        if (entry.get("x"), entry.get("y")) != (site["x"], site["y"]):
            raise ValidationError(f"{context}: coordinates do not match site")
        if not _site_satisfies_constraint(
            site, _cluster_constraint(constraints, cluster_id)
        ):
            raise ValidationError(f"{context}: placement constraint is violated")
        resolved = _resolve_cluster_bels(cluster, contracts[site_base[site_name]])
        if resolved is not None:
            explicit_sites = {
                assignment.get("instance"): assignment.get("site")
                for assignment in entry.get("assignments", [])
                if isinstance(assignment, Mapping)
            }
            resolved = [
                {
                    **assignment,
                    **(
                        {"site": explicit_sites[assignment["instance"]]}
                        if assignment["instance"] in explicit_sites else {}
                    ),
                }
                for assignment in resolved
            ]
            resolved = _materialize_assignment_sites(
                site_name,
                resolved,
                native_bram_groups=(native_bram_groups or None),
            )
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
    native_edges_by_kind: Dict[str, Set[Tuple[str, str]]] = {}
    if provider == XILINX_OPENPARF_ATOMIC_BRIDGE_PROVIDER and chains:
        if native_constraints_path is None or provider_manifest_path is None:
            raise ValidationError(
                "OpenPARF cascade placement validation requires native "
                "constraints and provider manifest"
            )
        if (
            source.get("native_constraints_sha256")
            != _sha256(native_constraints_path)
            or source.get("provider_manifest_sha256")
            != _sha256(provider_manifest_path)
        ):
            raise ValidationError(
                "OpenPARF cascade placement native source identity is invalid"
            )
        if native is None:
            native, _native_report = load_xilinx_native_device_constraints(
                native_constraints_path,
                architecture_path=architecture_path,
                provider_manifest_path=provider_manifest_path,
            )
        for family in native["payload"]["dedicated_adjacency"]:
            native_edges_by_kind[family["kind"]] = {
                edge
                for native_chain in family["chains"]
                for edge in zip(native_chain, native_chain[1:])
            }
            kind_by_cell_type = {
                "CARRY8": "CARRY_NEXT",
                "DSP48E2": "DSP_CASCADE",
            "RAMB36E2": "BRAM_CASCADE",
            "URAM288": "URAM_CASCADE",
        }
        typed_chains = []
        for raw_chain in packed.get("cascade_chains", []):
            cluster_chain = []
            for instance in raw_chain.get("instances", []):
                cluster = owner.get(instance)
                if cluster is not None and (
                    not cluster_chain or cluster_chain[-1] != cluster
                ):
                    cluster_chain.append(cluster)
            if len(cluster_chain) > 1:
                typed_chains.append((raw_chain.get("cell_type"), cluster_chain))
        if [chain for _cell_type, chain in typed_chains] != chains:
            raise ValidationError("cascade chain typing is inconsistent")
        for cell_type, chain in typed_chains:
            kind = kind_by_cell_type.get(cell_type)
            if kind is None or kind not in native_edges_by_kind:
                raise ValidationError(
                    f"OpenPARF cascade type {cell_type!r} lacks native adjacency"
                )
            site_path = [placed[cluster] for cluster in chain]
            if any(
                edge not in native_edges_by_kind[kind]
                for edge in zip(site_path, site_path[1:])
            ):
                raise ValidationError(
                    "OpenPARF cascade placement violates source-sealed native "
                    "adjacency: " + " -> ".join(chain)
                )
    for chain in chains:
        coordinates = [_physical_site_coordinate(placed[cluster]) for cluster in chain]
        slrs = {
            architecture_sites[placed[cluster]].get(
                "physical_region", {}
            ).get("slr")
            for cluster in chain
        }
        if len(slrs) != 1 or None in slrs:
            raise ValidationError(
                "dedicated cascade placement crosses an SLR boundary: "
                + " -> ".join(chain)
            )
        if provider != XILINX_OPENPARF_ATOMIC_BRIDGE_PROVIDER:
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
    expected_local_summary = {
        "clock_region_site_groups": len(local_capacity),
        "maximum_clock_region_site_utilization": max(
            (
                local_usage[key] / capacity
                for key, capacity in local_capacity.items()
            ),
            default=None,
        ),
        "maximum_clock_region_site_reservation": max(
            (
                local_usage[key] / local_limits[key]
                for key in local_capacity
            ),
            default=None,
        ),
    }
    summary = placement.get("summary")
    if not isinstance(summary, dict) or any(
        summary.get(key) != expected
        for key, expected in expected_local_summary.items()
    ):
        raise ValidationError("Xilinx placement local-utilization summary is invalid")
    return {
        "status": "pass",
        "schema": "emuflow.xilinx-placement-validation/v1",
        "clusters": len(placed),
        "cells": len(cell_owners),
        "cascade_chains": len(chains),
        "placement_sha256": _sha256(placement_path),
    }
