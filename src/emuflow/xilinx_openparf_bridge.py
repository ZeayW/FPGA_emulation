"""Strict bridge from native OpenPARF atoms to Route A physical contracts.

The bridge does not run EmuFlow's historical site packer or legalizer.  It
independently checks a native OpenPARF atomic placement certificate, groups
the already legal atoms by their physical site, and emits the standard
PackedSiteNetlist and Xilinx placement schemas consumed by RWRoute.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, Mapping, Optional, Tuple

from .architecture import ArchitectureDB
from .errors import ValidationError
from .io import read_json, write_json
from .xilinx_openparf_atomic import OPENPARF_ATOMIC_PLACEMENT_SCHEMA
from .xilinx_packing import (
    CONSTANT_TYPES,
    FF_TYPES,
    HARD_BINDINGS,
    LUT_TYPES,
    PACKED_SITE_NETLIST_SCHEMA,
    _ff_control_set,
    validate_xilinx_packing,
)
from .xilinx_placement import (
    XILINX_OPENPARF_ATOMIC_BRIDGE_PROVIDER,
    XILINX_PLACEMENT_SCHEMA,
    XILINX_ROUTE_A_SITE_UTILIZATION_LIMIT,
    _materialize_assignment_sites,
    validate_xilinx_placement,
)
from .xilinx_primitives import XILINX_ULTRASCALEPLUS_OPEN_PROFILE


OPENPARF_ATOMIC_BRIDGE_REPORT_SCHEMA = (
    "emuflow.openparf-atomic-physical-bridge-report/v1"
)
_ATOMIC_PROVIDER = "openparf-native-mcf-direct-lg-ism-atomic-v1"
_SUPPORTED_PHYSICAL_TYPES = LUT_TYPES | FF_TYPES | set(HARD_BINDINGS)
_CERTIFICATE_ASSIGNMENT_KEYS = {
    "instance", "cell_type", "bel", "placement_mode", "source_cluster",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _select_module(
    mapped: Mapping[str, Any], top: Optional[str]
) -> Tuple[str, Mapping[str, Any]]:
    modules = mapped.get("modules")
    if not isinstance(modules, Mapping) or not modules:
        raise ValidationError("mapped JSON modules are invalid")
    if top is not None:
        module = modules.get(top)
        if not isinstance(module, Mapping):
            raise ValidationError(f"mapped JSON has no top module {top!r}")
        return top, module
    marked = [
        (str(name), module)
        for name, module in modules.items()
        if isinstance(module, Mapping)
        and str(module.get("attributes", {}).get("top", "0")) not in {"", "0"}
    ]
    if len(marked) == 1:
        return marked[0]
    if len(modules) == 1:
        name, module = next(iter(modules.items()))
        if isinstance(module, Mapping):
            return str(name), module
    raise ValidationError("mapped JSON top module is ambiguous")


def _clock_region_key(site: Mapping[str, Any]) -> Optional[Tuple[str, str, str]]:
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


def _local_utilization_summary(
    architecture: ArchitectureDB, occupied_sites: Counter
) -> Dict[str, Any]:
    capacity = Counter()
    for site in architecture.value["sites"]:
        key = _clock_region_key(site)
        if key is not None:
            capacity[key] += 1
    limits = {
        key: max(1, int(math.ceil(count * XILINX_ROUTE_A_SITE_UTILIZATION_LIMIT)))
        for key, count in capacity.items()
    }
    for key, used in occupied_sites.items():
        if used > limits[key]:
            raise ValidationError(
                "OpenPARF placement exceeds the Route A clock-region site limit"
            )
    return {
        "clock_region_site_groups": len(capacity),
        "maximum_clock_region_site_utilization": max(
            (occupied_sites[key] / count for key, count in capacity.items()),
            default=None,
        ),
        "maximum_clock_region_site_reservation": max(
            (occupied_sites[key] / limits[key] for key in capacity),
            default=None,
        ),
    }


def _validate_certificate(
    mapped: Mapping[str, Any],
    certificate: Mapping[str, Any],
    architecture: ArchitectureDB,
    top: Optional[str],
) -> Tuple[str, Mapping[str, Any], list[Dict[str, Any]], list[str]]:
    if (
        certificate.get("schema") != OPENPARF_ATOMIC_PLACEMENT_SCHEMA
        or certificate.get("status") != "pass"
        or certificate.get("provider") != _ATOMIC_PROVIDER
        or certificate.get("part") != architecture.part
    ):
        raise ValidationError("OpenPARF atomic placement identity is invalid")
    if certificate.get("runtime_validation") not in {
        "unverified", "native-openparf",
    }:
        raise ValidationError("OpenPARF atomic runtime validation is invalid")
    selected_top, module = _select_module(mapped, top)
    cells = module.get("cells")
    if not isinstance(cells, Mapping):
        raise ValidationError("mapped JSON cells are invalid")
    unsupported = sorted({
        cell.get("type") for cell in cells.values()
        if isinstance(cell, Mapping)
        and cell.get("type") not in _SUPPORTED_PHYSICAL_TYPES | CONSTANT_TYPES
    })
    if unsupported:
        raise ValidationError(
            "OpenPARF physical bridge does not support primitives: "
            + ", ".join(str(item) for item in unsupported)
        )
    constants = sorted(
        name for name, cell in cells.items()
        if isinstance(cell, Mapping) and cell.get("type") in CONSTANT_TYPES
    )
    expected = set(cells) - set(constants)
    clusters = certificate.get("clusters")
    if not isinstance(clusters, list) or not clusters:
        raise ValidationError("OpenPARF atomic placement clusters are invalid")

    owners: Dict[str, str] = {}
    physical_clusters: list[Dict[str, Any]] = []
    site_names = set()
    hard_counts: Counter = Counter()
    lut_count = 0
    ff_count = 0
    for index, entry in enumerate(clusters):
        context = f"atomic.clusters[{index}]"
        if not isinstance(entry, Mapping):
            raise ValidationError(f"{context}: expected an object")
        site_name = entry.get("site")
        site = architecture.site_named(site_name) if isinstance(site_name, str) else None
        if site is None or site_name in site_names:
            raise ValidationError(f"{context}: unknown or duplicate physical site")
        site_names.add(site_name)
        if entry.get("cluster") != f"openparf:{site_name}":
            raise ValidationError(f"{context}: site-group identity is invalid")
        if (
            entry.get("site_type") != site["type"]
            or entry.get("x") != site["x"]
            or entry.get("y") != site["y"]
        ):
            raise ValidationError(f"{context}: site metadata disagrees with ArchitectureDB")
        assignments = entry.get("assignments")
        if not isinstance(assignments, list) or not assignments:
            raise ValidationError(f"{context}: assignments are invalid")
        used_bels = set()
        slice_assignments = []
        hard_assignments = []
        for assignment_index, assignment in enumerate(assignments):
            assignment_context = f"{context}.assignments[{assignment_index}]"
            if not isinstance(assignment, Mapping) or set(assignment) != _CERTIFICATE_ASSIGNMENT_KEYS:
                raise ValidationError(f"{assignment_context}: certificate fields are invalid")
            name = assignment.get("instance")
            cell = cells.get(name)
            cell_type = assignment.get("cell_type")
            if not isinstance(cell, Mapping) or cell.get("type") != cell_type:
                raise ValidationError(f"{assignment_context}: mapped cell identity is invalid")
            if name in owners:
                raise ValidationError(f"cell {name!r} is assigned more than once")
            owners[name] = site_name
            bel_name = assignment.get("bel")
            if not isinstance(bel_name, str) or not bel_name or bel_name in used_bels:
                raise ValidationError(f"{assignment_context}: BEL is invalid or reused")
            used_bels.add(bel_name)
            compatible = [
                bel for bel in site.get("bels", [])
                if bel.get("name") == bel_name
                and cell_type in bel.get("compatible_cells", [])
            ]
            if len(compatible) != 1:
                raise ValidationError(f"{assignment_context}: BEL is not uniquely compatible")
            placement_mode = compatible[0].get("placement_mode", site["type"])
            if assignment.get("placement_mode") != placement_mode:
                raise ValidationError(f"{assignment_context}: placement mode is invalid")
            normalized = {
                "instance": name, "cell_type": cell_type, "bel": bel_name,
            }
            if cell_type in LUT_TYPES | FF_TYPES:
                slice_assignments.append(normalized)
                if cell_type in LUT_TYPES:
                    lut_count += 1
                else:
                    ff_count += 1
            elif cell_type in HARD_BINDINGS:
                hard_assignments.append(normalized)
                hard_counts[cell_type] += 1
            else:
                raise ValidationError(f"{assignment_context}: unsupported primitive")
        if slice_assignments and hard_assignments:
            raise ValidationError(f"{context}: mixes slice and hard resources")
        if hard_assignments:
            if len(assignments) != 1:
                raise ValidationError(f"{context}: hard-resource site is not singleton")
            kind = "hard"
            control_set = None
        else:
            if sum(item["cell_type"] in LUT_TYPES for item in slice_assignments) > 8:
                raise ValidationError(f"{context}: exceeds conservative LUT capacity")
            ff_names = [
                item["instance"] for item in slice_assignments
                if item["cell_type"] in FF_TYPES
            ]
            if len(ff_names) > 16:
                raise ValidationError(f"{context}: exceeds physical FF capacity")
            control_sets = {_ff_control_set(cells[name]) for name in ff_names}
            if len(control_sets) > 1:
                raise ValidationError(
                    f"{context}: cannot represent multiple FF control sets in "
                    "the conservative PackedSiteNetlist contract"
                )
            kind = "slice"
            control_set = next(iter(control_sets), None)
        modes = sorted({
            assignment["placement_mode"] for assignment in assignments
        })
        physical_clusters.append({
            "id": entry["cluster"], "kind": kind,
            "site_templates": modes, "control_set": control_set,
            "assignments": sorted(
                [*slice_assignments, *hard_assignments],
                key=lambda item: item["instance"],
            ),
        })
    if set(owners) != expected:
        missing = sorted(expected - set(owners))
        extra = sorted(set(owners) - expected)
        raise ValidationError(
            f"OpenPARF atomic placement coverage is incomplete; missing={missing}, extra={extra}"
        )
    expected_summary = {
        "atoms": len(expected), "occupied_sites": len(clusters),
        "luts": lut_count, "ffs": ff_count,
        "hard_resources": dict(sorted(hard_counts.items())),
    }
    if certificate.get("summary") != expected_summary:
        raise ValidationError("OpenPARF atomic placement summary is invalid")
    physical_clusters.sort(key=lambda item: item["id"])
    return selected_top, cells, physical_clusters, constants


def materialize_xilinx_openparf_atomic_contract(
    mapped_path: Path,
    architecture_path: Path,
    atomic_placement_path: Path,
    packed_output_path: Path,
    placement_output_path: Path,
    *,
    top: Optional[str] = None,
) -> Dict[str, Any]:
    """Materialize standard physical contracts without repacking/replacing."""

    mapped = read_json(mapped_path)
    certificate = read_json(atomic_placement_path)
    if not isinstance(mapped, Mapping) or not isinstance(certificate, Mapping):
        raise ValidationError("OpenPARF bridge inputs are invalid")
    architecture = ArchitectureDB.load(architecture_path)
    source = certificate.get("source")
    if (
        not isinstance(source, Mapping)
        or source.get("mapped_sha256") != _sha256(mapped_path)
        or source.get("architecture_sha256") != _sha256(architecture_path)
        or not isinstance(source.get("native_placement_sha256"), str)
        or not isinstance(source.get("name_map_sha256"), str)
    ):
        raise ValidationError("OpenPARF atomic placement source identity is invalid")
    selected_top, cells, clusters, constants = _validate_certificate(
        mapped, certificate, architecture, top
    )
    certificate_sha = _sha256(atomic_placement_path)
    mapped_sha = _sha256(mapped_path)
    architecture_sha = _sha256(architecture_path)
    kind_counts = Counter(cluster["kind"] for cluster in clusters)
    packed = {
        "schema": PACKED_SITE_NETLIST_SCHEMA,
        "status": "pass",
        "mapping_profile": XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
        "top": selected_top,
        "source": {
            "mapped_json_sha256": mapped_sha,
            "architecture_sha256": architecture_sha,
            "openparf_atomic_placement_sha256": certificate_sha,
        },
        "policy": {
            "provider": "openparf-native-atomic-site-group-bridge-v1",
            "source_grouping": "one-cluster-per-certified-physical-site",
            "repacking": False,
            "ff_control_sets_per_slice": 1,
        },
        "clusters": clusters,
        "cascade_chains": [],
        "unplaced_constants": constants,
        "summary": {
            "cells": len(cells),
            "placed_cells": len(cells) - len(constants),
            "constant_cells": len(constants),
            "clusters": len(clusters),
            "cluster_kinds": dict(sorted(kind_counts.items())),
            "cascade_chains": 0, "cascade_links": 0,
        },
    }

    occupied_local = Counter()
    placement_clusters = []
    for cluster in clusters:
        cluster_id = cluster["id"]
        prefix = "openparf:"
        if not cluster_id.startswith(prefix):
            raise ValidationError(
                f"OpenPARF bridge cluster id {cluster_id!r} does not start "
                f"with {prefix!r}"
            )
        # The installed HPC container still executes this bridge with
        # CPython 3.8.  Avoid str.removeprefix (added in Python 3.9) in the
        # runtime path and validate the namespace instead of silently
        # accepting a malformed id.
        site_name = cluster_id[len(prefix):]
        site = architecture.site_named(site_name)
        local_key = _clock_region_key(site)
        if local_key is not None:
            occupied_local[local_key] += 1
        assignments = []
        certificate_entry = next(
            entry for entry in certificate["clusters"]
            if entry["site"] == site_name
        )
        certificate_by_instance = {
            item["instance"]: item for item in certificate_entry["assignments"]
        }
        for assignment in cluster["assignments"]:
            source = certificate_by_instance[assignment["instance"]]
            assignments.append({
                **assignment,
                "placement_mode": source["placement_mode"],
            })
        assignments = _materialize_assignment_sites(site_name, assignments)
        placement_clusters.append({
            "cluster": cluster["id"], "site": site_name,
            "site_type": site["type"], "x": site["x"], "y": site["y"],
            "fixed": False, "physical_region": site.get("physical_region"),
            "assignments": assignments,
        })
    local_summary = _local_utilization_summary(architecture, occupied_local)

    packed_output_path.parent.mkdir(parents=True, exist_ok=True)
    placement_output_path.parent.mkdir(parents=True, exist_ok=True)
    packed_fd, packed_name = tempfile.mkstemp(
        prefix=f".{packed_output_path.name}.", suffix=".tmp",
        dir=packed_output_path.parent,
    )
    placement_fd, placement_name = tempfile.mkstemp(
        prefix=f".{placement_output_path.name}.", suffix=".tmp",
        dir=placement_output_path.parent,
    )
    os.close(packed_fd)
    os.close(placement_fd)
    packed_temp = Path(packed_name)
    placement_temp = Path(placement_name)
    try:
        write_json(packed_temp, packed, compact=True)
        placement = {
            "schema": XILINX_PLACEMENT_SCHEMA, "status": "pass",
            "part": architecture.part,
            "provider": XILINX_OPENPARF_ATOMIC_BRIDGE_PROVIDER,
            "policy": {
                "clock_region_site_utilization_limit": XILINX_ROUTE_A_SITE_UTILIZATION_LIMIT,
                "capacity_rounding": "ceil-with-one-site-minimum",
                "packing": "native-openparf-atomic-site-groups-v1",
                "placement_certificate": OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
            },
            "source": {
                "packed_sha256": _sha256(packed_temp),
                "architecture_sha256": architecture_sha,
                "guidance_sha256": None, "constraints_sha256": None,
                "openparf_atomic_placement_sha256": certificate_sha,
            },
            "clusters": placement_clusters,
            "summary": {
                "clusters": len(placement_clusters),
                "cells": len(cells) - len(constants),
                "fixed_clusters": 0, "cascade_chains": 0,
                "mean_guidance_displacement": None,
                "max_guidance_displacement": None,
                **local_summary,
                "site_types": dict(sorted(Counter(
                    item["site_type"] for item in placement_clusters
                ).items())),
            },
        }
        write_json(placement_temp, placement, compact=True)
        validate_xilinx_packing(
            mapped_path, packed_temp, top=selected_top,
            architecture_path=architecture_path,
        )
        validate_xilinx_placement(
            packed_temp, architecture_path, placement_temp
        )
        os.replace(packed_temp, packed_output_path)
        os.replace(placement_temp, placement_output_path)
    finally:
        packed_temp.unlink(missing_ok=True)
        placement_temp.unlink(missing_ok=True)

    return {
        "schema": OPENPARF_ATOMIC_BRIDGE_REPORT_SCHEMA,
        "status": "pass", "top": selected_top,
        "source": {
            "mapped_sha256": mapped_sha,
            "architecture_sha256": architecture_sha,
            "openparf_atomic_placement_sha256": certificate_sha,
        },
        "packed_sha256": _sha256(packed_output_path),
        "placement_sha256": _sha256(placement_output_path),
        "cells": len(cells), "placed_cells": len(cells) - len(constants),
        "clusters": len(clusters),
        "runtime_validation": certificate["runtime_validation"],
    }
