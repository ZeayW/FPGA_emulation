"""Deterministic, fail-closed UltraScale+ site packing for Route A."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .errors import ValidationError
from .io import read_json, write_json
from .xilinx_primitives import (
    XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
    audit_xilinx_mapped_json,
)


PACKED_SITE_NETLIST_SCHEMA = "emuflow.packed-site-netlist/v1"
SLICE_LUT_BELS = tuple(f"{letter}6LUT" for letter in "ABCDEFGH")
SLICE_FF_BELS = tuple(
    bel for letter in "ABCDEFGH" for bel in (f"{letter}FF", f"{letter}FF2")
)
FF_TYPES = {"FDCE", "FDPE", "FDRE", "FDSE"}
LUT_TYPES = {f"LUT{width}" for width in range(1, 7)}
CONSTANT_TYPES = {"GND", "VCC"}
MUX_TYPES = {"MUXF7", "MUXF8"}
HARD_BINDINGS = {
    "DSP48E2": (["DSP48E2"], ["DSP_ALU"]),
    "RAMB18E2": (["RAMB180", "RAMB181"], ["RAMB18E2_L", "RAMB18E2_U"]),
    "RAMB36E2": (["RAMB36"], ["RAMB36E2"]),
    "URAM288": (["URAM288"], ["URAM_288K_INST"]),
}

# Dedicated vertical cascade wires are not ordinary fabric nets.  The packer
# records their exact instance chains so placement can enforce adjacency and
# routing cannot silently demote them to general interconnect.
CASCADE_PORTS = {
    "CARRY8": (("carry", "CO", "CI", -1),),
    "DSP48E2": (
        ("ac", "ACOUT", "ACIN", None),
        ("bc", "BCOUT", "BCIN", None),
        ("carry", "CARRYCASCOUT", "CARRYCASCIN", None),
        ("multsign", "MULTSIGNOUT", "MULTSIGNIN", None),
        ("pc", "PCOUT", "PCIN", None),
    ),
    "RAMB18E2": (
        ("data-a", "CASDOUTA", "CASDINA", None),
        ("data-b", "CASDOUTB", "CASDINB", None),
        ("parity-a", "CASDOUTPA", "CASDINPA", None),
        ("parity-b", "CASDOUTPB", "CASDINPB", None),
    ),
    "RAMB36E2": (
        ("data-a", "CASDOUTA", "CASDINA", None),
        ("data-b", "CASDOUTB", "CASDINB", None),
        ("parity-a", "CASDOUTPA", "CASDINPA", None),
        ("parity-b", "CASDOUTPB", "CASDINPB", None),
        ("dbiterr", "CASOUTDBITERR", "CASINDBITERR", None),
        ("sbiterr", "CASOUTSBITERR", "CASINSBITERR", None),
    ),
    "URAM288": tuple(
        (suffix.lower(), f"CAS_OUT_{suffix}", f"CAS_IN_{suffix}", None)
        for suffix in (
            "ADDR_A", "ADDR_B", "BWE_A", "BWE_B", "DBITERR_A",
            "DBITERR_B", "DIN_A", "DIN_B", "DOUT_A", "DOUT_B",
            "EN_A", "EN_B", "RDACCESS_A", "RDACCESS_B", "RDB_WR_A",
            "RDB_WR_B", "SBITERR_A", "SBITERR_B",
        )
    ),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _select_module(source: Mapping[str, Any], top: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    modules = source.get("modules")
    if not isinstance(modules, dict) or not modules:
        raise ValidationError("mapped Yosys JSON contains no modules")
    if top is not None:
        module = modules.get(top)
        if not isinstance(module, dict):
            raise ValidationError(f"mapped Yosys JSON is missing top {top!r}")
        return top, module
    if len(modules) == 1:
        name, module = next(iter(modules.items()))
        return name, module
    marked = [
        name
        for name, module in modules.items()
        if isinstance(module, dict)
        and str(module.get("attributes", {}).get("top", "0")) not in {"", "0"}
    ]
    if len(marked) != 1:
        raise ValidationError("mapped Yosys JSON top is ambiguous")
    return marked[0], modules[marked[0]]


def _bits(cell: Mapping[str, Any], port: str) -> Tuple[Any, ...]:
    connections = cell.get("connections")
    value = connections.get(port, []) if isinstance(connections, dict) else []
    if not isinstance(value, list):
        raise ValidationError(f"cell port {port!r} has invalid connections")
    return tuple(value)


def _ff_control_set(cell: Mapping[str, Any]) -> str:
    cell_type = cell.get("type")
    control_port = "R" if cell_type in {"FDCE", "FDRE"} else "S"
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


def _net_drivers(cells: Mapping[str, Any]) -> Dict[Any, str]:
    drivers: Dict[Any, str] = {}
    for name, cell in sorted(cells.items()):
        directions = cell.get("port_directions")
        connections = cell.get("connections")
        if not isinstance(directions, dict) or not isinstance(connections, dict):
            raise ValidationError(f"cell {name!r} lacks explicit port metadata")
        for port, direction in directions.items():
            if direction != "output":
                continue
            bits = connections.get(port, [])
            if not isinstance(bits, list):
                raise ValidationError(f"cell {name!r} output {port!r} is invalid")
            for bit in bits:
                if isinstance(bit, int):
                    if bit in drivers:
                        raise ValidationError(f"mapped net bit {bit} has multiple drivers")
                    drivers[bit] = name
    return drivers


def _mux_input_driver(
    cells: Mapping[str, Any], drivers: Mapping[Any, str], name: str, port: str
) -> str:
    bits = _bits(cells[name], port)
    if len(bits) != 1 or bits[0] not in drivers:
        raise ValidationError(f"mux {name!r} port {port!r} has no local driver")
    return drivers[bits[0]]


def _pack_mux_cone(
    root: str, cells: Mapping[str, Any], drivers: Mapping[Any, str]
) -> List[Dict[str, Any]]:
    root_type = cells[root].get("type")
    if root_type == "MUXF7":
        left = _mux_input_driver(cells, drivers, root, "I0")
        right = _mux_input_driver(cells, drivers, root, "I1")
        if {cells[left].get("type"), cells[right].get("type")} - LUT_TYPES:
            raise ValidationError(f"MUXF7 {root!r} is not driven by two LUTs")
        return [
            {"instance": left, "cell_type": cells[left]["type"], "bel": "A6LUT"},
            {"instance": right, "cell_type": cells[right]["type"], "bel": "B6LUT"},
            {"instance": root, "cell_type": "MUXF7", "bel": "F7MUX_AB"},
        ]
    if root_type == "MUXF8":
        lower = _mux_input_driver(cells, drivers, root, "I0")
        upper = _mux_input_driver(cells, drivers, root, "I1")
        if cells[lower].get("type") != "MUXF7" or cells[upper].get("type") != "MUXF7":
            raise ValidationError(f"MUXF8 {root!r} is not driven by two MUXF7 cells")
        assignments = []
        for mux, letters, mux_bel in (
            (lower, ("A", "B"), "F7MUX_AB"),
            (upper, ("C", "D"), "F7MUX_CD"),
        ):
            lut0 = _mux_input_driver(cells, drivers, mux, "I0")
            lut1 = _mux_input_driver(cells, drivers, mux, "I1")
            if {cells[lut0].get("type"), cells[lut1].get("type")} - LUT_TYPES:
                raise ValidationError(f"MUXF7 {mux!r} is not driven by two LUTs")
            assignments.extend([
                {"instance": lut0, "cell_type": cells[lut0]["type"], "bel": f"{letters[0]}6LUT"},
                {"instance": lut1, "cell_type": cells[lut1]["type"], "bel": f"{letters[1]}6LUT"},
                {"instance": mux, "cell_type": "MUXF7", "bel": mux_bel},
            ])
        assignments.append({"instance": root, "cell_type": "MUXF8", "bel": "F8MUX_BOT"})
        return assignments
    raise ValidationError(f"unsupported mux root type {root_type!r}")


def _cascade_signal(cell: Mapping[str, Any], port: str, index: Optional[int]) -> Tuple[Any, ...]:
    bits = _bits(cell, port)
    if not bits:
        return ()
    if index is not None:
        try:
            bits = (bits[index],)
        except IndexError as error:
            raise ValidationError(f"cascade port {port!r} has the wrong width") from error
    # Constants cannot identify a dedicated point-to-point cascade net.
    if not all(isinstance(bit, int) for bit in bits):
        return ()
    return bits


def _derive_cascade_chains(cells: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Derive non-branching dedicated-cascade chains from mapped connectivity."""

    chains: List[Dict[str, Any]] = []
    chain_index = 0
    for cell_type, ports in CASCADE_PORTS.items():
        names = sorted(name for name, cell in cells.items() if cell.get("type") == cell_type)
        output_owners: Dict[Tuple[str, Tuple[Any, ...]], str] = {}
        for name in names:
            for label, output_port, _input_port, index in ports:
                signal = _cascade_signal(cells[name], output_port, index)
                if not signal:
                    continue
                key = (label, signal)
                if key in output_owners:
                    raise ValidationError(
                        f"{cell_type} cascade {label!r} has multiple drivers"
                    )
                output_owners[key] = name

        pair_labels: Dict[Tuple[str, str], set] = defaultdict(set)
        for target in names:
            for label, _output_port, input_port, _index in ports:
                signal = _cascade_signal(cells[target], input_port, None)
                source = output_owners.get((label, signal)) if signal else None
                if source is not None and source != target:
                    pair_labels[(source, target)].add(label)

        successor: Dict[str, str] = {}
        predecessor: Dict[str, str] = {}
        for source, target in sorted(pair_labels):
            if source in successor and successor[source] != target:
                raise ValidationError(f"{cell_type} dedicated cascade branches at {source!r}")
            if target in predecessor and predecessor[target] != source:
                raise ValidationError(f"{cell_type} dedicated cascade merges at {target!r}")
            successor[source] = target
            predecessor[target] = source

        visited = set()
        for head in sorted(set(successor) - set(predecessor)):
            instances = [head]
            links = []
            current = head
            while current in successor:
                target = successor[current]
                if target in instances:
                    raise ValidationError(f"{cell_type} dedicated cascade contains a cycle")
                links.append({
                    "source": current,
                    "target": target,
                    "signals": sorted(pair_labels[(current, target)]),
                })
                instances.append(target)
                visited.add(current)
                current = target
            visited.add(current)
            chains.append({
                "id": f"cascade-{chain_index:06d}",
                "cell_type": cell_type,
                "instances": instances,
                "links": links,
                "placement": "dedicated-adjacent-chain",
            })
            chain_index += 1
        if set(successor) - visited:
            raise ValidationError(f"{cell_type} dedicated cascade has no acyclic head")
    return chains


