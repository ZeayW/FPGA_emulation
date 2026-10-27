"""Bounded packing/placement test oracles; never installed in the runtime.

These helpers construct small legality fixtures for the independent production
validators. They do not launch OpenPARF, RapidWright or any complete flow.
"""
from __future__ import annotations
import json
import math
from bisect import bisect_left
import re
from emuflow.physical.xilinx_native_device_constraints import load_xilinx_native_device_constraints
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from emuflow.platform.architecture import ArchitectureDB
from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256, read_json, write_json
from emuflow.physical.xilinx_primitives import XILINX_ULTRASCALEPLUS_OPEN_PROFILE, audit_xilinx_mapped_json
from emuflow.physical.contracts import PACKED_SITE_NETLIST_SCHEMA
SLICE_LUT_BELS = tuple(f"{letter}6LUT" for letter in "ABCDEFGH")
SLICE_FF_BELS = tuple(
    bel for letter in "ABCDEFGH" for bel in (f"{letter}FF", f"{letter}FF2")
)
from emuflow.physical.xilinx_packing import FF_TYPES
from emuflow.physical.xilinx_packing import LUT_TYPES
from emuflow.physical.xilinx_packing import DUAL_OUTPUT_LUT_TYPE
from emuflow.physical.xilinx_packing import CONSTANT_TYPES
from emuflow.physical.xilinx_packing import MUX_TYPES
HARD_BINDINGS = {
    "DSP48E2": (["DSP48E2"], ["DSP_ALU"]),
    "RAMB36E2": (["RAMB36"], ["RAMB36E2"]),
    "URAM288": (["URAM288"], ["URAM_288K_INST"]),
}
from emuflow.physical.xilinx_packing import CASCADE_PORTS
from emuflow.physical.xilinx_packing import _select_module
from emuflow.physical.xilinx_packing import _bits
from emuflow.physical.xilinx_packing import _net_drivers
from emuflow.physical.xilinx_packing import _pack_mux_cone
from emuflow.physical.xilinx_packing import _mux_input_driver
from emuflow.physical.xilinx_packing import _derive_cascade_chains
from emuflow.physical.contracts import XILINX_PLACEMENT_SCHEMA
XILINX_GUIDANCE_SCHEMA = "emuflow.xilinx-global-placement-guidance/v1"
XILINX_CONSTRAINTS_SCHEMA = "emuflow.xilinx-placement-constraints/v1"
XILINX_OPENPARF_ATOMIC_BRIDGE_PROVIDER = (
    "openparf-native-mcf-direct-lg-ism-atomic-bridge-v1"
)
_SITE_XY_RE = re.compile(r"^(?P<kind>[A-Z0-9_]+)_X(?P<x>\d+)Y(?P<y>\d+)$")

SLICE_MUX_SELECT_SITE_PINS = {
    "F7MUX_AB": "AX",
    "F7MUX_CD": "CX",
    "F7MUX_EF": "EX",
    "F7MUX_GH": "GX",
    "F8MUX_BOT": "BX",
    "F8MUX_TOP": "FX",
    "F9MUX": "DX",
}
_sha256 = file_sha256
_MAX_FIXTURE_OBJECTS = 4096
_MAX_FIXTURE_BYTES = 8 * 1024 * 1024

XILINX_EXACT_SITE_LEGALIZER_PROVIDER = (
    "emuflow-xilinx-exact-site-legalizer-v3-physical-grid"
)


XILINX_ROUTE_A_SITE_UTILIZATION_LIMIT = 1.0


