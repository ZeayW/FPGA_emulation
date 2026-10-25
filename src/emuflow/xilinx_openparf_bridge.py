"""Strict bridge from native OpenPARF atoms to Route A physical contracts.

The bridge does not run EmuFlow's historical site packer or site placer.  It
independently checks a native OpenPARF atomic placement certificate, groups
the atoms by their certified physical site, legalizes only intra-site FF BELs
against the device's shared MUX-select pins, and publishes one typed physical
handoff exposing packing and placement views to independent validators and RWRoute.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Sequence
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

from .architecture import ArchitectureDB
from .errors import ValidationError
from .io import file_sha256, read_json
from .xilinx_openparf_atomic import (
    OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
    OPENPARF_ATOMIC_PROVIDER,
    OPENPARF_ATOMIC_SOURCE_SCHEMA,
    open_xilinx_openparf_atomic_placement,
)
from .xilinx_openparf_source_storage import (
    OPENPARF_ATOMIC_SOURCE_STORAGE_SCHEMA,
    XilinxOpenparfAtomicSourceView,
    read_xilinx_openparf_atomic_source,
)
from .xilinx_netlist import is_xilinx_mapped_index
from .xilinx_native_device_constraints import (
    load_xilinx_native_device_constraints,
)
from .xilinx_packing import (
    CONSTANT_TYPES,
    DUAL_OUTPUT_LUT_TYPE,
    FF_TYPES,
    HARD_BINDINGS,
    LUT_TYPES,
    MUX_TYPES,
    PACKED_SITE_NETLIST_SCHEMA,
    SLICE_FF_BELS,
    SLICE_LUT_BELS,
    _bits,
    _derive_cascade_chains,
    _mux_input_driver,
    _slice_site_pin_conflicts,
    _validate_declared_slice_control_sets,
    _slice_ff_control_sets,
    legalize_xilinx_slice_site_pins,
)
from .xilinx_placement import (
    XILINX_OPENPARF_ATOMIC_BRIDGE_PROVIDER,
    XILINX_CONSTRAINTS_SCHEMA,
    XILINX_PLACEMENT_SCHEMA,
    XILINX_ROUTE_A_SITE_UTILIZATION_LIMIT,
    _clock_region_site_key,
    _site_satisfies_constraint,
    _materialize_assignment_sites,
    _resolve_cluster_bels,
    _template_contracts,
)
from .xilinx_primitives import XILINX_ULTRASCALEPLUS_OPEN_PROFILE
from .xilinx_physical_handoff import (
    XilinxPhysicalHandoffBuilder,
    iter_xilinx_physical_handoff_cluster_pairs,
    open_xilinx_physical_handoff,
    validate_xilinx_physical_handoff,
)


OPENPARF_ATOMIC_BRIDGE_REPORT_SCHEMA = (
    "emuflow.openparf-atomic-physical-bridge-report/v1"
)
_SUPPORTED_HARD_TYPES = set(HARD_BINDINGS) | {"RAMB18E2"}
_SUPPORTED_PHYSICAL_TYPES = (
    LUT_TYPES | {DUAL_OUTPUT_LUT_TYPE, "CARRY8"}
    | FF_TYPES | MUX_TYPES | _SUPPORTED_HARD_TYPES
)
_CERTIFICATE_ASSIGNMENT_KEYS = {
    "instance", "cell_type", "bel", "physical_site", "placement_mode",
    "source_cluster", "bram_tile_group",
}


def _sha256(path: Path) -> str:
    return file_sha256(path)


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
    source_packed: Optional[Mapping[str, Any]] = None,
    certificate_clusters: Optional[Iterable[Mapping[str, Any]]] = None,
    native_bram_groups: Optional[Mapping[str, Mapping[str, Any]]] = None,
    *,
    physical_cluster_sink: Callable[[Mapping[str, Any]], None],
    sink_enforces_assignment_uniqueness: bool = False,
    source_assignment_sink: Optional[Callable[[str, str], None]] = None,
    source_ownership_validation: Optional[Mapping[str, Any]] = None,
) -> Tuple[
    str, Mapping[str, Any], list[str],
    list[Dict[str, Any]], Mapping[str, Any]
]:
    if (
        certificate.get("schema") != OPENPARF_ATOMIC_PLACEMENT_SCHEMA
        or certificate.get("status") != "pass"
        or certificate.get("provider") != OPENPARF_ATOMIC_PROVIDER
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
    types_by_id = getattr(cells, "types_by_id", None)
    if callable(types_by_id):
        unsupported = sorted(
            {
                kind
                for _name, kind in types_by_id()
                if kind not in _SUPPORTED_PHYSICAL_TYPES | CONSTANT_TYPES
            }
        )
        constants = [name for name, kind in types_by_id() if kind in CONSTANT_TYPES]
    else:
        unsupported = sorted(
            {
                cell.get("type")
                for cell in cells.values()
                if isinstance(cell, Mapping)
                and cell.get("type") not in _SUPPORTED_PHYSICAL_TYPES | CONSTANT_TYPES
            }
        )
        constants = sorted(
            name
            for name, cell in cells.items()
            if isinstance(cell, Mapping) and cell.get("type") in CONSTANT_TYPES
        )
    if unsupported:
        raise ValidationError(
            "OpenPARF physical bridge does not support primitives: "
            + ", ".join(str(item) for item in unsupported)
        )
    expected_count = len(cells) - len(constants)
    cascades: list[Dict[str, Any]] = []
    if source_ownership_validation is not None and (
        source_packed is None or source_assignment_sink is not None
        or source_ownership_validation.get("schema") != "emuflow.atomic-ownership-validation/v1"
        or source_ownership_validation.get("status") != "pass"
        or source_ownership_validation.get("implementation_id") != "native-atomic-ownership-merge-v1"
        or source_ownership_validation.get("assignments") != expected_count
        or source_ownership_validation.get("ownership_snapshots") != 0
    ):
        raise ValidationError("OpenPARF native source ownership validation disagrees")
    source_owner: Optional[Dict[str, str]] = (
        None if source_assignment_sink is not None or source_ownership_validation is not None else {}
    )
    source_owner_count = 0
    if source_packed is not None:
        if (
            source_packed.get("schema") not in {
                PACKED_SITE_NETLIST_SCHEMA, OPENPARF_ATOMIC_SOURCE_SCHEMA,
            }
            or source_packed.get("status") != "pass"
            or source_packed.get("top") != selected_top
        ):
            raise ValidationError(
                "OpenPARF atomic source PackedSiteNetlist identity is invalid"
            )
        source_clusters = source_packed.get("clusters")
        source_cascades = source_packed.get("cascade_chains")
        if not isinstance(source_clusters, Sequence) or isinstance(
            source_clusters, (str, bytes)
        ) or not isinstance(source_cascades, Sequence) or isinstance(
            source_cascades, (str, bytes)
        ):
            raise ValidationError(
                "OpenPARF atomic source PackedSiteNetlist is invalid"
            )
        if source_ownership_validation is not None:
            source_owner_count = expected_count
        else:
            indexed_source_assignments = getattr(
                source_packed, "iter_source_assignments", None
            )
            if callable(indexed_source_assignments):
                for instance, source_cluster_id, source_cell_type in (
                    indexed_source_assignments()
                ):
                    if (
                        not isinstance(instance, str)
                        or not isinstance(source_cluster_id, str)
                        or not isinstance(source_cell_type, str)
                        or source_cell_type in CONSTANT_TYPES
                        or (source_owner is not None and instance in source_owner)
                    ):
                        raise ValidationError(
                            "OpenPARF atomic source cell ownership is invalid"
                        )
                    if source_owner is not None:
                        source_owner[instance] = source_cluster_id
                    else:
                        assert source_assignment_sink is not None
                        source_assignment_sink(instance, source_cluster_id)
                    source_owner_count += 1
            else:
                for source_cluster in source_clusters:
                    if not isinstance(source_cluster, Mapping) or not isinstance(
                        source_cluster.get("id"), str
                    ):
                        raise ValidationError(
                            "OpenPARF atomic source cluster identity is invalid"
                        )
                    for assignment in source_cluster.get("assignments", []):
                        if not isinstance(assignment, Mapping):
                            raise ValidationError(
                                "OpenPARF atomic source assignment is invalid"
                            )
                        instance = assignment.get("instance")
                        if (
                            not isinstance(instance, str)
                            or (
                                source_owner is not None
                                and instance in source_owner
                            )
                        ):
                            raise ValidationError(
                                "OpenPARF atomic source cell ownership is invalid"
                            )
                        source_cell = cells.get(instance)
                        if (
                            not isinstance(source_cell, Mapping)
                            or source_cell.get("type") in CONSTANT_TYPES
                        ):
                            raise ValidationError(
                                "OpenPARF atomic source cell ownership is invalid"
                            )
                        if source_owner is not None:
                            source_owner[instance] = source_cluster["id"]
                        else:
                            assert source_assignment_sink is not None
                            source_assignment_sink(instance, source_cluster["id"])
                        source_owner_count += 1
        if source_owner_count != expected_count:
            raise ValidationError(
                "OpenPARF atomic source cell coverage is incomplete"
            )
        cascades = [dict(item) for item in source_cascades]
    clusters = (
        certificate.get("clusters")
        if certificate_clusters is None else certificate_clusters
    )
    if clusters is None or isinstance(clusters, (str, bytes, Mapping)):
        raise ValidationError("OpenPARF atomic placement clusters are invalid")

    owners: Optional[set[str]] = (
        None if sink_enforces_assignment_uniqueness else set()
    )
    assignment_count = 0
    site_names: Optional[set[str]] = (
        None if sink_enforces_assignment_uniqueness else set()
    )
    hard_counts: Counter = Counter()
    kind_counts: Counter = Counter()
    site_type_counts: Counter = Counter()
    occupied_local: Counter = Counter()
    cluster_count = 0
    carry8_macros = 0
    lut_count = 0
    ff_count = 0
    for index, entry in enumerate(clusters):
        context = f"atomic.clusters[{index}]"
        if not isinstance(entry, Mapping):
            raise ValidationError(f"{context}: expected an object")
        site_name = entry.get("site")
        site = architecture.site_named(site_name) if isinstance(site_name, str) else None
        if site is None or (
            site_names is not None and site_name in site_names
        ):
            raise ValidationError(f"{context}: unknown or duplicate physical site")
        if site_names is not None:
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
            if owners is not None and name in owners:
                raise ValidationError(f"cell {name!r} is assigned more than once")
            if owners is not None:
                owners.add(name)
            assignment_count += 1
            source_cluster = assignment.get("source_cluster")
            if (
                source_packed is not None
                and source_owner is not None
                and source_owner.get(name) != source_cluster
            ):
                raise ValidationError(
                    f"{assignment_context}: source cluster identity is invalid"
                )
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
                if cell_type not in {"RAMB18E2", "RAMB36E2"}:
                    raise ValidationError(
                        f"{assignment_context}: placement mode is invalid"
                    )
            physical_site = assignment.get("physical_site")
            if not isinstance(physical_site, str) or not physical_site:
                raise ValidationError(
                    f"{assignment_context}: physical site is invalid"
                )
            if cell_type not in {"RAMB18E2", "RAMB36E2"} and physical_site != site_name:
                raise ValidationError(
                    f"{assignment_context}: physical site disagrees with anchor"
                )
            bram_group = assignment.get("bram_tile_group")
            if cell_type in {"RAMB18E2", "RAMB36E2"}:
                if bram_group is None:
                    if native_bram_groups is not None:
                        raise ValidationError(
                            f"{assignment_context}: BRAM tile group is missing"
                        )
                else:
                    if (
                        not isinstance(bram_group, Mapping)
                        or set(bram_group) != {"anchor", "claims", "role", "tile"}
                        or bram_group.get("anchor") != site_name
                        or not isinstance(bram_group.get("tile"), str)
                    ):
                        raise ValidationError(
                            f"{assignment_context}: BRAM tile group is invalid"
                        )
                    role = (
                        "lower"
                        if cell_type == "RAMB18E2" and bel_name == "RAMB18E2_L"
                        else "upper"
                        if cell_type == "RAMB18E2" and bel_name == "RAMB18E2_U"
                        else "whole"
                        if cell_type == "RAMB36E2" and bel_name == "RAMB36E2"
                        else None
                    )
                    native_group = (
                        native_bram_groups.get(site_name)
                        if native_bram_groups is not None else None
                    )
                    native_view = (
                        native_group.get(role)
                        if native_group is not None and role is not None else None
                    )
                    expected_claims = (
                        [f"bram:{bram_group['tile']}:{role}"]
                        if role in {"lower", "upper"}
                        else [
                            f"bram:{bram_group['tile']}:lower",
                            f"bram:{bram_group['tile']}:upper",
                        ]
                        if role == "whole" else None
                    )
                    if (
                        role is None
                        or bram_group.get("role") != role
                        or bram_group.get("claims") != expected_claims
                        or not isinstance(native_view, Mapping)
                        or native_group.get("tile") != bram_group.get("tile")
                        or native_view.get("site") != physical_site
                        or native_view.get("bel") != bel_name
                        or native_view.get("site_type")
                        != assignment.get("placement_mode")
                    ):
                        raise ValidationError(
                            f"{assignment_context}: BRAM tile group disagrees "
                            "with native constraints"
                        )
            elif bram_group is not None:
                raise ValidationError(
                    f"{assignment_context}: non-BRAM assignment has a BRAM tile group"
                )
            normalized = {
                "instance": name, "cell_type": cell_type, "bel": bel_name,
                "_placement_mode": assignment["placement_mode"],
                "_physical_site": assignment["physical_site"],
                "_placement_ordinal": assignment_index,
                "_source_cluster": (
                    source_cluster if source_packed is not None and source_ownership_validation is None else None
                ),
            }
            if cell_type in (
                LUT_TYPES | {DUAL_OUTPUT_LUT_TYPE, "CARRY8"}
                | FF_TYPES | MUX_TYPES
            ):
                slice_assignments.append(normalized)
                if cell_type in LUT_TYPES | {DUAL_OUTPUT_LUT_TYPE}:
                    lut_count += 1
                elif cell_type in FF_TYPES:
                    ff_count += 1
            elif cell_type in _SUPPORTED_HARD_TYPES:
                hard_assignments.append(normalized)
                hard_counts[cell_type] += 1
            else:
                raise ValidationError(f"{assignment_context}: unsupported primitive")
        if slice_assignments and hard_assignments:
            raise ValidationError(f"{context}: mixes slice and hard resources")
        control_sets = []
        if hard_assignments:
            hard_types = {item["cell_type"] for item in hard_assignments}
            if hard_types == {"RAMB18E2"}:
                bels = {item["bel"] for item in hard_assignments}
                if (
                    len(hard_assignments) > 2
                    or len(bels) != len(hard_assignments)
                    or not bels.issubset({"RAMB18E2_L", "RAMB18E2_U"})
                ):
                    raise ValidationError(
                        f"{context}: RAMB18E2 shared-tile assignment is invalid"
                    )
            elif len(assignments) != 1:
                raise ValidationError(f"{context}: hard-resource site is not singleton")
            kind = "hard"
            control_set = None
        else:
            if sum(
                item["cell_type"] in LUT_TYPES | {DUAL_OUTPUT_LUT_TYPE}
                for item in slice_assignments
            ) > 8:
                raise ValidationError(f"{context}: exceeds conservative LUT capacity")
            ff_names = [
                item["instance"] for item in slice_assignments
                if item["cell_type"] in FF_TYPES
            ]
            if len(ff_names) > 16:
                raise ValidationError(f"{context}: exceeds physical FF capacity")
            control_sets = _slice_ff_control_sets(cells, slice_assignments)
            carry_counts = Counter(
                item["cell_type"] for item in slice_assignments
                if item["cell_type"] in {"CARRY8", DUAL_OUTPUT_LUT_TYPE}
            )
            if carry_counts:
                if (
                    len(slice_assignments) != 9
                    or carry_counts
                    != Counter({"CARRY8": 1, DUAL_OUTPUT_LUT_TYPE: 8})
                    or {item["bel"] for item in slice_assignments}
                    != {"CARRY8", *{f"{letter}6LUT" for letter in "ABCDEFGH"}}
                ):
                    raise ValidationError(
                        f"{context}: invalid exclusive CARRY8/LUT6_2 full-slice macro"
                    )
                kind = "carry"
            else:
                kind = "slice"
            control_set = control_sets[0] if len(control_sets) == 1 else None
        modes = sorted({
            assignment["placement_mode"] for assignment in assignments
        })
        physical_cluster = {
            "id": entry["cluster"], "kind": kind,
            "site_templates": modes, "control_set": control_set,
            "assignments": sorted(
                [*slice_assignments, *hard_assignments],
                key=lambda item: item["instance"],
            ),
            "_placement": {
                "site": site_name, "site_type": site["type"],
                "x": site["x"], "y": site["y"], "fixed": False,
                "physical_region": site.get("physical_region"),
            },
        }
        if len(control_sets) > 1:
            physical_cluster["control_sets"] = control_sets
        if hard_assignments and {
            item["cell_type"] for item in hard_assignments
        } == {"RAMB18E2"}:
            physical_cluster["site_mode"] = (
                f"RAMB18E2x{len(hard_assignments)}"
            )
        cluster_count += 1
        kind_counts[kind] += 1
        site_type_counts[site["type"]] += 1
        local_key = _clock_region_key(site)
        if local_key is not None:
            occupied_local[local_key] += 1
        if kind == "carry":
            carry8_macros += 1
        physical_cluster_sink(physical_cluster)
    actual_count = len(owners) if owners is not None else assignment_count
    if actual_count != expected_count:
        missing = (
            sorted(
                name
                for name, cell in cells.items()
                if isinstance(cell, Mapping)
                and cell.get("type") not in CONSTANT_TYPES
                and name not in owners
            )
            if owners is not None else ["@streamed-coverage-gap"]
        )
        extra: list[str] = []
        raise ValidationError(
            f"OpenPARF atomic placement coverage is incomplete; missing={missing}, extra={extra}"
        )
    expected_summary = {
        "atoms": expected_count, "occupied_sites": cluster_count,
        "luts": lut_count, "ffs": ff_count,
        "hard_resources": dict(sorted(hard_counts.items())),
    }
    native_hardblock_edges = sum(
        max(0, len(chain.get("instances", [])) - 1)
        for chain in cascades
        if isinstance(chain, Mapping) and chain.get("cell_type") != "CARRY8"
    )
    native_carry_edges = sum(
        max(0, len(chain.get("instances", [])) - 1)
        for chain in cascades
        if isinstance(chain, Mapping) and chain.get("cell_type") == "CARRY8"
    )
    if carry8_macros:
        expected_summary["carry8_macros"] = carry8_macros
    if native_hardblock_edges:
        expected_summary["native_hardblock_edges"] = native_hardblock_edges
    elif certificate.get("summary", {}).get("native_hardblock_edges"):
        raise ValidationError(
            "OpenPARF atomic cascade certificate requires its source packing"
        )
    if native_carry_edges:
        expected_summary["native_carry_edges"] = native_carry_edges
    elif certificate.get("summary", {}).get("native_carry_edges"):
        raise ValidationError(
            "OpenPARF atomic carry certificate requires its source packing"
        )
    if certificate.get("summary") != expected_summary:
        raise ValidationError("OpenPARF atomic placement summary is invalid")
    return (
        selected_top, cells, constants, cascades,
        {
            "clusters": cluster_count,
            "cluster_kinds": dict(sorted(kind_counts.items())),
            "site_types": dict(sorted(site_type_counts.items())),
            "occupied_local": occupied_local,
        },
    )


def _validate_streamed_physical_handoff(
    path: Path,
    *,
    handoff_sha: str,
    mapped_sha: str,
    selected_top: str,
    cells: Mapping[str, Any],
    architecture_path: Path,
    architecture: ArchitectureDB,
    packed: Mapping[str, Any],
    placement: Mapping[str, Any],
    constraints_path: Optional[Path],
    native: Optional[Mapping[str, Any]],
    mapped_index_path: Optional[Path] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Independently validate typed rows with bounded per-site state.

    Uniqueness is owned by SQLite constraints.  Coverage follows from those
    constraints plus an exact row count and a second mapped-cell/type pass.
    Only MUX/CARRY sites and explicit cascade paths are retained because their
    topology cannot be checked from one row in isolation.
    """

    if (
        packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA
        or packed.get("status") != "pass"
        or packed.get("top") != selected_top
        or packed.get("source", {}).get("mapped_json_sha256") != mapped_sha
    ):
        raise ValidationError("PackedSiteNetlist header is invalid")
    if (
        placement.get("schema") != XILINX_PLACEMENT_SCHEMA
        or placement.get("status") != "pass"
        or placement.get("part") != architecture.part
        or placement.get("provider") != XILINX_OPENPARF_ATOMIC_BRIDGE_PROVIDER
    ):
        raise ValidationError("Xilinx placement identity is invalid")
    expected_policy = {
        "clock_region_site_utilization_limit": XILINX_ROUTE_A_SITE_UTILIZATION_LIMIT,
        "capacity_rounding": "ceil-with-one-site-minimum",
        "packing": "native-openparf-atomic-site-groups-v1",
        "placement_certificate": OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
        "site_pin_legalization": "ultrascaleplus-slice-site-pin-legalizer-v1",
    }
    if placement.get("policy") != expected_policy:
        raise ValidationError("Xilinx placement routability policy is invalid")
    placement_source = placement.get("source", {})
    if (
        placement_source.get("packed_sha256") != handoff_sha
        or placement_source.get("architecture_sha256")
        != file_sha256(architecture_path)
        or placement_source.get("constraints_sha256")
        != (file_sha256(constraints_path) if constraints_path is not None else None)
    ):
        raise ValidationError("Xilinx placement source identity is invalid")

    native_population = None
    if mapped_index_path is not None:
        from .physical_cells_native import validate_native_physical_cells

        native_population = validate_native_physical_cells(
            mapped_index_path,
            path,
            mapped_sha256=mapped_sha,
            top=selected_top,
        )
    types_by_id = getattr(cells, "types_by_id", None)
    expected_constants = (
        [name for name, kind in types_by_id() if kind in CONSTANT_TYPES]
        if callable(types_by_id)
        else sorted(
            name
            for name, cell in cells.items()
            if isinstance(cell, Mapping) and cell.get("type") in CONSTANT_TYPES
        )
    )
    constants = packed.get("unplaced_constants")
    if constants != expected_constants:
        raise ValidationError("PackedSiteNetlist constants are invalid")
    expected_cells = len(cells) - len(expected_constants)
    expected_cascades = _derive_cascade_chains(cells)
    cascades = packed.get("cascade_chains")
    if cascades != expected_cascades:
        raise ValidationError(
            "PackedSiteNetlist dedicated-cascade certificate is invalid"
        )

    # Stage optional cluster constraints in a disk-backed SQLite temp table.
    # Iteration below consumes them as an ordered merge; no cluster-sized
    # Python map/set is created.
    constraint_connection: Optional[sqlite3.Connection] = None
    constraint_path: Optional[Path] = None
    global_constraint: Dict[str, Any] = {}
    constraint_row: Optional[sqlite3.Row] = None
    constraint_rows: Optional[Iterator[sqlite3.Row]] = None
    if constraints_path is not None:
        value = read_json(constraints_path)
        if not isinstance(value, dict) or value.get("schema") != XILINX_CONSTRAINTS_SCHEMA:
            raise ValidationError("Xilinx placement constraints header is invalid")
        global_entry = value.get("global")
        if global_entry is not None:
            allowed = global_entry.get("allowed_slrs") if isinstance(global_entry, dict) else None
            if (
                set(global_entry or {}) != {"allowed_slrs"}
                or not isinstance(allowed, list) or not allowed
                or any(not isinstance(item, str) or not item for item in allowed)
                or len(set(allowed)) != len(allowed)
            ):
                raise ValidationError("Xilinx placement global constraint is invalid")
            global_constraint = {"allowed_slrs": tuple(sorted(allowed))}
        entries = value.get("clusters", [])
        if not isinstance(entries, list):
            raise ValidationError("Xilinx placement constraints clusters are invalid")
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.constraints.", suffix=".tmp", dir=path.parent
        )
        os.close(descriptor)
        constraint_path = Path(name)
        try:
            constraint_connection = sqlite3.connect(constraint_path)
            constraint_connection.row_factory = sqlite3.Row
            constraint_connection.execute(
                "CREATE TABLE contract (cluster_id TEXT PRIMARY KEY, value_json TEXT NOT NULL) WITHOUT ROWID"
            )
            for index, entry in enumerate(entries):
                context = f"constraints.clusters[{index}]"
                if not isinstance(entry, dict):
                    raise ValidationError(f"{context}: expected an object")
                cluster_id = entry.get("cluster")
                if not isinstance(cluster_id, str) or not cluster_id:
                    raise ValidationError(f"{context}.cluster: expected a non-empty string")
                contract = {}
                for key in ("site", "slr", "clock_region"):
                    item = entry.get(key)
                    if item is not None:
                        if not isinstance(item, str) or not item:
                            raise ValidationError(f"{context}.{key}: expected a non-empty string")
                        contract[key] = item
                if not contract:
                    raise ValidationError(f"{context}: empty constraint")
                try:
                    constraint_connection.execute(
                        "INSERT INTO contract VALUES (?, ?)",
                        (cluster_id, json.dumps(contract, sort_keys=True)),
                    )
                except sqlite3.IntegrityError as error:
                    raise ValidationError(f"{context}: duplicate cluster {cluster_id!r}") from error
            constraint_connection.commit()
            constraint_rows = iter(constraint_connection.execute(
                "SELECT * FROM contract ORDER BY cluster_id"
            ))
            constraint_row = next(constraint_rows, None)
        except BaseException:
            if constraint_connection is not None:
                constraint_connection.close()
            constraint_path.unlink(missing_ok=True)
            raise

    contracts, _sites_by_template = _template_contracts(architecture)
    native_bram_groups = {
        group["anchor"]: group
        for group in (native or {}).get("payload", {}).get("bram_tile_groups", [])
    }
    packed_clusters = packed.get("clusters")
    placed_clusters = placement.get("clusters")
    if (
        not isinstance(packed_clusters, Sequence)
        or isinstance(packed_clusters, (str, bytes))
        or not isinstance(placed_clusters, Sequence)
        or isinstance(placed_clusters, (str, bytes))
        or len(packed_clusters) != len(placed_clusters)
    ):
        raise ValidationError("Xilinx physical handoff clusters are invalid")

    assignment_count = 0
    cluster_count = 0
    local_usage: Counter = Counter()
    special_clusters: list[Dict[str, Any]] = []
    try:
        for index, (cluster, placed) in enumerate(
            iter_xilinx_physical_handoff_cluster_pairs(path)
        ):
            context = f"physical_handoff.clusters[{index}]"
            if not isinstance(cluster, Mapping) or not isinstance(placed, Mapping):
                raise ValidationError(f"{context}: expected objects")
            cluster_id = cluster.get("id")
            if placed.get("cluster") != cluster_id:
                raise ValidationError(f"{context}: packing/placement identity disagrees")
            site_name = placed.get("site")
            site = architecture.site_named(site_name) if isinstance(site_name, str) else None
            if site is None:
                raise ValidationError(f"{context}: unknown site")
            if (
                placed.get("site_type") != site.get("type")
                or (placed.get("x"), placed.get("y")) != (site.get("x"), site.get("y"))
            ):
                raise ValidationError(f"{context}: site metadata disagrees")

            constraint = dict(global_constraint)
            while constraint_row is not None and constraint_row["cluster_id"] < cluster_id:
                raise ValidationError(
                    f"placement constraint references unknown cluster {constraint_row['cluster_id']!r}"
                )
            if constraint_row is not None and constraint_row["cluster_id"] == cluster_id:
                constraint.update(json.loads(constraint_row["value_json"]))
                assert constraint_rows is not None
                constraint_row = next(constraint_rows, None)
            if not _site_satisfies_constraint(site, constraint):
                raise ValidationError(f"{context}: placement constraint is violated")

            local_key = _clock_region_site_key(site)
            if local_key is not None:
                local_usage[local_key] += 1
            assignments = cluster.get("assignments")
            actual_assignments = placed.get("assignments")
            if not isinstance(assignments, list) or not isinstance(actual_assignments, list):
                raise ValidationError(f"{context}: assignments are invalid")
            instances: list[str] = []
            used_bels: set[str] = set()
            for assignment in assignments:
                name = assignment.get("instance") if isinstance(assignment, Mapping) else None
                cell_type = assignment.get("cell_type") if isinstance(assignment, Mapping) else None
                if native_population is None:
                    cell = cells.get(name)
                    if not isinstance(cell, Mapping) or cell.get("type") != cell_type:
                        raise ValidationError(
                            "PackedSiteNetlist assignment does not match source"
                        )
                bel = assignment.get("bel")
                if not isinstance(bel, str) or not bel or bel in used_bels:
                    raise ValidationError("PackedSiteNetlist reuses a BEL")
                used_bels.add(bel)
                instances.append(name)
                assignment_count += 1

            kind = cluster.get("kind")
            if kind == "slice":
                if sum(item["cell_type"] in LUT_TYPES for item in assignments) > len(
                    SLICE_LUT_BELS
                ) or sum(item["cell_type"] in FF_TYPES for item in assignments) > len(
                    SLICE_FF_BELS
                ):
                    raise ValidationError("slice cluster exceeds physical capacity")
                # The site owns at most sixteen FFs. Read each once; the
                # independent semantic checker consumes only this local scope.
                control_cells = {
                    item["instance"]: cells[item["instance"]]
                    for item in assignments
                    if item["cell_type"] in FF_TYPES
                }
                control_sets = _slice_ff_control_sets(control_cells, assignments)
                _validate_declared_slice_control_sets(cluster, control_sets)
            elif kind == "carry":
                carry = [
                    item["instance"]
                    for item in assignments
                    if item["cell_type"] == "CARRY8"
                ]
                adapters = [
                    item["instance"]
                    for item in assignments
                    if item["cell_type"] == DUAL_OUTPUT_LUT_TYPE
                ]
                if len(carry) != 1 or len(adapters) != 8 or len(instances) != 9:
                    raise ValidationError("carry cluster is invalid")
            elif kind == "hard":
                cell_types = [item["cell_type"] for item in assignments]
                if set(cell_types) == {"RAMB18E2"}:
                    if (
                        not 1 <= len(instances) <= 2
                        or set(used_bels) - {"RAMB18E2_L", "RAMB18E2_U"}
                        or cluster.get("site_mode") != f"RAMB18E2x{len(instances)}"
                    ):
                        raise ValidationError("RAMB18E2 shared-site cluster is invalid")
                elif len(instances) != 1 or cell_types[0] not in HARD_BINDINGS:
                    raise ValidationError("hard cluster is invalid")
            else:
                raise ValidationError(f"unknown packing cluster kind {kind!r}")

            base = site.get("template", f"@site:{site_name}")
            resolved = _resolve_cluster_bels(cluster, contracts[base])
            if resolved is not None:
                explicit_sites = {
                    assignment.get("instance"): assignment.get("site")
                    for assignment in actual_assignments
                    if isinstance(assignment, Mapping)
                }
                resolved = _materialize_assignment_sites(
                    site_name,
                    [
                        {**assignment, **({"site": explicit_sites[assignment["instance"]]}
                         if assignment["instance"] in explicit_sites else {})}
                        for assignment in resolved
                    ],
                    native_bram_groups=(native_bram_groups or None),
                )
            if resolved is None or actual_assignments != resolved:
                raise ValidationError(f"{context}: exact BEL assignment is invalid")
            if kind == "carry" or any(
                assignment.get("cell_type") in MUX_TYPES
                for assignment in assignments
            ):
                special_clusters.append(dict(cluster))
            cluster_count += 1
        if constraint_row is not None:
            raise ValidationError(
                f"placement constraint references unknown cluster {constraint_row['cluster_id']!r}"
            )
    finally:
        if constraint_connection is not None:
            constraint_connection.close()
        if constraint_path is not None:
            constraint_path.unlink(missing_ok=True)

    if assignment_count != expected_cells:
        raise ValidationError("PackedSiteNetlist cell ownership is incomplete")
    required_bits = {
        bit
        for cluster in special_clusters
        for assignment in cluster.get("assignments", [])
        for port in (
            ("I0", "I1", "S") if assignment.get("cell_type") in MUX_TYPES
            else ("DI", "S") if assignment.get("cell_type") == "CARRY8"
            else ("D",) if assignment.get("cell_type") in FF_TYPES else ()
        )
        for bit in _bits(cells[assignment["instance"]], port)
        if isinstance(bit, int)
    }
    # The native check covers every mapped integer output bit directly in its
    # typed owner. Explicit JSON inputs use a disk-backed uniqueness relation.
    # The Python map contains only the MUX/CARRY topology bits below.
    if native_population is not None:
        output_drivers = getattr(cells, "output_drivers", None)
        if not callable(output_drivers):
            raise ValidationError("typed mapped driver lookup is unavailable")
        drivers = output_drivers(required_bits)
    else:
        descriptor, driver_database_name = tempfile.mkstemp(
            prefix=f".{path.name}.drivers.", suffix=".tmp", dir=path.parent
        )
        os.close(descriptor)
        driver_database_path = Path(driver_database_name)
        driver_database: Optional[sqlite3.Connection] = None
        drivers: Dict[Any, str] = {}
        try:
            driver_database = sqlite3.connect(driver_database_path)
            driver_database.execute("PRAGMA journal_mode=OFF")
            driver_database.execute("PRAGMA synchronous=OFF")
            driver_database.execute(
                "CREATE TABLE driver (bit INTEGER PRIMARY KEY) WITHOUT ROWID"
            )
            rows_since_commit = 0
            items_by_id = getattr(cells, "items_by_id", None)
            cell_rows = items_by_id() if callable(items_by_id) else cells.items()
            for name, cell in cell_rows:
                directions = cell.get("port_directions")
                connections = cell.get("connections")
                if not isinstance(directions, Mapping) or not isinstance(
                    connections, Mapping
                ):
                    raise ValidationError(f"cell {name!r} lacks explicit port metadata")
                for port, direction in directions.items():
                    if direction != "output":
                        continue
                    bits = connections.get(port, [])
                    if not isinstance(bits, list):
                        raise ValidationError(
                            f"cell {name!r} output {port!r} is invalid"
                        )
                    for bit in bits:
                        if not isinstance(bit, int):
                            continue
                        try:
                            driver_database.execute(
                                "INSERT INTO driver(bit) VALUES (?)", (bit,)
                            )
                        except sqlite3.IntegrityError as error:
                            raise ValidationError(
                                f"mapped net bit {bit} has multiple drivers"
                            ) from error
                        if bit in required_bits:
                            drivers[bit] = name
                        rows_since_commit += 1
                        if rows_since_commit >= 65_536:
                            driver_database.commit()
                            rows_since_commit = 0
            driver_database.commit()
        except sqlite3.Error as error:
            raise ValidationError(
                "failed to validate mapped net driver uniqueness"
            ) from error
        finally:
            if driver_database is not None:
                driver_database.close()
            driver_database_path.unlink(missing_ok=True)
    if _slice_site_pin_conflicts(cells, special_clusters, drivers):
        raise ValidationError("slice external site pin assignment is invalid")
    for cluster in special_clusters:
        assignments = cluster.get("assignments", [])
        bels = {item["instance"]: item.get("bel") for item in assignments}
        for name in bels:
            cell_type = cells[name].get("type")
            if cell_type == "MUXF7":
                expected = {
                    "F7MUX_AB": {"A6LUT", "B6LUT"}, "F7MUX_CD": {"C6LUT", "D6LUT"},
                    "F7MUX_EF": {"E6LUT", "F6LUT"}, "F7MUX_GH": {"G6LUT", "H6LUT"},
                }.get(bels[name])
                actual = {bels.get(_mux_input_driver(cells, drivers, name, port)) for port in ("I0", "I1")}
                if expected is None or actual != expected:
                    raise ValidationError("MUXF7 packing topology is invalid")
            elif cell_type == "MUXF8":
                expected = {
                    "F8MUX_BOT": {"F7MUX_AB", "F7MUX_CD"},
                    "F8MUX_TOP": {"F7MUX_EF", "F7MUX_GH"},
                }.get(bels[name])
                actual = {bels.get(_mux_input_driver(cells, drivers, name, port)) for port in ("I0", "I1")}
                if expected is None or actual != expected:
                    raise ValidationError("MUXF8 packing topology is invalid")
            elif cell_type == "MUXF9":
                actual = {bels.get(_mux_input_driver(cells, drivers, name, port)) for port in ("I0", "I1")}
                if actual != {"F8MUX_BOT", "F8MUX_TOP"}:
                    raise ValidationError("MUXF9 packing topology is invalid")
            elif cell_type == "CARRY8":
                adapters = {item["instance"] for item in assignments if item.get("cell_type") == DUAL_OUTPUT_LUT_TYPE}
                for bit_index, letter in enumerate("ABCDEFGH"):
                    di = drivers.get(_bits(cells[name], "DI")[bit_index])
                    select = drivers.get(_bits(cells[name], "S")[bit_index])
                    if di is None or di != select or di not in adapters or bels.get(di) != f"{letter}6LUT":
                        raise ValidationError("CARRY8 LUT6_2 packing topology is invalid")

    # Cascade queries touch only the explicit cascade subgraph.
    database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    try:
        native_edges: Dict[str, set[Tuple[str, str]]] = {}
        if native is not None:
            for family in native.get("payload", {}).get("dedicated_adjacency", []):
                native_edges[family["kind"]] = {
                    edge for chain in family.get("chains", [])
                    for edge in zip(chain, chain[1:])
                }
        kind_by_type = {"CARRY8": "CARRY_NEXT", "DSP48E2": "DSP_CASCADE", "RAMB36E2": "BRAM_CASCADE", "URAM288": "URAM_CASCADE"}
        for chain in cascades:
            cluster_path: list[Tuple[str, str]] = []
            for instance in chain.get("instances", []):
                row = database.execute(
                    "SELECT c.cluster_id, c.site FROM assignment a JOIN cluster c "
                    "ON c.ordinal=a.cluster_ordinal WHERE a.instance=?", (instance,)
                ).fetchone()
                if row is None:
                    raise ValidationError("cascade certificate references an unknown cell")
                item = (row["cluster_id"], row["site"])
                if not cluster_path or cluster_path[-1] != item:
                    cluster_path.append(item)
            if len(cluster_path) <= 1:
                continue
            if len({cluster_id for cluster_id, _site in cluster_path}) != len(cluster_path):
                raise ValidationError("cascade chain revisits a packed cluster")
            slrs = {
                architecture.site_named(site).get("physical_region", {}).get("slr")
                for _cluster, site in cluster_path
            }
            if len(slrs) != 1 or None in slrs:
                raise ValidationError("dedicated cascade placement crosses an SLR boundary")
            kind = kind_by_type.get(chain.get("cell_type"))
            edges = native_edges.get(kind) if kind is not None else None
            if edges is None or any(
                edge not in edges for edge in zip(
                    [site for _cluster, site in cluster_path],
                    [site for _cluster, site in cluster_path][1:],
                )
            ):
                raise ValidationError("OpenPARF cascade placement violates source-sealed native adjacency")
    finally:
        database.close()

    expected_local = _local_utilization_summary(architecture, local_usage)
    if any(placement.get("summary", {}).get(key) != value for key, value in expected_local.items()):
        raise ValidationError("Xilinx placement local-utilization summary is invalid")
    digest = handoff_sha
    return (
        {
            "status": "pass",
            "schema": "emuflow.packed-site-netlist-validation/v1",
            "cells": len(cells),
            "clusters": cluster_count,
            "architecture_checked": True,
            "packed_sha256": digest,
            **(
                {"source_population_validation": native_population}
                if native_population is not None
                else {}
            ),
        },
        {
            "status": "pass",
            "schema": "emuflow.xilinx-placement-validation/v1",
            "clusters": cluster_count,
            "cells": assignment_count,
            "cascade_chains": len(cascades),
            "placement_sha256": digest,
        },
    )