def pack_xilinx_sites(
    mapped_json: Path,
    output: Path,
    *,
    top: Optional[str] = None,
) -> Dict[str, Any]:
    """Pack normalized cells using conservative exact UltraScale+ rules."""

    audit = audit_xilinx_mapped_json(mapped_json, top=top)
    source = read_json(mapped_json)
    selected_top, module = _select_module(source, top)
    cells = module.get("cells")
    if not isinstance(cells, dict):
        raise ValidationError("mapped Yosys JSON cells are invalid")
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

    # Carry sites remain exclusive in v1.  This is conservative but exactly
    # legal; the normalizer has already merged every connected CARRY4 pair.
    for index, name in enumerate(sorted(name for name, cell in cells.items() if cell.get("type") == "CARRY8")):
        clusters.append({
            "id": f"carry-{index:06d}",
            "kind": "carry",
            "site_templates": ["SLICEL", "SLICEM"],
            "control_set": None,
            "assignments": [{"instance": name, "cell_type": "CARRY8", "bel": "CARRY8"}],
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
            "mapped_json_sha256": _sha256(mapped_json),
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
    validate_xilinx_packing(mapped_json, output, top=selected_top)
    return value


def validate_xilinx_packing(
    mapped_json: Path,
    packed_path: Path,
    *,
    top: Optional[str] = None,
    architecture_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Independently re-check ownership, capacities, BELs, and templates."""

    source = read_json(mapped_json)
    selected_top, module = _select_module(source, top)
    cells = module.get("cells")
    packed = read_json(packed_path)
    if not isinstance(cells, dict) or not isinstance(packed, dict):
        raise ValidationError("packing inputs are invalid")
    if packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA or packed.get("status") != "pass":
        raise ValidationError("PackedSiteNetlist header is invalid")
    if packed.get("top") != selected_top:
        raise ValidationError("PackedSiteNetlist top does not match mapped netlist")
    if packed.get("source", {}).get("mapped_json_sha256") != _sha256(mapped_json):
        raise ValidationError("PackedSiteNetlist source digest is invalid")
    drivers = _net_drivers(cells)

    expected_cascades = _derive_cascade_chains(cells)
    if packed.get("cascade_chains") != expected_cascades:
        raise ValidationError("PackedSiteNetlist dedicated-cascade certificate is invalid")

    template_bels: Dict[str, Dict[str, set]] = {}
    if architecture_path is not None:
        architecture = read_json(architecture_path)
        templates = architecture.get("site_templates") if isinstance(architecture, dict) else None
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
            control_sets = {_ff_control_set(cells[name]) for name in ff_names}
            if len(control_sets) > 1:
                raise ValidationError("slice cluster mixes FF control sets")
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
        elif kind == "carry":
            if [cells[name].get("type") for name in instances] != ["CARRY8"]:
                raise ValidationError("carry cluster is invalid")
        elif kind == "hard":
            if len(instances) != 1 or cells[instances[0]].get("type") not in HARD_BINDINGS:
                raise ValidationError("hard cluster is invalid")
        else:
            raise ValidationError(f"unknown packing cluster kind {kind!r}")

    constants = packed.get("unplaced_constants")
    if not isinstance(constants, list) or any(cells.get(name, {}).get("type") not in CONSTANT_TYPES for name in constants):
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


def exhaustive_minimum_slice_count(items: Iterable[Tuple[int, int]]) -> int:
    """Tiny independent oracle for (LUT, FF) capacity-directed tests."""

    demands = sorted(items, reverse=True)
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
