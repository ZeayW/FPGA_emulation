"""Normalize VTR-oriented Yosys JSON into hard-macro-aware EmuIR input."""

from __future__ import annotations

import copy
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, DefaultDict, Dict, List, Mapping, Tuple

from .errors import ValidationError
from .io import read_json, write_json


VTR_MEMORY_ATOM = re.compile(
    r"^(?P<prefix>.+)\.bits\[(?P<bit>[0-9]+)\]\.bit_cell$"
)
VTR_MEMORY_TYPES = {"single_port_ram", "dual_port_ram"}


def _require_equal(
    cells: List[Tuple[int, str, Mapping[str, Any]]],
    port: str,
) -> List[Any]:
    reference = cells[0][2]["connections"].get(port)
    if not isinstance(reference, list):
        raise ValidationError(
            f"VTR memory atom {cells[0][1]!r} lacks port {port!r}"
        )
    for _, name, cell in cells[1:]:
        if cell["connections"].get(port) != reference:
            raise ValidationError(
                f"VTR memory atoms disagree on shared port {port!r}: "
                f"{cells[0][1]!r}, {name!r}"
            )
    return list(reference)


def normalize_vtr_hard_block_json(
    input_path: Path,
    output_path: Path,
    *,
    top: str,
) -> Dict[str, Any]:
    value = read_json(input_path)
    modules = value.get("modules")
    if not isinstance(modules, dict) or top not in modules:
        raise ValidationError(
            f"VTR Yosys JSON does not contain top module {top!r}"
        )
    normalized = copy.deepcopy(value)
    module = normalized["modules"][top]
    cells = module.get("cells")
    if not isinstance(cells, dict):
        raise ValidationError(f"VTR Yosys top {top!r} has no cells object")

    memory_groups: DefaultDict[
        Tuple[str, str], List[Tuple[int, str, Mapping[str, Any]]]
    ] = defaultdict(list)
    for name, cell in sorted(cells.items()):
        if not isinstance(cell, dict) or cell.get("type") not in VTR_MEMORY_TYPES:
            continue
        match = VTR_MEMORY_ATOM.fullmatch(name)
        if match is None:
            raise ValidationError(
                f"VTR memory atom name cannot be grouped: {name!r}"
            )
        memory_groups[(match.group("prefix"), cell["type"])].append(
            (int(match.group("bit")), name, cell)
        )

    removed = set()
    grouped_cells: Dict[str, Dict[str, Any]] = {}
    atom_count = 0
    compacted_memory_groups = 0
    for (prefix, atom_type), raw_group in sorted(memory_groups.items()):
        group = sorted(raw_group)
        indices = [item[0] for item in group]
        if len(indices) != len(set(indices)):
            raise ValidationError(
                f"VTR memory {prefix!r} has duplicate bit atoms {indices}"
            )
        # Yosys may delete unused or constant memory bit slices while the
        # surviving one-bit VTR atoms retain their original indices (for
        # example, bits[2]..bits[15]).  The index is only a grouping/ordering
        # key: these atoms are independent one-bit memories sharing address
        # and control ports.  Compacting the live atoms into a narrower word
        # macro preserves all observable behavior and the physical BRAM count.
        if indices != list(range(len(group))):
            compacted_memory_groups += 1
        for _, name, _ in group:
            removed.add(name)
        atom_count += len(group)
        shared = {
            "clk": _require_equal(group, "clk"),
        }
        if atom_type == "single_port_ram":
            shared.update(
                {
                    "addr": _require_equal(group, "addr"),
                    "we": _require_equal(group, "we"),
                }
            )
            data = [
                item[2]["connections"]["data"][0] for item in group
            ]
            output = [
                item[2]["connections"]["out"][0] for item in group
            ]
            connections = {
                **shared,
                "data": data,
                "out": output,
            }
            directions = {
                "clk": "input",
                "addr": "input",
                "we": "input",
                "data": "input",
                "out": "output",
            }
            cell_type = "VTR_SP_RAM"
            address_width = len(shared["addr"])
        else:
            shared.update(
                {
                    "addr1": _require_equal(group, "addr1"),
                    "addr2": _require_equal(group, "addr2"),
                    "we1": _require_equal(group, "we1"),
                    "we2": _require_equal(group, "we2"),
                }
            )
            connections = {
                **shared,
                "data1": [
                    item[2]["connections"]["data1"][0] for item in group
                ],
                "data2": [
                    item[2]["connections"]["data2"][0] for item in group
                ],
                "out1": [
                    item[2]["connections"]["out1"][0] for item in group
                ],
                "out2": [
                    item[2]["connections"]["out2"][0] for item in group
                ],
            }
            directions = {
                port: (
                    "output" if port.startswith("out") else "input"
                )
                for port in connections
            }
            cell_type = "VTR_DP_RAM"
            address_width = len(shared["addr1"])
        macro_name = f"{prefix}.memory_macro"
        if macro_name in cells or macro_name in grouped_cells:
            raise ValidationError(
                f"VTR memory macro name collides: {macro_name!r}"
            )
        grouped_cells[macro_name] = {
            "hide_name": 0,
            "type": cell_type,
            "parameters": {
                "ADDR_WIDTH": address_width,
                "DATA_WIDTH": len(group),
                "DEPTH": 1 << address_width,
                "READ_DURING_WRITE": "old",
            },
            "attributes": {
                "emuflow_hard_macro": "1",
                "emuflow_vtr_atom_count": str(len(group)),
                "emuflow_vtr_atom_indices": ",".join(map(str, indices)),
                "emuflow_vtr_atoms": ",".join(
                    item[1] for item in group
                ),
            },
            "port_directions": directions,
            "connections": connections,
        }

    result_cells = {
        name: cell for name, cell in cells.items() if name not in removed
    }
    multiplier_count = 0
    for name, cell in result_cells.items():
        if cell.get("type") != "multiply":
            continue
        connections = cell.get("connections", {})
        cell["type"] = "VTR_MULTIPLY"
        cell["port_directions"] = {
            "a": "input",
            "b": "input",
            "out": "output",
        }
        cell["parameters"] = {
            "A_WIDTH": len(connections.get("a", [])),
            "B_WIDTH": len(connections.get("b", [])),
            "Y_WIDTH": len(connections.get("out", [])),
        }
        attributes = cell.setdefault("attributes", {})
        attributes["emuflow_hard_macro"] = "1"
        multiplier_count += 1
    result_cells.update(grouped_cells)
    module["cells"] = dict(sorted(result_cells.items()))
    write_json(output_path, normalized)
    return {
        "status": "pass",
        "provider": "vtr-hard-block-json-normalizer-v1",
        "top": top,
        "input_cells": len(cells),
        "output_cells": len(result_cells),
        "multiplier_macros": multiplier_count,
        "memory_macros": len(grouped_cells),
        "memory_atoms_collapsed": atom_count,
        "memory_compacted_groups": compacted_memory_groups,
        "output": str(output_path),
    }
