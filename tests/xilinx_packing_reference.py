"""Independent fixture oracle for native primitive packing and macro checks."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from emuflow.common.errors import ValidationError


FF_TYPES = {"FDCE", "FDPE", "FDRE", "FDSE"}
LUT_TYPES = {f"LUT{width}" for width in range(1, 7)}
DUAL_OUTPUT_LUT_TYPE = "LUT6_2"
CONSTANT_TYPES = {"GND", "VCC"}
MUX_TYPES = {"MUXF7", "MUXF8", "MUXF9"}

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
    projected = getattr(cells, "items_of_types", None)
    source = projected(CASCADE_PORTS) if callable(projected) else cells.items()
    for name, cell in source:
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