def materialize_xilinx_openparf_atomic_contract(
    mapped_path: Path,
    architecture_path: Path,
    atomic_placement_path: Path,
    physical_handoff_path: Path,
    *,
    top: Optional[str] = None,
    source_packed_path: Optional[Path] = None,
    source_packed_value: Optional[Mapping[str, Any]] = None,
    constraints_path: Optional[Path] = None,
    native_constraints_path: Optional[Path] = None,
    provider_manifest_path: Optional[Path] = None,
    mapped_value: Optional[Mapping[str, Any]] = None,
    mapped_sha256: Optional[str] = None,
    architecture: Optional[ArchitectureDB] = None,
    packed_value_sink: Optional[Dict[str, Any]] = None,
    placement_value_sink: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Publish one typed handoff; separate JSON output is retired."""

    started = time.perf_counter()
    sources = [mapped_path, architecture_path, atomic_placement_path]
    sources.extend(path for path in (
        source_packed_path, constraints_path, native_constraints_path,
        provider_manifest_path, getattr(mapped_value, "path", None),
        getattr(source_packed_value, "store_path", None),
    ) if path is not None)
    with XilinxPhysicalHandoffBuilder(physical_handoff_path, sources=sources) as builder:
        result = _materialize_typed_physical_handoff(
            mapped_path, architecture_path, atomic_placement_path,
            handoff_builder=builder,
            top=top,
            source_packed_path=source_packed_path,
            source_packed_value=source_packed_value,
            constraints_path=constraints_path,
            native_constraints_path=native_constraints_path,
            provider_manifest_path=provider_manifest_path,
            mapped_value=mapped_value,
            mapped_sha256=mapped_sha256,
            architecture=architecture,
            packed_value_sink=packed_value_sink,
            placement_value_sink=placement_value_sink,
        )
    result["runtime_breakdown"]["total_seconds"] = time.perf_counter() - started
    return result


def _materialize_typed_physical_handoff(
    mapped_path: Path,
    architecture_path: Path,
    atomic_placement_path: Path,
    *,
    handoff_builder: XilinxPhysicalHandoffBuilder,
    top: Optional[str] = None,
    source_packed_path: Optional[Path] = None,
    source_packed_value: Optional[Mapping[str, Any]] = None,
    constraints_path: Optional[Path] = None,
    native_constraints_path: Optional[Path] = None,
    provider_manifest_path: Optional[Path] = None,
    mapped_value: Optional[Mapping[str, Any]] = None,
    mapped_sha256: Optional[str] = None,
    architecture: Optional[ArchitectureDB] = None,
    packed_value_sink: Optional[Dict[str, Any]] = None,
    placement_value_sink: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate a typed placement certificate and publish one physical handoff.

    Packing and placement are lazy views of the same authoritative SQLite
    owner. Separate JSON outputs and inline placement certificates are retired.
    """

    bridge_started = time.perf_counter()
    physical_handoff_path = handoff_builder.path
    source_digests: Dict[Path, str] = {}

    def digest(path: Path) -> str:
        # The publisher binds these immutable sources and rechecks their
        # generations at rename. Reuse each strong hash within this operation.
        key = Path(path).absolute()
        if key not in source_digests:
            source_digests[key] = _sha256(key)
        return source_digests[key]

    certificate = read_json(atomic_placement_path)
    if not isinstance(certificate, Mapping):
        raise ValidationError("OpenPARF bridge inputs are invalid")
    if 'clusters' in certificate:
        raise ValidationError(
            "production OpenPARF bridge requires the typed placement-certificate "
            "store; inline legacy clusters are retired"
        )
    mapped = read_json(mapped_path) if mapped_value is None else mapped_value
    if not isinstance(mapped, Mapping):
        raise ValidationError("OpenPARF bridge inputs are invalid")
    if getattr(mapped, "schema", None) == "emuflow.xilinx-mapped-index/v2":
        mapped.require_source_unchanged()
        if is_xilinx_mapped_index(mapped_path) and not os.path.samefile(mapped_path, mapped.path):
            raise ValidationError("OpenPARF bridge borrowed mapped owner disagrees with input")
        handoff_builder.bind_store_sources(mapped.bound_store_sources())
    descriptor = certificate.get("cluster_storage")
    if isinstance(descriptor, Mapping):
        filename = descriptor.get("file")
        if isinstance(filename, str) and Path(filename).name == filename:
            handoff_builder.bind_sources([atomic_placement_path.with_name(filename)])
    architecture = (
        ArchitectureDB.load(architecture_path)
        if architecture is None else architecture
    )
    mapped_sha = mapped_sha256 or digest(mapped_path)
    source_packed: Optional[Mapping[str, Any]] = None
    if source_packed_path is not None:
        if source_packed_value is None:
            candidate = read_json(source_packed_path)
            if isinstance(candidate, Mapping) and candidate.get("schema") == OPENPARF_ATOMIC_SOURCE_STORAGE_SCHEMA:
                filename = candidate.get("artifact")
                if isinstance(filename, str) and Path(filename).name == filename:
                    handoff_builder.bind_sources([source_packed_path.with_name(filename)])
            loaded_source_packed = (
                read_xilinx_openparf_atomic_source(
                    source_packed_path,
                    mapped_path=mapped_path,
                    mapped_value=mapped,
                    mapped_sha256=mapped_sha,
                )
                if isinstance(candidate, Mapping)
                and candidate.get("schema")
                == OPENPARF_ATOMIC_SOURCE_STORAGE_SCHEMA
                else candidate
            )
        else:
            loaded_source_packed = source_packed_value
        if not isinstance(loaded_source_packed, Mapping):
            raise ValidationError(
                "OpenPARF atomic source PackedSiteNetlist is invalid"
            )
        source_identity = loaded_source_packed.get("source", {})
        if not isinstance(source_identity, Mapping) or (
            source_identity.get("mapped_json_sha256")
            if loaded_source_packed.get("schema") == PACKED_SITE_NETLIST_SCHEMA
            else source_identity.get("mapped_sha256")
        ) != mapped_sha:
            raise ValidationError(
                "OpenPARF atomic source PackedSiteNetlist seal is invalid"
            )
        source_packed = loaded_source_packed
        if isinstance(source_packed, XilinxOpenparfAtomicSourceView):
            source_packed.require_sources_unchanged()
            if read_json(source_packed_path) != source_packed.selector:
                raise ValidationError("OpenPARF bridge borrowed atomic source selector disagrees")
            handoff_builder.bind_store_sources(source_packed.bound_store_sources())
    source = certificate.get("source")
    if (
        not isinstance(source, Mapping)
        or source.get("mapped_sha256") != mapped_sha
        or source.get("architecture_sha256") != digest(architecture_path)
        or not isinstance(source.get("native_placement_sha256"), str)
        or not isinstance(source.get("name_map_sha256"), str)
    ):
        raise ValidationError("OpenPARF atomic placement source identity is invalid")
    if source_packed_path is not None and source.get('packed_sha256') != digest(source_packed_path):
        raise ValidationError(
            "OpenPARF atomic placement packed-source identity is invalid"
        )
    has_native_seal = any(
        key in source
        for key in ("native_constraints_sha256", "provider_manifest_sha256")
    )
    if has_native_seal:
        if native_constraints_path is None or provider_manifest_path is None:
            raise ValidationError(
                "OpenPARF native hardblock bridge requires its constraints "
                "and provider manifest"
            )
        certificate_source = certificate.get("source", {})
        if (
            certificate_source.get("native_constraints_sha256")
            != digest(native_constraints_path)
            or certificate_source.get("provider_manifest_sha256")
            != digest(provider_manifest_path)
        ):
            raise ValidationError(
                "OpenPARF native certificate source identity is invalid"
            )
    native = None
    native_bram_groups = None
    if has_native_seal:
        native, _native_report = load_xilinx_native_device_constraints(
            native_constraints_path,
            architecture_path=architecture_path,
            provider_manifest_path=provider_manifest_path,
        )
        native_bram_groups = {
            group["anchor"]: group
            for group in native["payload"].get("bram_tile_groups", [])
        }
    certificate_view = None
    try:
        certificate_view = open_xilinx_openparf_atomic_placement(
            atomic_placement_path,
            allow_legacy=False,
        )
        if dict(certificate_view) != dict(certificate):
            raise ValidationError(
                "OpenPARF atomic placement selector changed before leased read"
            )
    except BaseException:
        if certificate_view is not None:
            certificate_view.close()
        handoff_builder.close()
        raise
    assert certificate_view is not None
    handoff_builder.bind_store_sources(certificate_view.bound_store_sources())
    certificate_clusters = certificate_view.iter_clusters()
    legalization_clusters: list[Dict[str, Any]] = []

    def stream_physical_cluster(cluster: Mapping[str, Any]) -> None:
        handoff_builder.append_bridge_cluster(cluster)
        if cluster.get("kind") == "slice" and any(
            assignment.get("cell_type") in MUX_TYPES
            for assignment in cluster.get("assignments", [])
            if isinstance(assignment, Mapping)
        ):
            # Site-pin legalization is local to a bounded slice.  Retain only
            # sites that use a MUX select resource, not the complete physical
            # cluster/assignment tree.
            legalization_clusters.append(dict(cluster))

    try:
        validation_started = time.perf_counter()
        source_ownership_validation = None
        if (
            getattr(mapped, "schema", None) == "emuflow.xilinx-mapped-index/v2"
            and isinstance(source_packed, XilinxOpenparfAtomicSourceView)
            and is_xilinx_mapped_index(source_packed.mapped_path)
        ):
            from .physical_cells_native import validate_native_atomic_ownership
            mapped.require_source_unchanged()
            source_packed.require_sources_unchanged()
            certificate_view.require_source_unchanged()
            source_ownership_validation = validate_native_atomic_ownership(
                mapped.path, source_packed.store_path, certificate_view.store_path,
                mapped_sha256=mapped_sha,
                source_logical_sha256=source_packed.selector["logical_sha256"],
                certificate_logical_sha256=certificate_view.descriptor["logical_sha256"],
                top=_select_module(mapped, top)[0],
                scratch_parent=physical_handoff_path.parent,
            )
        (
            selected_top, cells, constants, cascades,
            certificate_statistics,
        ) = _validate_certificate(
            mapped, certificate, architecture, top, source_packed,
            certificate_clusters=certificate_clusters,
            native_bram_groups=native_bram_groups,
            physical_cluster_sink=stream_physical_cluster,
            sink_enforces_assignment_uniqueness=True,
            source_assignment_sink=(
                handoff_builder.append_source_assignment
                if source_packed is not None and source_ownership_validation is None
                else None
            ),
            source_ownership_validation=source_ownership_validation,
        )
        if source_packed is not None and source_ownership_validation is None:
            handoff_builder.validate_source_ownership(
                len(cells) - len(constants)
            )
        certificate_validation_seconds = (
            time.perf_counter() - validation_started
        )
        if cascades and not has_native_seal:
            raise ValidationError(
                "OpenPARF native hardblock bridge requires a source-sealed "
                "native placement certificate"
            )
        site_pin_legalization = legalize_xilinx_slice_site_pins(
            cells, legalization_clusters
        )
        handoff_builder.update_assignment_bels(legalization_clusters)
        legalization_clusters.clear()
        certificate_sha = digest(atomic_placement_path)
        architecture_sha = digest(architecture_path)
        constraints_sha = (
            digest(constraints_path)
            if constraints_path is not None else None
        )
        native_constraints_sha = (
            digest(native_constraints_path)
            if native_constraints_path is not None else None
        )
        provider_manifest_sha = (
            digest(provider_manifest_path)
            if provider_manifest_path is not None else None
        )
        source_packed_sha = (
            digest(source_packed_path)
            if source_packed_path is not None else None
        )
        cluster_count = int(certificate_statistics["clusters"])
        local_summary = _local_utilization_summary(
            architecture, certificate_statistics["occupied_local"]
        )
        cluster_storage = certificate.get("cluster_storage")
        certificate_database_bytes = (
            int(cluster_storage["bytes"])
            if isinstance(cluster_storage, Mapping)
            and isinstance(cluster_storage.get("bytes"), int)
            and not isinstance(cluster_storage.get("bytes"), bool)
            else 0
        )
    except BaseException:
        handoff_builder.close()
        raise
    finally:
        certificate_view.close()

    packed = {
        "schema": PACKED_SITE_NETLIST_SCHEMA,
        "status": "pass",
        "mapping_profile": XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
        "top": selected_top,
        "source": {
            "mapped_json_sha256": mapped_sha,
            "architecture_sha256": architecture_sha,
            "openparf_atomic_placement_sha256": certificate_sha,
            "source_packed_sha256": (
                source_packed_sha
            ),
        },
        "policy": {
            "provider": "openparf-native-atomic-site-group-bridge-v1",
            "source_grouping": "one-cluster-per-certified-physical-site",
            "repacking": site_pin_legalization["moved_ffs"] > 0,
            "site_pin_legalization": site_pin_legalization,
            "ff_control_set_model": (
                "ultrascaleplus-half-slice-cksr-ff-lane-ce-v1"
            ),
        },
        "clusters": (),
        "cascade_chains": cascades,
        "unplaced_constants": constants,
        "summary": {
            "cells": len(cells),
            "placed_cells": len(cells) - len(constants),
            "constant_cells": len(constants),
            "clusters": cluster_count,
            "cluster_kinds": certificate_statistics["cluster_kinds"],
            "cascade_chains": len(cascades),
            "cascade_links": sum(
                max(0, len(chain.get("instances", [])) - 1)
                for chain in cascades
            ),
        },
    }

    publication_started = time.perf_counter()
    publication_steps: Dict[str, float] = {}
    placement = {
            "schema": XILINX_PLACEMENT_SCHEMA, "status": "pass",
            "part": architecture.part,
            "provider": XILINX_OPENPARF_ATOMIC_BRIDGE_PROVIDER,
            "policy": {
                "clock_region_site_utilization_limit": XILINX_ROUTE_A_SITE_UTILIZATION_LIMIT,
                "capacity_rounding": "ceil-with-one-site-minimum",
                "packing": "native-openparf-atomic-site-groups-v1",
                "placement_certificate": OPENPARF_ATOMIC_PLACEMENT_SCHEMA,
                "site_pin_legalization": (
                    "ultrascaleplus-slice-site-pin-legalizer-v1"
                ),
            },
            "source": {
                "packed_sha256": None,
                "architecture_sha256": architecture_sha,
                "guidance_sha256": None,
                "constraints_sha256": constraints_sha,
                "openparf_atomic_placement_sha256": certificate_sha,
            },
            "clusters": (),
            "summary": {
                "clusters": cluster_count,
                "cells": len(cells) - len(constants),
                "fixed_clusters": 0, "cascade_chains": len(cascades),
                "mean_guidance_displacement": None,
                "max_guidance_displacement": None,
                "site_pin_moved_ffs": site_pin_legalization["moved_ffs"],
                "site_pin_repaired_sites": site_pin_legalization["sites"],
                **local_summary,
                "site_types": certificate_statistics["site_types"],
            },
        }
    if cascades or has_native_seal:
        assert native_constraints_path is not None
        assert provider_manifest_path is not None
        placement["source"].update({
            "native_constraints_sha256": native_constraints_sha,
            "provider_manifest_sha256": provider_manifest_sha,
        })

    try:
        step_started = time.perf_counter()
        handoff_builder.seal(
            packed, placement, cascades=cascades, constants=constants
        )
        publication_steps["physical_handoff_write_seconds"] = (
            time.perf_counter() - step_started
        )
        temporary_handoff = handoff_builder.temporary_path
        step_started = time.perf_counter()
        handoff_validation = validate_xilinx_physical_handoff(
            temporary_handoff
        )
        publication_steps["physical_handoff_validation_seconds"] = (
            time.perf_counter() - step_started
        )
        step_started = time.perf_counter()
        validation_packed, validation_placement = (
            open_xilinx_physical_handoff(
                temporary_handoff,
                validated_sha256=(
                    handoff_validation["physical_handoff_sha256"]
                ),
            )
        )
        publication_steps["physical_handoff_open_seconds"] = (
            time.perf_counter() - step_started
        )
        step_started = time.perf_counter()
        packing_validation, placement_validation = _validate_streamed_physical_handoff(
            temporary_handoff,
            handoff_sha=handoff_validation["physical_handoff_sha256"],
            mapped_sha=mapped_sha,
            selected_top=selected_top,
            cells=cells,
            architecture_path=architecture_path,
            architecture=architecture,
            packed=validation_packed,
            placement=validation_placement,
            constraints_path=constraints_path,
            native=native,
            mapped_index_path=(
                mapped.path
                if getattr(mapped, "schema", None) == "emuflow.xilinx-mapped-index/v2"
                else None
            ),
        )
        publication_steps["packing_validation_seconds"] = (
            time.perf_counter() - step_started
        )
        publication_steps["placement_validation_seconds"] = 0.0
        published_digest = handoff_validation[
            "physical_handoff_sha256"
        ]
        handoff_builder.publish()
        published_packed, published_placement = (
            open_xilinx_physical_handoff(
                physical_handoff_path,
                validated_sha256=published_digest,
            )
        )
    except BaseException:
        handoff_builder.close()
        raise
    handoff_builder.close()

    if packed_value_sink is not None:
        packed_value_sink.clear()
        packed_value_sink.update(published_packed)
    if placement_value_sink is not None:
        placement_value_sink.clear()
        placement_value_sink.update(published_placement)

    publication_validation_seconds = time.perf_counter() - publication_started
    return {
        "schema": OPENPARF_ATOMIC_BRIDGE_REPORT_SCHEMA,
        "status": "pass",
        "top": selected_top,
        "source": {
            "mapped_sha256": mapped_sha,
            "architecture_sha256": architecture_sha,
            "openparf_atomic_placement_sha256": certificate_sha,
            "source_packed_sha256": (source_packed_sha),
            "constraints_sha256": (constraints_sha),
        },
        "packed_sha256": published_digest,
        "placement_sha256": published_digest,
        "physical_handoff_sha256": published_digest,
        "cells": len(cells),
        "placed_cells": len(cells) - len(constants),
        "clusters": cluster_count,
        "site_pin_moved_ffs": site_pin_legalization["moved_ffs"],
        "site_pin_repaired_sites": site_pin_legalization["sites"],
        "cascade_chains": len(cascades),
        "validation": {
            "packing": packing_validation,
            **({"source_ownership": source_ownership_validation}
               if source_ownership_validation is not None else {}),
            "placement": placement_validation,
        },
        "runtime_breakdown": {
            "schema": "emuflow.openparf-bridge-runtime/v1",
            "certificate_validation_seconds": certificate_validation_seconds,
            "publication_and_validation_seconds": (publication_validation_seconds),
            "publication_steps": publication_steps,
            "total_seconds": time.perf_counter() - bridge_started,
            "rows": {
                "mapped_cells": len(cells),
                "physical_clusters": cluster_count,
                "placed_assignments": len(cells) - len(constants),
            },
            "bytes": {
                "placement_certificate": atomic_placement_path.stat().st_size,
                "placement_certificate_database": certificate_database_bytes,
                "packed_output": 0,
                "placement_output": 0,
                "physical_handoff": (physical_handoff_path.stat().st_size),
            },
            "dataflow": {
                "certificate": "streaming-sqlite",
                "bridge_contracts": ("incremental-typed-single-owner-dual-view-v1"),
                "certificate_to_handoff": ("validated-cluster-direct-typed-row"),
                "legalization_working_set": ("mux-slice-clusters-only"),
                "certificate_assignment_ownership": (
                    "native-source-certificate-ordered-merge-v1"
                    if source_ownership_validation is not None
                    else "typed-unique-index-and-count"
                ),
                "handoff_publication": ("validate-temporary-before-atomic-replace"),
                "physical_contract_validation": (
                    "native-typed-population-with-local-site-semantics-v1"
                    if "source_population_validation" in packing_validation
                    else "sqlite-single-scan-ordered-merge-bounded-special-subgraph"
                ),
                "source_packed": (
                    "in-process-view"
                    if source_packed_value is not None
                    else "serialized-compatibility"
                ),
                "published_values": ("lazy-typed-sqlite-views"),
                "validation_replay": False,
            },
        },
        "runtime_validation": certificate["runtime_validation"],
    }