def _ff_control_set(cell: Mapping[str, Any]) -> str:
    cell_type = cell.get("type")
    control_port = {
        "FDCE": "CLR", "FDRE": "R", "FDPE": "PRE", "FDSE": "S",
    }[cell_type]
    parameters = cell.get("parameters")
    relevant_parameters = {
        key: value
        for key, value in sorted(parameters.items())
        if key.startswith("IS_")
    } if isinstance(parameters, dict) else {}
    value = {
        "type": cell_type,
        "clock": _bits(cell, "C"),
        "enable": _bits(cell, "CE"),
        "control": _bits(cell, control_port),
        "parameters": relevant_parameters,
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _slice_ff_control_sets(
    cells: Mapping[str, Any], assignments: Sequence[Mapping[str, Any]]
) -> List[str]:
    """Validate UltraScale+ slice FF controls and return their exact sets.

    UltraScale+ does not require one control set across a complete slice.  It
    shares clock and set/reset within each half-slice, while clock-enable is
    shared independently by the FF and FF2 lanes in that half.  Keep this
    physical rule in one place so native-placement bridging and the
    independent PackedSiteNetlist checker cannot drift apart.
    """

    half_cksr: Dict[int, Tuple[Any, ...]] = {}
    lane_ce: Dict[Tuple[int, int], Tuple[Any, ...]] = {}
    control_sets = set()
    for assignment in assignments:
        name = assignment.get("instance")
        if name not in cells or cells[name].get("type") not in FF_TYPES:
            continue
        bel = assignment.get("bel")
        if bel not in SLICE_FF_BELS:
            raise ValidationError(
                f"FF {name!r} has invalid concrete slice BEL {bel!r}"
            )
        bel_index = SLICE_FF_BELS.index(bel)
        half = 0 if bel_index < 8 else 1
        lane = bel_index % 2
        cell = cells[name]
        cell_type = cell["type"]
        control_port = {
            "FDCE": "CLR", "FDRE": "R", "FDPE": "PRE", "FDSE": "S",
        }[cell_type]
        parameters = cell.get("parameters")
        relevant_parameters = {
            key: value
            for key, value in sorted(parameters.items())
            if key.startswith("IS_")
        } if isinstance(parameters, dict) else {}
        clock_sr = (
            _bits(cell, "C"), cell_type, control_port,
            _bits(cell, control_port),
            json.dumps(relevant_parameters, sort_keys=True, separators=(",", ":")),
        )
        enable = _bits(cell, "CE")
        previous_cksr = half_cksr.setdefault(half, clock_sr)
        previous_ce = lane_ce.setdefault((half, lane), enable)
        if previous_cksr != clock_sr or previous_ce != enable:
            raise ValidationError(
                "slice cluster violates UltraScale+ half-slice FF control legality"
            )
        control_sets.add(_ff_control_set(cell))
    return sorted(control_sets)


def _validate_declared_slice_control_sets(
    cluster: Mapping[str, Any], expected: Sequence[str]
) -> None:
    """Check the non-duplicated scalar/list control-set representation."""

    scalar = cluster.get("control_set")
    multiple = cluster.get("control_sets")
    if len(expected) <= 1:
        expected_scalar = expected[0] if expected else None
        if scalar != expected_scalar or multiple is not None:
            raise ValidationError(
                "slice cluster FF control-set certificate is invalid"
            )
        return
    if scalar is not None or multiple != list(expected):
        raise ValidationError(
            "slice cluster multi-control-set certificate is invalid"
        )


def _slice_site_pin_conflicts(
    cells: Mapping[str, Any],
    clusters: Sequence[Mapping[str, Any]],
    drivers: Mapping[Any, str],
) -> List[Dict[str, Any]]:
    """Return conflicting external users of UltraScale+ slice input pins.

    The primary FF in each A-H lane receives its fabric bypass through the
    corresponding AX-HX site pin.  Dedicated MUXF7/F8/F9 select inputs reuse
    seven of those pins.  The secondary FF receives its bypass through the
    independent A_I-H_I pin and therefore must not be aliased with the primary
    FF.  A same-net shared pin is legal; two different external nets are not.
    """

    relevant_clusters = [
        cluster for cluster in clusters
        if cluster.get("kind") == "slice"
        and any(
            assignment.get("cell_type") in MUX_TYPES
            for assignment in cluster.get("assignments", [])
        )
    ]
    target_bits = {
        bit
        for cluster in relevant_clusters
        for assignment in cluster.get("assignments", [])
        for port in (
            ("S",)
            if assignment.get("cell_type") in MUX_TYPES
            and assignment.get("bel") in SLICE_MUX_SELECT_SITE_PINS
            else ("D",)
            if assignment.get("cell_type") in FF_TYPES
            else ()
        )
        for bit in _bits(cells[assignment["instance"]], port)
        if isinstance(bit, int)
    }
    driver_instances = {
        drivers[bit] for bit in target_bits if bit in drivers
    }
    owner = {
        assignment["instance"]: cluster["id"]
        for cluster in clusters
        for assignment in cluster.get("assignments", [])
        if assignment.get("instance") in driver_instances
    }

    def external_bit(
        cluster_id: str, instance: str, port: str
    ) -> Optional[int]:
        bits = _bits(cells[instance], port)
        if len(bits) != 1 or not isinstance(bits[0], int):
            return None
        driver = drivers.get(bits[0])
        # A missing cell driver can be a top-level input.  Treat it as an
        # external user conservatively; driverless nets are harmlessly
        # over-constrained instead of escaping physical-pin validation.
        if driver is None or owner.get(driver) != cluster_id:
            return bits[0]
        return None

    conflicts: List[Dict[str, Any]] = []
    for cluster in relevant_clusters:
        assignments = cluster.get("assignments", [])
        cluster_id = cluster.get("id")
        mux_users: Dict[str, List[Tuple[int, str, str]]] = defaultdict(list)
        ff_users: Dict[str, List[Tuple[int, str, str]]] = defaultdict(list)
        for assignment in assignments:
            instance = assignment.get("instance")
            bel = assignment.get("bel")
            cell_type = assignment.get("cell_type")
            if not isinstance(instance, str):
                continue
            if cell_type in MUX_TYPES and bel in SLICE_MUX_SELECT_SITE_PINS:
                bit = external_bit(cluster_id, instance, "S")
                if bit is not None:
                    resource = SLICE_MUX_SELECT_SITE_PINS[bel]
                    mux_users[resource].append((bit, instance, bel))
            elif cell_type in FF_TYPES and isinstance(bel, str) and bel in {
                f"{letter}FF" for letter in "ABCDEFGH"
            }:
                bit = external_bit(cluster_id, instance, "D")
                if bit is not None:
                    ff_users[f"{bel[0]}X"].append((bit, instance, bel))
        for resource in sorted(set(mux_users).intersection(ff_users)):
            uses = [*mux_users[resource], *ff_users[resource]]
            if len({bit for bit, _instance, _bel in uses}) > 1:
                conflicts.append({
                    "cluster": cluster_id,
                    "resource": resource,
                    "uses": [
                        {"bit": bit, "instance": instance, "bel": bel}
                        for bit, instance, bel in uses
                    ],
                })
    return conflicts


def legalize_xilinx_slice_site_pins(
    cells: Mapping[str, Any], clusters: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Repair MUX-select/primary-FF site-pin conflicts within each slice.

    OpenPARF chooses legal atomic BEL capacities but does not model the shared
    AX/CX/... bypass pins.  Preserve every certified physical site and move
    only FFs among free or swappable FF BELs in that same site.  Each candidate
    is rechecked against the UltraScale+ half-slice clock/set-reset and lane-CE
    rules before it is accepted.
    """

    if len(cells) > _MAX_FIXTURE_OBJECTS or len(clusters) > _MAX_FIXTURE_OBJECTS:
        raise ValidationError("site oracle accepts bounded test fixtures only")
    required_bits: Set[Any] = set()
    for cluster in clusters:
        assignments = cluster.get("assignments", [])
        if cluster.get("kind") != "slice" or not any(
            assignment.get("cell_type") in MUX_TYPES
            for assignment in assignments
        ):
            continue
        for assignment in assignments:
            instance = assignment.get("instance")
            bel = assignment.get("bel")
            cell_type = assignment.get("cell_type")
            if not isinstance(instance, str):
                continue
            port = (
                "S"
                if cell_type in MUX_TYPES and bel in SLICE_MUX_SELECT_SITE_PINS
                else "D"
                if cell_type in FF_TYPES
                else None
            )
            if port is not None:
                required_bits.update(
                    bit for bit in _bits(cells[instance], port)
                    if isinstance(bit, int)
                )
    drivers = _net_drivers(cells, required_bits)
    repairs: List[Dict[str, str]] = []
    cluster_by_id = {cluster["id"]: cluster for cluster in clusters}
    initial = _slice_site_pin_conflicts(cells, clusters, drivers)
    for cluster_id in sorted({item["cluster"] for item in initial}):
        cluster = cluster_by_id[cluster_id]
        while True:
            # Once the initial whole-design audit identifies an affected site,
            # evaluate candidates against that one bounded 16-FF cluster.
            # This avoids rescanning a production netlist for every repair.
            conflicts = _slice_site_pin_conflicts(cells, [cluster], drivers)
            if not conflicts:
                break
            conflict = conflicts[0]
            assignments = cluster["assignments"]
            ff_by_bel = {
                assignment["bel"]: assignment
                for assignment in assignments
                if assignment.get("cell_type") in FF_TYPES
            }
            resource = conflict["resource"]
            source_bel = f"{resource[0]}FF"
            source = ff_by_bel.get(source_bel)
            if source is None:
                raise ValidationError(
                    "slice site-pin conflict has no primary FF owner"
                )
            original_conflicts = len(conflicts)
            source_index = SLICE_FF_BELS.index(source_bel)
            candidate_bels = sorted(
                (bel for bel in SLICE_FF_BELS if bel not in ff_by_bel),
                key=lambda bel: (
                    bel != f"{resource[0]}FF2",
                    (SLICE_FF_BELS.index(bel) < 8) != (source_index < 8),
                    abs(SLICE_FF_BELS.index(bel) - source_index),
                    bel,
                ),
            )
            accepted = False
            for target_bel in candidate_bels:
                source["bel"] = target_bel
                try:
                    _slice_ff_control_sets(cells, assignments)
                    remaining = _slice_site_pin_conflicts(
                        cells, [cluster], drivers
                    )
                except ValidationError:
                    remaining = conflicts
                if len(remaining) < original_conflicts:
                    repairs.append({
                        "cluster": cluster["id"],
                        "instance": source["instance"],
                        "from_bel": source_bel,
                        "to_bel": target_bel,
                    })
                    accepted = True
                    break
                source["bel"] = source_bel
            if accepted:
                continue
            for target_bel in sorted(ff_by_bel):
                if target_bel == source_bel:
                    continue
                target = ff_by_bel[target_bel]
                source["bel"], target["bel"] = target_bel, source_bel
                try:
                    _slice_ff_control_sets(cells, assignments)
                    remaining = _slice_site_pin_conflicts(
                        cells, [cluster], drivers
                    )
                except ValidationError:
                    remaining = conflicts
                if len(remaining) < original_conflicts:
                    repairs.extend([
                        {
                            "cluster": cluster["id"],
                            "instance": source["instance"],
                            "from_bel": source_bel,
                            "to_bel": target_bel,
                        },
                        {
                            "cluster": cluster["id"],
                            "instance": target["instance"],
                            "from_bel": target_bel,
                            "to_bel": source_bel,
                        },
                    ])
                    accepted = True
                    break
                source["bel"], target["bel"] = source_bel, target_bel
            if not accepted:
                raise ValidationError(
                    "slice site-pin conflict cannot be legalized within certified "
                    f"site {cluster['id']!r} on {resource}"
                )
    return {
        "provider": "ultrascaleplus-slice-site-pin-legalizer-v1",
        "moved_ffs": len(repairs),
        "sites": len({repair["cluster"] for repair in repairs}),
        "repairs": repairs,
    }


def validate_xilinx_packing(
    mapped_json: Path,
    packed_path: Path,
    *,
    top: Optional[str] = None,
    architecture_path: Optional[Path] = None,
    mapped_value: Optional[Mapping[str, Any]] = None,
    mapped_sha256: Optional[str] = None,
    packed_value: Optional[Mapping[str, Any]] = None,
    architecture: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Independently re-check ownership, capacities, BELs, and templates."""

    _check_fixture_files(mapped_json if mapped_value is None else None,
                         packed_path if packed_value is None else None, architecture_path)
    source = read_json(mapped_json) if mapped_value is None else mapped_value
    selected_top, module = _select_module(source, top)
    cells = module.get("cells")
    if isinstance(cells, Mapping) and len(cells) > _MAX_FIXTURE_OBJECTS:
        raise ValidationError("site oracle accepts bounded test fixtures only")
    packed = read_json(packed_path) if packed_value is None else packed_value
    if not isinstance(cells, Mapping) or not isinstance(packed, dict):
        raise ValidationError("packing inputs are invalid")
    if packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA or packed.get("status") != "pass":
        raise ValidationError("PackedSiteNetlist header is invalid")
    if packed.get("top") != selected_top:
        raise ValidationError("PackedSiteNetlist top does not match mapped netlist")
    if packed.get("source", {}).get("mapped_json_sha256") != (
        mapped_sha256 or _sha256(mapped_json)
    ):
        raise ValidationError("PackedSiteNetlist source digest is invalid")
    drivers = _net_drivers(cells)

    expected_cascades = _derive_cascade_chains(cells)
    if packed.get("cascade_chains") != expected_cascades:
        raise ValidationError("PackedSiteNetlist dedicated-cascade certificate is invalid")

    template_bels: Dict[str, Dict[str, set]] = {}
    if architecture_path is not None:
        architecture_value = (
            read_json(architecture_path) if architecture is None else architecture
        )
        templates = (
            architecture_value.get("site_templates")
            if isinstance(architecture_value, Mapping) else None
        )
        if not isinstance(templates, dict):
            raise ValidationError("ArchitectureDB site templates are invalid")
        for template, contract in templates.items():
            bel_map = {}
            for bel in contract.get("bels", []):
                bel_map[bel["name"]] = set(bel.get("compatible_cells", []))
            template_bels[template] = bel_map

    owners: Dict[str, str] = {}
    for cluster in packed.get("clusters", []):
        if not isinstance(cluster, dict) or not isinstance(cluster.get("assignments"), list):
            raise ValidationError("PackedSiteNetlist cluster is invalid")
        assignments = cluster["assignments"]
        instances = []
        used_bels = set()
        for assignment in assignments:
            name = assignment.get("instance")
            cell_type = assignment.get("cell_type")
            if name not in cells or cells[name].get("type") != cell_type:
                raise ValidationError("PackedSiteNetlist assignment does not match source")
            if name in owners:
                raise ValidationError(f"cell {name!r} is assigned more than once")
            owners[name] = cluster.get("id")
            instances.append(name)
            candidates = assignment.get("bel_candidates") or [assignment.get("bel")]
            if not isinstance(candidates, list) or not candidates or any(not isinstance(b, str) for b in candidates):
                raise ValidationError("PackedSiteNetlist BEL contract is invalid")
            if assignment.get("bel") is not None:
                if assignment["bel"] in used_bels:
                    raise ValidationError("PackedSiteNetlist reuses a BEL")
                used_bels.add(assignment["bel"])
            if template_bels:
                compatible = False
                for template in cluster.get("site_templates", []):
                    bel_map = template_bels.get(template, {})
                    compatible |= any(cell_type in bel_map.get(bel, set()) for bel in candidates)
                if not compatible:
                    raise ValidationError(f"cell {name!r} has no compatible ArchitectureDB BEL")

        kind = cluster.get("kind")
        if kind == "slice":
            lut_count = sum(cells[name].get("type") in LUT_TYPES for name in instances)
            ff_names = [name for name in instances if cells[name].get("type") in FF_TYPES]
            if lut_count > len(SLICE_LUT_BELS) or len(ff_names) > len(SLICE_FF_BELS):
                raise ValidationError("slice cluster exceeds physical capacity")
            control_sets = _slice_ff_control_sets(cells, assignments)
            _validate_declared_slice_control_sets(cluster, control_sets)
            assignment_bels = {
                assignment["instance"]: assignment.get("bel")
                for assignment in assignments
            }
            for name in instances:
                cell_type = cells[name].get("type")
                if cell_type == "MUXF7":
                    expected = {
                        "F7MUX_AB": {"A6LUT", "B6LUT"},
                        "F7MUX_CD": {"C6LUT", "D6LUT"},
                        "F7MUX_EF": {"E6LUT", "F6LUT"},
                        "F7MUX_GH": {"G6LUT", "H6LUT"},
                    }.get(assignment_bels[name])
                    input_bels = {
                        assignment_bels.get(
                            _mux_input_driver(cells, drivers, name, port)
                        )
                        for port in ("I0", "I1")
                    }
                    if expected is None or input_bels != expected:
                        raise ValidationError("MUXF7 packing topology is invalid")
                elif cell_type == "MUXF8":
                    expected = {
                        "F8MUX_BOT": {"F7MUX_AB", "F7MUX_CD"},
                        "F8MUX_TOP": {"F7MUX_EF", "F7MUX_GH"},
                    }.get(assignment_bels[name])
                    input_bels = {
                        assignment_bels.get(
                            _mux_input_driver(cells, drivers, name, port)
                        )
                        for port in ("I0", "I1")
                    }
                    if expected is None or input_bels != expected:
                        raise ValidationError("MUXF8 packing topology is invalid")
                elif cell_type == "MUXF9":
                    input_bels = {
                        assignment_bels.get(
                            _mux_input_driver(cells, drivers, name, port)
                        )
                        for port in ("I0", "I1")
                    }
                    if input_bels != {"F8MUX_BOT", "F8MUX_TOP"}:
                        raise ValidationError("MUXF9 packing topology is invalid")
        elif kind == "carry":
            carry_names = [
                name for name in instances if cells[name].get("type") == "CARRY8"
            ]
            adapters = [
                name for name in instances
                if cells[name].get("type") == DUAL_OUTPUT_LUT_TYPE
            ]
            if len(carry_names) != 1 or len(adapters) != 8 or len(instances) != 9:
                raise ValidationError("carry cluster is invalid")
            carry_name = carry_names[0]
            assignment_bels = {
                assignment["instance"]: assignment.get("bel")
                for assignment in assignments
            }
            for bit_index, letter in enumerate("ABCDEFGH"):
                di_driver = drivers.get(_bits(cells[carry_name], "DI")[bit_index])
                s_driver = drivers.get(_bits(cells[carry_name], "S")[bit_index])
                if (
                    di_driver is None
                    or di_driver != s_driver
                    or di_driver not in adapters
                    or assignment_bels.get(di_driver) != f"{letter}6LUT"
                ):
                    raise ValidationError("CARRY8 LUT6_2 packing topology is invalid")
        elif kind == "hard":
            cell_types = [cells[name].get("type") for name in instances]
            if set(cell_types) == {"RAMB18E2"}:
                bels = {assignment.get("bel") for assignment in assignments}
                if not 1 <= len(instances) <= 2 or len(bels) != len(instances):
                    raise ValidationError("RAMB18E2 shared-site cluster is invalid")
                if not bels.issubset({"RAMB18E2_L", "RAMB18E2_U"}):
                    raise ValidationError("RAMB18E2 shared-site BEL is invalid")
                if cluster.get("site_mode") != f"RAMB18E2x{len(instances)}":
                    raise ValidationError("RAMB18E2 shared-site mode is invalid")
            elif len(instances) != 1 or cell_types[0] not in HARD_BINDINGS:
                raise ValidationError("hard cluster is invalid")
        else:
            raise ValidationError(f"unknown packing cluster kind {kind!r}")

    site_pin_conflicts = _slice_site_pin_conflicts(
        cells, packed.get("clusters", []), drivers
    )
    if site_pin_conflicts:
        conflict = site_pin_conflicts[0]
        raise ValidationError(
            "slice external site pin is assigned to different nets: "
            f"cluster={conflict['cluster']!r} resource={conflict['resource']}"
        )

    constants = packed.get("unplaced_constants")
    if not isinstance(constants, Sequence) or isinstance(constants, (str, bytes)) or any(cells.get(name, {}).get("type") not in CONSTANT_TYPES for name in constants):
        raise ValidationError("PackedSiteNetlist constants are invalid")
    if set(owners).intersection(constants) or set(owners).union(constants) != set(cells):
        raise ValidationError("PackedSiteNetlist cell ownership is incomplete")
    for chain in expected_cascades:
        if any(instance not in owners for instance in chain["instances"]):
            raise ValidationError("dedicated cascade contains an unplaced cell")
    return {
        "status": "pass",
        "schema": "emuflow.packed-site-netlist-validation/v1",
        "cells": len(cells),
        "clusters": len(packed.get("clusters", [])),
        "architecture_checked": architecture_path is not None,
        "packed_sha256": _sha256(packed_path),
    }


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

    _check_fixture_files(packed_path if packed_value is None else None, architecture_path,
                         placement_path if placement_value is None else None, constraints_path)
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


def _check_fixture_files(*paths):
    for path in paths:
        if path is not None and Path(path).stat().st_size > _MAX_FIXTURE_BYTES:
            raise ValidationError("site oracle accepts bounded test fixtures only")

def _slice_cluster(
    cluster_id: str,
    lut_names: Sequence[str],
    ff_names: Sequence[str],
    cells: Mapping[str, Any],
    control_set: Optional[str],
) -> Dict[str, Any]:
    assignments = [
        {"instance": name, "cell_type": cells[name]["type"], "bel": bel}
        for name, bel in zip(lut_names, SLICE_LUT_BELS)
    ]
    assignments.extend(
        {"instance": name, "cell_type": cells[name]["type"], "bel": bel}
        for name, bel in zip(ff_names, SLICE_FF_BELS)
    )
    return {
        "id": cluster_id,
        "kind": "slice",
        "site_templates": ["SLICEL", "SLICEM"],
        "control_set": control_set,
        "assignments": assignments,
    }


def pack_xilinx_sites(
    mapped_json: Path,
    output: Path,
    *,
    top: Optional[str] = None,
    mapped_value: Optional[Mapping[str, Any]] = None,
    mapped_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    """Pack normalized cells using conservative exact UltraScale+ rules."""

    if mapped_value is None:
        _check_fixture_files(mapped_json)
    source = read_json(mapped_json) if mapped_value is None else mapped_value
    source_sha256 = mapped_sha256 or _sha256(mapped_json)
    selected_top, module = _select_module(source, top)
    cells = module.get("cells")
    if not isinstance(cells, Mapping):
        raise ValidationError("mapped Yosys JSON cells are invalid")
    if len(cells) > _MAX_FIXTURE_OBJECTS:
        raise ValidationError("site oracle accepts bounded test fixtures only")
    audit = audit_xilinx_mapped_json(
        mapped_json,
        top=top,
        source=source,
        hash_path=False,
    )
    clusters: List[Dict[str, Any]] = []
    constants = sorted(name for name, cell in cells.items() if cell.get("type") in CONSTANT_TYPES)

    drivers = _net_drivers(cells)
    muxes = {name for name, cell in cells.items() if cell.get("type") in MUX_TYPES}
    mux_children = set()
    for name in muxes:
        for port in ("I0", "I1"):
            driver = _mux_input_driver(cells, drivers, name, port)
            if driver in muxes:
                mux_children.add(driver)
    mux_roots = sorted(muxes - mux_children)
    mux_members = set()
    for index, root in enumerate(mux_roots):
        assignments = _pack_mux_cone(root, cells, drivers)
        members = {item["instance"] for item in assignments}
        overlap = members.intersection(mux_members)
        if overlap:
            raise ValidationError(
                "mux cones share physical cells: " + ", ".join(sorted(overlap))
            )
        mux_members.update(members)
        clusters.append({
            "id": f"mux-{index:06d}",
            "kind": "slice",
            "site_templates": ["SLICEL", "SLICEM"],
            "control_set": None,
            "assignments": assignments,
        })
    if mux_members.intersection(constants):
        raise ValidationError("mux cone contains a constant pseudo-cell")

    # CARRY8 DI/S are dedicated in-slice LUT O5/O6 connections, not general
    # inter-site pins.  The normalizer therefore creates one LUT6_2
    # route-through per carry bit; bind those eight cells to the matching LUT
    # BELs in the same exclusive slice.
    carry_members = set()
    for index, name in enumerate(sorted(name for name, cell in cells.items() if cell.get("type") == "CARRY8")):
        assignments = []
        for bit_index, letter in enumerate("ABCDEFGH"):
            di = _bits(cells[name], "DI")[bit_index]
            s = _bits(cells[name], "S")[bit_index]
            di_driver = drivers.get(di)
            s_driver = drivers.get(s)
            if (
                di_driver is None
                or di_driver != s_driver
                or cells[di_driver].get("type") != DUAL_OUTPUT_LUT_TYPE
            ):
                raise ValidationError(
                    f"CARRY8 {name!r} bit {bit_index} lacks its LUT6_2 DI/S adapter"
                )
            if di_driver in carry_members:
                raise ValidationError("a LUT6_2 adapter belongs to multiple carry bits")
            carry_members.add(di_driver)
            assignments.append({
                "instance": di_driver,
                "cell_type": DUAL_OUTPUT_LUT_TYPE,
                "bel": f"{letter}6LUT",
            })
        assignments.append({"instance": name, "cell_type": "CARRY8", "bel": "CARRY8"})
        clusters.append({
            "id": f"carry-{index:06d}",
            "kind": "carry",
            "site_templates": ["SLICEL", "SLICEM"],
            "control_set": None,
            "assignments": assignments,
        })

    # One physical RAMB36 site supports either one RAMB36E2 or two independent
    # RAMB18E2 halves.  DeviceResources exposes RAMB180/RAMB181/RAMB36 as
    # alternate site modes, so a two-cell cluster explicitly consumes the
    # lower and upper BEL together rather than pretending they are two sites.
    ramb18_names = sorted(
        name for name, cell in cells.items() if cell.get("type") == "RAMB18E2"
    )
    for index, offset in enumerate(range(0, len(ramb18_names), 2)):
        names = ramb18_names[offset:offset + 2]
        assignments = []
        for name, bel in zip(names, ("RAMB18E2_L", "RAMB18E2_U")):
            assignments.append({
                "instance": name,
                "cell_type": "RAMB18E2",
                "bel": bel,
            })
        clusters.append({
            "id": f"ramb18e2-{index:06d}",
            "kind": "hard",
            "site_templates": ["RAMB180", "RAMB181"],
            "site_mode": f"RAMB18E2x{len(names)}",
            "control_set": None,
            "assignments": assignments,
        })

    for cell_type, (templates, bels) in HARD_BINDINGS.items():
        names = sorted(name for name, cell in cells.items() if cell.get("type") == cell_type)
        for index, name in enumerate(names):
            clusters.append({
                "id": f"{cell_type.lower()}-{index:06d}",
                "kind": "hard",
                "site_templates": templates,
                "control_set": None,
                "assignments": [{
                    "instance": name,
                    "cell_type": cell_type,
                    "bel_candidates": bels,
                }],
            })

    luts = sorted(
        name for name, cell in cells.items()
        if cell.get("type") in LUT_TYPES and name not in mux_members
    )
    ff_groups: Dict[str, List[str]] = defaultdict(list)
    for name, cell in sorted(cells.items()):
        if cell.get("type") in FF_TYPES and name not in mux_members:
            ff_groups[_ff_control_set(cell)].append(name)

    # A slice is restricted to one complete FF control set.  This is stricter
    # than the silicon maximum and therefore cannot create an illegal pack.
    slice_index = 0
    lut_cursor = 0
    for control_set in sorted(ff_groups):
        ff_names = ff_groups[control_set]
        for offset in range(0, len(ff_names), len(SLICE_FF_BELS)):
            chunk = ff_names[offset:offset + len(SLICE_FF_BELS)]
            lut_chunk = luts[lut_cursor:lut_cursor + len(SLICE_LUT_BELS)]
            lut_cursor += len(lut_chunk)
            clusters.append(_slice_cluster(
                f"slice-{slice_index:06d}", lut_chunk, chunk, cells, control_set
            ))
            slice_index += 1
    while lut_cursor < len(luts):
        lut_chunk = luts[lut_cursor:lut_cursor + len(SLICE_LUT_BELS)]
        lut_cursor += len(lut_chunk)
        clusters.append(_slice_cluster(
            f"slice-{slice_index:06d}", lut_chunk, [], cells, None
        ))
        slice_index += 1

    handled = set(constants)
    for cluster in clusters:
        handled.update(item["instance"] for item in cluster["assignments"])
    missing = sorted(set(cells) - handled)
    if missing:
        raise ValidationError("unpacked Xilinx cells: " + ", ".join(missing))

    cascade_chains = _derive_cascade_chains(cells)

    kind_counts = Counter(cluster["kind"] for cluster in clusters)
    value = {
        "schema": PACKED_SITE_NETLIST_SCHEMA,
        "status": "pass",
        "mapping_profile": XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
        "top": selected_top,
        "source": {
            "mapped_json_sha256": source_sha256,
            "primitive_library_sha256": audit["primitive_library_sha256"],
        },
        "policy": {
            "provider": "emuflow-ultrascaleplus-conservative-packer-v1",
            "slice_lut_capacity": len(SLICE_LUT_BELS),
            "slice_ff_capacity": len(SLICE_FF_BELS),
            "ff_control_sets_per_slice": 1,
            "carry_site_exclusive": True,
        },
        "clusters": clusters,
        "cascade_chains": cascade_chains,
        "unplaced_constants": constants,
        "summary": {
            "cells": len(cells),
            "placed_cells": len(cells) - len(constants),
            "constant_cells": len(constants),
            "clusters": len(clusters),
            "cluster_kinds": dict(sorted(kind_counts.items())),
            "cascade_chains": len(cascade_chains),
            "cascade_links": sum(len(chain["links"]) for chain in cascade_chains),
        },
    }
    write_json(output, value)
    validate_xilinx_packing(
        mapped_json,
        output,
        top=selected_top,
        mapped_value=source,
        mapped_sha256=source_sha256,
        packed_value=value,
    )
    return value


def exhaustive_minimum_slice_count(items: Iterable[Tuple[int, int]]) -> int:
    """Tiny independent oracle for (LUT, FF) capacity-directed tests."""

    from itertools import islice
    demands = list(islice(items, 25))
    if len(demands) > 24:
        raise ValidationError("exhaustive slice oracle accepts at most 24 items")
    demands.sort(reverse=True)
    best = len(demands)
    bins: List[List[int]] = []

    def visit(index: int) -> None:
        nonlocal best
        if len(bins) >= best:
            return
        if index == len(demands):
            best = len(bins)
            return
        lut, ff = demands[index]
        seen = set()
        for slot in bins:
            state = tuple(slot)
            if state in seen:
                continue
            seen.add(state)
            if slot[0] + lut <= 8 and slot[1] + ff <= 16:
                slot[0] += lut
                slot[1] += ff
                visit(index + 1)
                slot[0] -= lut
                slot[1] -= ff
        bins.append([lut, ff])
        visit(index + 1)
        bins.pop()

    if not demands:
        return 0
    visit(0)
    return best


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


def _distance(
    cluster_ids: Sequence[str], sites: Sequence[Mapping[str, Any]],
    guidance: Mapping[str, Tuple[float, float]],
) -> Tuple[float, Tuple[str, ...]]:
    cost = 0.0
    for cluster_id, site in zip(cluster_ids, sites):
        target = guidance.get(cluster_id)
        if target is not None:
            x, y = _placer_coordinate(site)
            cost += abs(x - target[0]) + abs(y - target[1])
    return cost, tuple(site["name"] for site in sites)


def place_xilinx_clusters(
    packed_path: Path,
    architecture_path: Path,
    output_path: Path,
    *,
    guidance_path: Optional[Path] = None,
    constraints_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Place whole packed clusters onto exact, non-overlapping device sites."""

    _check_fixture_files(packed_path, architecture_path, guidance_path, constraints_path)
    packed = read_json(packed_path)
    if not isinstance(packed, dict) or packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA:
        raise ValidationError("PackedSiteNetlist header is invalid")
    architecture = ArchitectureDB.load(architecture_path)
    clusters = packed.get("clusters")
    if not isinstance(clusters, list):
        raise ValidationError("PackedSiteNetlist clusters are invalid")
    if len(clusters) > _MAX_FIXTURE_OBJECTS or len(architecture.value["sites"]) > _MAX_FIXTURE_OBJECTS:
        raise ValidationError("site oracle accepts bounded test fixtures only")
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
    local_capacity, local_limits = _local_site_limits(
        sites, XILINX_ROUTE_A_SITE_UTILIZATION_LIMIT
    )
    local_usage: Counter = Counter()

    def can_reserve_sites(site_names: Sequence[str]) -> bool:
        added = Counter(
            key
            for site_name in site_names
            if (key := _clock_region_site_key(sites[site_name])) is not None
        )
        return all(
            local_usage[key] + count <= local_limits[key]
            for key, count in added.items()
        )

    def reserve_site(site_name: str) -> None:
        key = _clock_region_site_key(sites[site_name])
        if key is not None:
            local_usage[key] += 1
    candidates: Dict[str, List[str]] = {}
    candidate_cache: Dict[
        Tuple[Tuple[str, ...], Tuple[Tuple[str, Any], ...]], List[str]
    ] = {}
    resolved_by_cluster_template: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for cluster_id, cluster in cluster_by_id.items():
        legal_bases = []
        for base, site_names in sites_by_template.items():
            resolved = _resolve_cluster_bels(cluster, contracts[base])
            if resolved is None:
                continue
            resolved_by_cluster_template[(cluster_id, base)] = resolved
            legal_bases.append(base)
        cache_key = (
            tuple(sorted(legal_bases)),
            tuple(sorted(_cluster_constraint(constraints, cluster_id).items())),
        )
        legal_sites = candidate_cache.get(cache_key)
        if legal_sites is None:
            legal_sites = sorted({
                site_name
                for base in legal_bases
                for site_name in sites_by_template[base]
                if _site_satisfies_constraint(
                    sites[site_name], _cluster_constraint(constraints, cluster_id)
                )
            })
            candidate_cache[cache_key] = legal_sites
        if not legal_sites:
            raise ValidationError(
                f"cluster {cluster_id!r} has no legal site after exact constraints"
            )
        candidates[cluster_id] = legal_sites

    site_base = {}
    for base, names in sites_by_template.items():
        for name in names:
            site_base[name] = base

    placed: Dict[str, str] = {}
    used_sites: Set[str] = set()
    chains = _cascade_cluster_chains(packed, owner)
    physical_sites = {
        _physical_site_coordinate(name): name for name in site_base
    }
    membership_cache: Dict[int, Set[str]] = {}
    cascade_window_cache: Dict[
        Tuple[int, int],
        List[Tuple[
            Tuple[str, int],
            List[Tuple[str, ...]],
            List[Set[int]],
            List[Tuple[int, int, List[Tuple[int, int, int, int]]]],
        ]],
    ] = {}

    def candidate_members(cluster_id: str) -> Set[str]:
        values = candidates[cluster_id]
        key = id(values)
        if key not in membership_cache:
            membership_cache[key] = set(values)
        return membership_cache[key]

    def cascade_windows(
        values: List[str], length: int
    ) -> List[
        Tuple[
            Tuple[str, int],
            List[Tuple[str, ...]],
            List[Set[int]],
            List[Tuple[int, int, List[Tuple[int, int, int, int]]]],
        ]
    ]:
        """Index physical cascade starts once per shared candidate contract.

        The prior legalizer rescanned every candidate site for every carry/DSP
        chain.  A VU19P SLR has more than 100k slice sites, so thousands of
        short chains turned that exact search into hundreds of millions of
        Python dictionary operations.  Candidate lists are deliberately shared
        by clusters with the same placement contract; cache their contiguous
        physical windows and prune exact search with conservative column and
        fixed-size-block Manhattan lower bounds instead.
        """

        cache_key = (id(values), length)
        cached = cascade_window_cache.get(cache_key)
        if cached is not None:
            return cached
        grouped: Dict[Tuple[str, int], List[Tuple[int, Tuple[str, ...]]]] = (
            defaultdict(list)
        )
        for first_name in values:
            kind, physical_x, physical_y = _physical_site_coordinate(first_name)
            names = tuple(
                physical_sites.get((kind, physical_x, physical_y + offset), "")
                for offset in range(length)
            )
            slrs = {
                sites[name].get("physical_region", {}).get("slr")
                for name in names
                if name
            }
            if all(names) and len(slrs) == 1 and None not in slrs:
                grouped[(kind, physical_x)].append((physical_y, names))

        result = []
        for column, entries in grouped.items():
            windows = [names for _physical_y, names in sorted(entries)]
            x_values = [
                {
                    _placer_coordinate(sites[names[offset]])[0]
                    for names in windows
                }
                for offset in range(length)
            ]
            blocks = []
            for start in range(0, len(windows), 32):
                end = min(start + 32, len(windows))
                bounds = []
                for offset in range(length):
                    coordinates = [
                        _placer_coordinate(sites[windows[index][offset]])
                        for index in range(start, end)
                    ]
                    xs = [coordinate[0] for coordinate in coordinates]
                    ys = [coordinate[1] for coordinate in coordinates]
                    bounds.append((min(xs), max(xs), min(ys), max(ys)))
                blocks.append((start, end, bounds))
            result.append((column, windows, x_values, blocks))
        result.sort(key=lambda item: item[0])
        cascade_window_cache[cache_key] = result
        return result

    def best_cascade_window(chain: Sequence[str]) -> Optional[Tuple[str, ...]]:
        guided = [cluster_id for cluster_id in chain if cluster_id in guidance]
        if not guided:
            return None

        columns = []
        for column, windows, x_values, blocks in cascade_windows(
            candidates[chain[0]], len(chain)
        ):
            x_lower_bound = 0.0
            for offset, cluster_id in enumerate(chain):
                target = guidance.get(cluster_id)
                if target is not None:
                    x_lower_bound += min(
                        abs(value - target[0]) for value in x_values[offset]
                    )
            columns.append((x_lower_bound, column, windows, blocks))
        columns.sort(key=lambda item: (item[0], item[1]))

        member_sets = [candidate_members(cluster_id) for cluster_id in chain]
        best_names: Optional[Tuple[str, ...]] = None
        best_key: Optional[Tuple[float, Tuple[str, ...]]] = None

        def window_key(names: Tuple[str, ...]) -> Tuple[float, Tuple[str, ...]]:
            return _distance(chain, [sites[name] for name in names], guidance)

        def feasible(names: Tuple[str, ...]) -> bool:
            return all(
                name in member_sets[offset] and name not in used_sites
                for offset, name in enumerate(names)
            ) and can_reserve_sites(names)

        for x_lower_bound, _column, windows, blocks in columns:
            if best_key is not None and x_lower_bound > best_key[0]:
                break
            ranked_blocks = []
            for start, end, bounds in blocks:
                lower_bound = 0.0
                for offset, cluster_id in enumerate(chain):
                    target = guidance.get(cluster_id)
                    if target is None:
                        continue
                    min_x, max_x, min_y, max_y = bounds[offset]
                    if target[0] < min_x:
                        lower_bound += min_x - target[0]
                    elif target[0] > max_x:
                        lower_bound += target[0] - max_x
                    if target[1] < min_y:
                        lower_bound += min_y - target[1]
                    elif target[1] > max_y:
                        lower_bound += target[1] - max_y
                ranked_blocks.append((lower_bound, start, end))
            ranked_blocks.sort()
            for lower_bound, start, end in ranked_blocks:
                if best_key is not None and lower_bound > best_key[0]:
                    break
                for names in windows[start:end]:
                    if feasible(names):
                        key = window_key(names)
                        if best_key is None or key < best_key:
                            best_names, best_key = names, key
        return best_names

    # The tightest chain is placed first. Shared candidate contracts reuse a
    # compact column/window index, while every selected site still comes from
    # the exact deterministic minimum over all currently feasible windows.
    for chain in sorted(chains, key=lambda item: (len(candidates[item[0]]), item)):
        best = best_cascade_window(chain)
        if best is None:
            # Unguided chains retain the prior lexicographic first-fit rule.
            for first_name in candidates[chain[0]]:
                kind, physical_x, physical_y = _physical_site_coordinate(first_name)
                names = tuple(
                    physical_sites.get((kind, physical_x, physical_y + offset), "")
                    for offset in range(len(chain))
                )
                slrs = {
                    sites[name].get("physical_region", {}).get("slr")
                    for name in names
                    if name
                }
                if all(
                    name
                    and name in candidate_members(cluster_id)
                    and name not in used_sites
                    for cluster_id, name in zip(chain, names)
                ) and len(slrs) == 1 and None not in slrs and can_reserve_sites(names):
                    best = names
                    break
        if best is None:
            raise ValidationError(
                "dedicated cascade windows conflict for chain: " + " -> ".join(chain)
            )
        for cluster_id, site_name in zip(chain, best):
            placed[cluster_id] = site_name
            used_sites.add(site_name)
            reserve_site(site_name)

    remaining = sorted(
        (cluster_id for cluster_id in cluster_ids if cluster_id not in placed),
        key=lambda cluster_id: (
            len(candidates[cluster_id]),
            -len(cluster_by_id[cluster_id].get("assignments", [])),
            cluster_id,
        ),
    )
    cursors: Dict[int, int] = defaultdict(int)
    row_index_cache: Dict[int, Dict[int, List[Tuple[int, str]]]] = {}

    def nearest_available(
        values: List[str], target: Tuple[float, float]
    ) -> Optional[str]:
        key = id(values)
        rows = row_index_cache.get(key)
        if rows is None:
            mutable_rows: Dict[int, List[Tuple[int, str]]] = defaultdict(list)
            for name in values:
                x, y = _placer_coordinate(sites[name])
                mutable_rows[y].append((x, name))
            rows = {
                y: sorted(entries) for y, entries in mutable_rows.items()
            }
            row_index_cache[key] = rows
        target_x, target_y = target
        best_name = None
        best_cost = None
        for y, entries in rows.items():
            y_cost = abs(y - target_y)
            if best_cost is not None and y_cost >= best_cost:
                continue
            position = bisect_left(entries, (target_x, ""))
            left, right = position - 1, position
            while left >= 0 or right < len(entries):
                left_cost = (
                    abs(entries[left][0] - target_x) if left >= 0 else None
                )
                right_cost = (
                    abs(entries[right][0] - target_x)
                    if right < len(entries) else None
                )
                if right_cost is None or (
                    left_cost is not None and left_cost <= right_cost
                ):
                    x_cost, name = left_cost, entries[left][1]
                    left -= 1
                else:
                    x_cost, name = right_cost, entries[right][1]
                    right += 1
                cost = y_cost + x_cost
                if best_cost is not None and cost >= best_cost:
                    break
                if name not in used_sites and can_reserve_sites((name,)):
                    best_name, best_cost = name, cost
                    break
        return best_name

    for cluster_id in remaining:
        values = candidates[cluster_id]
        if cluster_id in guidance:
            selected = nearest_available(values, guidance[cluster_id])
        else:
            key = id(values)
            cursor = cursors[key]
            while cursor < len(values) and (
                values[cursor] in used_sites
                or not can_reserve_sites((values[cursor],))
            ):
                cursor += 1
            cursors[key] = cursor + 1
            selected = values[cursor] if cursor < len(values) else None
        if selected is None:
            raise ValidationError(
                f"no unoccupied legal site remains for cluster {cluster_id!r}"
            )
        placed[cluster_id] = selected
        used_sites.add(selected)
        reserve_site(selected)

    placements = []
    displacement = []
    for cluster_id in sorted(cluster_ids):
        site_name = placed[cluster_id]
        site = sites[site_name]
        base = site_base[site_name]
        assignments = _materialize_assignment_sites(
            site_name, resolved_by_cluster_template[(cluster_id, base)]
        )
        target = guidance.get(cluster_id)
        if target is not None:
            physical_x, physical_y = _placer_coordinate(site)
            displacement.append(
                abs(physical_x - target[0])
                + abs(physical_y - target[1])
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
        "provider": XILINX_EXACT_SITE_LEGALIZER_PROVIDER,
        "policy": {
            "clock_region_site_utilization_limit": (
                XILINX_ROUTE_A_SITE_UTILIZATION_LIMIT
            ),
            "capacity_rounding": "ceil-with-one-site-minimum",
        },
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
