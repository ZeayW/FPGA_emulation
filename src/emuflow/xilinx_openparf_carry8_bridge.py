"""Strict bridge from native OpenPARF CARRY8 placement to RWRoute inputs.

The bridge preserves the already validated PackedSiteNetlist.  It only binds
each packed cluster to the exact physical site and BEL roles certified by the
native OpenPARF result; it never repacks, replaces, or searches for a site.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, Mapping, Optional

from .architecture import ArchitectureDB
from .errors import ValidationError
from .io import read_json, write_json
from .xilinx_openparf_carry8 import (
    OPENPARF_CARRY8_PLACEMENT_SCHEMA,
    OPENPARF_CARRY8_PROVIDER,
)
from .xilinx_packing import (
    PACKED_SITE_NETLIST_SCHEMA,
    validate_xilinx_packing,
)
from .xilinx_placement import (
    XILINX_OPENPARF_CARRY8_BRIDGE_PROVIDER,
    XILINX_PLACEMENT_SCHEMA,
    XILINX_ROUTE_A_SITE_UTILIZATION_LIMIT,
    _materialize_assignment_sites,
    validate_xilinx_placement,
)


OPENPARF_CARRY8_BRIDGE_REPORT_SCHEMA = (
    "emuflow.openparf-carry8-physical-bridge-report/v1"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _region_key(site: Mapping[str, Any]) -> Optional[tuple[str, str, str]]:
    region = site.get("physical_region")
    if not isinstance(region, Mapping):
        return None
    clock_region = region.get("clock_region")
    if not isinstance(clock_region, str) or not clock_region:
        return None
    slr = region.get("slr")
    if not isinstance(slr, str) or not slr:
        slr = "@unspecified-slr"
    return slr, clock_region, str(site["type"])


def materialize_xilinx_openparf_carry8_contract(
    mapped_path: Path,
    packed_path: Path,
    architecture_path: Path,
    carry8_placement_path: Path,
    placement_output_path: Path,
    *,
    top: Optional[str] = None,
) -> Dict[str, Any]:
    """Materialize a standard placement without modifying packed clusters."""

    mapped = read_json(mapped_path)
    packed = read_json(packed_path)
    certificate = read_json(carry8_placement_path)
    if not all(isinstance(value, Mapping) for value in (mapped, packed, certificate)):
        raise ValidationError("OpenPARF CARRY8 bridge inputs are invalid")
    if packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA:
        raise ValidationError("OpenPARF CARRY8 bridge packing schema is invalid")
    architecture = ArchitectureDB.load(architecture_path)
    if (
        certificate.get("schema") != OPENPARF_CARRY8_PLACEMENT_SCHEMA
        or certificate.get("status") != "pass"
        or certificate.get("provider") != OPENPARF_CARRY8_PROVIDER
        or certificate.get("part") != architecture.part
        or certificate.get("runtime_validation") != "native-openparf"
    ):
        raise ValidationError("OpenPARF CARRY8 bridge certificate is not runtime-qualified")
    source = certificate.get("source")
    if (
        not isinstance(source, Mapping)
        or source.get("mapped_sha256") != _sha256(mapped_path)
        or source.get("architecture_sha256") != _sha256(architecture_path)
    ):
        raise ValidationError("OpenPARF CARRY8 bridge source identity is invalid")
    validate_xilinx_packing(
        mapped_path, packed_path, top=top, architecture_path=architecture_path
    )
    selected_top = packed.get("top")
    if not isinstance(selected_top, str) or (top is not None and selected_top != top):
        raise ValidationError("OpenPARF CARRY8 bridge top module is invalid")

    packed_clusters = packed.get("clusters")
    certificate_clusters = certificate.get("clusters")
    if not isinstance(packed_clusters, list) or not isinstance(certificate_clusters, list):
        raise ValidationError("OpenPARF CARRY8 bridge clusters are invalid")
    packed_by_id = {
        cluster.get("id"): cluster for cluster in packed_clusters
        if isinstance(cluster, Mapping) and isinstance(cluster.get("id"), str)
    }
    if len(packed_by_id) != len(packed_clusters):
        raise ValidationError("PackedSiteNetlist has duplicate or invalid cluster ids")

    placed_by_cluster: Dict[str, list[Dict[str, Any]]] = defaultdict(list)
    site_by_cluster: Dict[str, str] = {}
    certificate_cells = set()
    used_sites = set()
    for entry in certificate_clusters:
        if not isinstance(entry, Mapping):
            raise ValidationError("OpenPARF CARRY8 certificate cluster is invalid")
        site_name = entry.get("site")
        site = architecture.site_named(site_name) if isinstance(site_name, str) else None
        if site is None or site_name in used_sites:
            raise ValidationError("OpenPARF CARRY8 bridge has an unknown or reused site")
        used_sites.add(site_name)
        assignments = entry.get("assignments")
        if not isinstance(assignments, list) or not assignments:
            raise ValidationError("OpenPARF CARRY8 bridge assignment group is invalid")
        source_clusters = {
            item.get("source_cluster")
            for item in assignments
            if isinstance(item, Mapping)
        }
        if any(
            not isinstance(cluster_id, str) or cluster_id not in packed_by_id
            for cluster_id in source_clusters
        ):
            raise ValidationError("OpenPARF CARRY8 source cluster identity is invalid")
        if len(source_clusters) != 1:
            raise ValidationError(
                "OpenPARF CARRY8 bridge refuses to merge packed clusters at one site"
            )
        cluster_id = next(iter(source_clusters))
        if cluster_id not in packed_by_id or cluster_id in site_by_cluster:
            raise ValidationError("OpenPARF CARRY8 source cluster identity is invalid")
        site_by_cluster[cluster_id] = site_name
        for assignment in assignments:
            if not isinstance(assignment, Mapping):
                raise ValidationError("OpenPARF CARRY8 bridge assignment is invalid")
            name = assignment.get("instance")
            if not isinstance(name, str) or name in certificate_cells:
                raise ValidationError("OpenPARF CARRY8 bridge cell ownership is invalid")
            certificate_cells.add(name)
            placed_by_cluster[cluster_id].append({
                "instance": name,
                "cell_type": assignment.get("cell_type"),
                "bel": assignment.get("bel"),
                "placement_mode": assignment.get("placement_mode"),
            })

    if set(site_by_cluster) != set(packed_by_id):
        raise ValidationError("OpenPARF CARRY8 bridge cluster coverage is incomplete")
    placements = []
    usage: Counter = Counter()
    capacity: Counter = Counter()
    for site in architecture.sites:
        key = _region_key(site)
        if key is not None:
            capacity[key] += 1
    for cluster_id in sorted(packed_by_id):
        cluster = packed_by_id[cluster_id]
        expected = [
            {
                "instance": item.get("instance"),
                "cell_type": item.get("cell_type"),
                "bel": item.get("bel"),
            }
            for item in cluster.get("assignments", [])
        ]
        actual_by_instance = {
            item["instance"]: item for item in placed_by_cluster[cluster_id]
        }
        actual = [
            actual_by_instance.get(item["instance"])
            for item in expected
        ]
        if any(item is None for item in actual):
            raise ValidationError(
                f"OpenPARF CARRY8 bridge changed packed cluster {cluster_id!r}"
            )
        actual_contract = [
            {key: item.get(key) for key in ("instance", "cell_type", "bel")}
            for item in actual
        ]
        if actual_contract != expected:
            raise ValidationError(
                f"OpenPARF CARRY8 bridge changed packed cluster {cluster_id!r}"
            )
        site_name = site_by_cluster[cluster_id]
        site = architecture.site_named(site_name)
        key = _region_key(site)
        if key is not None:
            usage[key] += 1
            limit = max(1, math.ceil(capacity[key] * 0.75))
            if usage[key] > limit:
                raise ValidationError("OpenPARF CARRY8 clock-region utilization exceeds 75%")
        placements.append({
            "cluster": cluster_id,
            "site": site_name,
            "site_type": site["type"],
            "x": site["x"],
            "y": site["y"],
            "fixed": False,
            "physical_region": site.get("physical_region"),
            "assignments": _materialize_assignment_sites(site_name, actual),
        })

    local_summary = {
        "clock_region_site_groups": len(capacity),
        "maximum_clock_region_site_utilization": max(
            (usage[key] / count for key, count in capacity.items()), default=None
        ),
        "maximum_clock_region_site_reservation": max(
            (usage[key] / max(1, math.ceil(count * 0.75)) for key, count in capacity.items()),
            default=None,
        ),
    }
    result = {
        "schema": XILINX_PLACEMENT_SCHEMA,
        "status": "pass",
        "part": architecture.part,
        "provider": XILINX_OPENPARF_CARRY8_BRIDGE_PROVIDER,
        "policy": {
            "clock_region_site_utilization_limit": XILINX_ROUTE_A_SITE_UTILIZATION_LIMIT,
            "capacity_rounding": "ceil-with-one-site-minimum",
            "packing": "preserved-carry8-full-slice-macros-v1",
            "placement_certificate": OPENPARF_CARRY8_PLACEMENT_SCHEMA,
        },
        "source": {
            "packed_sha256": _sha256(packed_path),
            "architecture_sha256": _sha256(architecture_path),
            "guidance_sha256": None,
            "constraints_sha256": None,
            "openparf_carry8_placement_sha256": _sha256(carry8_placement_path),
        },
        "clusters": placements,
        "summary": {
            "clusters": len(placements),
            "cells": len(certificate_cells),
            "fixed_clusters": 0,
            "cascade_chains": len(packed.get("cascade_chains", [])),
            "mean_guidance_displacement": None,
            "max_guidance_displacement": None,
            **local_summary,
            "site_types": dict(sorted(Counter(item["site_type"] for item in placements).items())),
        },
    }
    placement_output_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{placement_output_path.name}.",
        suffix=".tmp",
        dir=placement_output_path.parent,
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        write_json(temporary_path, result, compact=True)
        validate_xilinx_placement(packed_path, architecture_path, temporary_path)
        os.replace(temporary_path, placement_output_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return {
        "schema": OPENPARF_CARRY8_BRIDGE_REPORT_SCHEMA,
        "status": "pass",
        "top": selected_top,
        "cells": len(certificate_cells),
        "clusters": len(placements),
        "placement_sha256": _sha256(placement_output_path),
        "runtime_validation": "native-openparf",
    }
