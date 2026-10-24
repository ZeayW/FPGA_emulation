"""Shared UltraScale+ packing legality and dedicated-macro rules."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .errors import ValidationError
from .io import file_sha256, read_json


PACKED_SITE_NETLIST_SCHEMA = "emuflow.packed-site-netlist/v1"
SLICE_LUT_BELS = tuple(f"{letter}6LUT" for letter in "ABCDEFGH")
SLICE_FF_BELS = tuple(
    bel for letter in "ABCDEFGH" for bel in (f"{letter}FF", f"{letter}FF2")
)
FF_TYPES = {"FDCE", "FDPE", "FDRE", "FDSE"}
LUT_TYPES = {f"LUT{width}" for width in range(1, 7)}
DUAL_OUTPUT_LUT_TYPE = "LUT6_2"
CONSTANT_TYPES = {"GND", "VCC"}
MUX_TYPES = {"MUXF7", "MUXF8", "MUXF9"}
SLICE_MUX_SELECT_SITE_PINS = {
    "F7MUX_AB": "AX",
    "F7MUX_CD": "CX",
    "F7MUX_EF": "EX",
    "F7MUX_GH": "GX",
    "F8MUX_BOT": "BX",
    "F8MUX_TOP": "FX",
    "F9MUX": "DX",
}
HARD_BINDINGS = {
    "DSP48E2": (["DSP48E2"], ["DSP_ALU"]),
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
    return file_sha256(path)


def _select_module(source: Mapping[str, Any], top: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    modules = source.get("modules")
    if not isinstance(modules, Mapping) or not modules:
        raise ValidationError("mapped Yosys JSON contains no modules")
    if top is not None:
        module = modules.get(top)
        if not isinstance(module, Mapping):
            raise ValidationError(f"mapped Yosys JSON is missing top {top!r}")
        return top, module
    if len(modules) == 1:
        name, module = next(iter(modules.items()))
        return name, module
    marked = [
        name
        for name, module in modules.items()
        if isinstance(module, Mapping)
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


def _net_drivers(
    cells: Mapping[str, Any], required_bits: Optional[Set[Any]] = None
) -> Dict[Any, str]:
    drivers: Dict[Any, str] = {}
    items_by_id = getattr(cells, "items_by_id", None)
    iterator = items_by_id() if callable(items_by_id) else sorted(cells.items())
    for name, cell in iterator:
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
                if isinstance(bit, int) and (
                    required_bits is None or bit in required_bits
                ):
                    if bit in drivers:
                        raise ValidationError(f"mapped net bit {bit} has multiple drivers")
                    drivers[bit] = name
    return drivers


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
    if root_type == "MUXF9":
        bottom = _mux_input_driver(cells, drivers, root, "I0")
        top = _mux_input_driver(cells, drivers, root, "I1")
        if cells[bottom].get("type") != "MUXF8" or cells[top].get("type") != "MUXF8":
            raise ValidationError(f"MUXF9 {root!r} is not driven by two MUXF8 cells")
        assignments = []
        for mux8, halves, mux8_bel in (
            (bottom, (("A", "B", "F7MUX_AB"), ("C", "D", "F7MUX_CD")), "F8MUX_BOT"),
            (top, (("E", "F", "F7MUX_EF"), ("G", "H", "F7MUX_GH")), "F8MUX_TOP"),
        ):
            mux7s = [
                _mux_input_driver(cells, drivers, mux8, port)
                for port in ("I0", "I1")
            ]
            if any(cells[mux].get("type") != "MUXF7" for mux in mux7s):
                raise ValidationError(f"MUXF8 {mux8!r} is not driven by two MUXF7 cells")
            for mux7, (left_letter, right_letter, mux7_bel) in zip(mux7s, halves):
                luts = [
                    _mux_input_driver(cells, drivers, mux7, port)
                    for port in ("I0", "I1")
                ]
                if any(cells[lut].get("type") not in LUT_TYPES for lut in luts):
                    raise ValidationError(f"MUXF7 {mux7!r} is not driven by two LUTs")
                assignments.extend([
                    {"instance": luts[0], "cell_type": cells[luts[0]]["type"], "bel": f"{left_letter}6LUT"},
                    {"instance": luts[1], "cell_type": cells[luts[1]]["type"], "bel": f"{right_letter}6LUT"},
                    {"instance": mux7, "cell_type": "MUXF7", "bel": mux7_bel},
                ])
            assignments.append({"instance": mux8, "cell_type": "MUXF8", "bel": mux8_bel})
        assignments.append({"instance": root, "cell_type": "MUXF9", "bel": "F9MUX"})
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


def _cascade_cells_by_type(
    cells: Mapping[str, Any],
) -> Dict[str, Dict[str, Mapping[str, Any]]]:
    """Collect only dedicated-cascade cells in one streamed netlist pass."""

    result: Dict[str, Dict[str, Mapping[str, Any]]] = {
        cell_type: {} for cell_type in CASCADE_PORTS
    }
    for name, cell in cells.items():
        if not isinstance(cell, Mapping):
            continue
        cell_type = cell.get("type")
        if cell_type in result:
            result[cell_type][name] = cell
    return result


def _derive_cascade_chains(
    cells: Mapping[str, Any],
    *,
    cells_by_type: Optional[
        Mapping[str, Mapping[str, Mapping[str, Any]]]
    ] = None,
) -> List[Dict[str, Any]]:
    """Derive non-branching dedicated-cascade chains from mapped connectivity."""

    typed = (
        _cascade_cells_by_type(cells)
        if cells_by_type is None else cells_by_type
    )
    chains: List[Dict[str, Any]] = []
    chain_index = 0
    for cell_type, ports in CASCADE_PORTS.items():
        type_cells = typed.get(cell_type, {})
        names = sorted(type_cells)
        output_owners: Dict[Tuple[str, Tuple[Any, ...]], str] = {}
        for name in names:
            for label, output_port, _input_port, index in ports:
                signal = _cascade_signal(type_cells[name], output_port, index)
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
                signal = _cascade_signal(type_cells[target], input_port, None)
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

    source = read_json(mapped_json) if mapped_value is None else mapped_value
    selected_top, module = _select_module(source, top)
    cells = module.get("cells")
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
