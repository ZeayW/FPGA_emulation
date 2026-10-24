"""Bounded packing/placement test oracles; never installed in the runtime.

These helpers construct small legality fixtures for the independent production
validators. They do not launch OpenPARF, RapidWright or any complete flow.
"""
from __future__ import annotations
import json
import math
from bisect import bisect_left
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from emuflow.architecture import ArchitectureDB
from emuflow.errors import ValidationError
from emuflow.io import file_sha256, read_json, write_json
from emuflow.xilinx_primitives import XILINX_ULTRASCALEPLUS_OPEN_PROFILE, audit_xilinx_mapped_json
from emuflow.xilinx_packing import (
    PACKED_SITE_NETLIST_SCHEMA, SLICE_LUT_BELS, SLICE_FF_BELS,
    FF_TYPES, LUT_TYPES, DUAL_OUTPUT_LUT_TYPE, CONSTANT_TYPES, MUX_TYPES,
    HARD_BINDINGS, CASCADE_PORTS, _select_module, _bits, _ff_control_set,
    _net_drivers, _pack_mux_cone, _mux_input_driver, _derive_cascade_chains,
    validate_xilinx_packing,
)
from emuflow.xilinx_placement import (
    XILINX_PLACEMENT_SCHEMA, XILINX_EXACT_SITE_LEGALIZER_PROVIDER,
    XILINX_ROUTE_A_SITE_UTILIZATION_LIMIT, _nonempty, _load_constraints,
    _template_contracts, _local_site_limits, _clock_region_site_key,
    _cluster_constraint, _site_satisfies_constraint, _resolve_cluster_bels,
    _physical_site_coordinate, _placer_coordinate, _materialize_assignment_sites,
    _cascade_cluster_chains, validate_xilinx_placement,
)
XILINX_GUIDANCE_SCHEMA = "emuflow.xilinx-global-placement-guidance/v1"
_sha256 = file_sha256
_MAX_FIXTURE_OBJECTS = 4096
_MAX_FIXTURE_BYTES = 8 * 1024 * 1024

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
