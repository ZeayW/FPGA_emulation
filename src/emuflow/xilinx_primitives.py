"""Fail-closed primitive contract for the Route A UltraScale+ frontend."""

from __future__ import annotations

import hashlib
import copy
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .errors import ValidationError
from .io import read_json, write_json
from .resources import ResourceVector, classify_primitive_resources


XILINX_PRIMITIVE_LIBRARY_SCHEMA = (
    "emuflow.xilinx-primitive-library-db/v1"
)
XILINX_ULTRASCALEPLUS_OPEN_PROFILE = "xilinx-ultrascaleplus-open-v1"
DEFAULT_XILINX_PRIMITIVE_LIBRARY = (
    Path(__file__).resolve().parents[2]
    / "resources"
    / "rapidwright"
    / "xilinx-ultrascaleplus-open-v1.primitives.json"
)
XILINX_NORMALIZATION_INPUT_CELLS = {"CARRY4", "INV"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_xilinx_primitive_library(
    value: Mapping[str, Any],
) -> Dict[str, Any]:
    if value.get("schema") != XILINX_PRIMITIVE_LIBRARY_SCHEMA:
        raise ValidationError("Xilinx primitive library schema is invalid")
    if value.get("mapping_profile") != XILINX_ULTRASCALEPLUS_OPEN_PROFILE:
        raise ValidationError("Xilinx primitive mapping profile is invalid")
    if value.get("family") != "UltraScale+":
        raise ValidationError("Xilinx primitive library family is invalid")
    if value.get("unknown_cell_policy") != "fail":
        raise ValidationError("Xilinx primitive library must fail unknown cells")
    cells = value.get("cells")
    if not isinstance(cells, dict) or not cells:
        raise ValidationError("Xilinx primitive library cells are missing")
    normalized: Dict[str, Any] = {}
    for name, contract in sorted(cells.items()):
        if not isinstance(name, str) or not name or not isinstance(contract, dict):
            raise ValidationError("Xilinx primitive cell contract is invalid")
        category = contract.get("category")
        if not isinstance(category, str) or not category:
            raise ValidationError(f"primitive {name!r} category is invalid")
        for field in ("hard_block", "placement_required"):
            if not isinstance(contract.get(field), bool):
                raise ValidationError(
                    f"primitive {name!r} field {field!r} is invalid"
                )
        resources = contract.get("resources")
        if not isinstance(resources, dict):
            raise ValidationError(f"primitive {name!r} resources are invalid")
        resource_vector = ResourceVector.from_mapping(
            resources, f"primitive {name!r} resources"
        )
        classified = classify_primitive_resources(name)
        if resource_vector != classified:
            raise ValidationError(
                f"primitive {name!r} resource contract disagrees with EmuIR"
            )
        normalized[name] = {
            "category": category,
            "hard_block": contract["hard_block"],
            "placement_required": contract["placement_required"],
            "resources": resource_vector.to_dict(include_zeros=False),
        }
    return {
        "status": "pass",
        "schema": XILINX_PRIMITIVE_LIBRARY_SCHEMA,
        "library_id": value.get("library_id"),
        "mapping_profile": XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
        "cells": normalized,
    }


def load_xilinx_primitive_library(
    path: Path = DEFAULT_XILINX_PRIMITIVE_LIBRARY,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValidationError("Xilinx primitive library must be an object")
    return value, validate_xilinx_primitive_library(value)


def _connection(
    cell: Mapping[str, Any], port: str, width: int, instance: str
) -> List[Any]:
    connections = cell.get("connections")
    value = connections.get(port) if isinstance(connections, dict) else None
    if not isinstance(value, list) or len(value) != width:
        raise ValidationError(
            f"mapped cell {instance!r} port {port!r} must have width {width}"
        )
    if any(not isinstance(bit, (int, str)) for bit in value):
        raise ValidationError(
            f"mapped cell {instance!r} port {port!r} is invalid"
        )
    return list(value)


def _carry_input(
    cell: Mapping[str, Any],
    instance: str,
    *,
    allocate_net: Any,
    helper_cells: Dict[str, Any],
) -> Any:
    ci = _connection(cell, "CI", 1, instance)[0]
    cyinit = _connection(cell, "CYINIT", 1, instance)[0]
    if ci == "0":
        return cyinit
    if cyinit == "0":
        return ci
    if ci == "1" or cyinit == "1":
        return "1"
    output = allocate_net()
    helper_name = f"{instance}$cyinit_or"
    helper_cells[helper_name] = {
        "hide_name": 1,
        "type": "LUT2",
        "parameters": {"INIT": "1110"},
        "attributes": {"emuflow_normalized": "carry4-cyinit-or-v1"},
        "port_directions": {"I0": "input", "I1": "input", "O": "output"},
        "connections": {"I0": [ci], "I1": [cyinit], "O": [output]},
    }
    return output


def normalize_xilinx_mapped_json(
    input_path: Path,
    output_path: Path,
    *,
    top: Optional[str] = None,
    library_path: Path = DEFAULT_XILINX_PRIMITIVE_LIBRARY,
) -> Dict[str, Any]:
    """Lower Yosys synthesis macros into the Route A physical namespace.

    Yosys 0.57 emits CARRY4 and INV even for ``-family xcup``.  XCVU19P has
    CARRY8 and LUT BELs instead.  This deterministic pass merges adjacent
    CARRY4 pairs into one SINGLE_CY8 and maps an unpaired CARRY4 to the lower
    half of one DUAL_CY4 CARRY8.  INV is exactly a LUT1 with INIT=2'b01.
    """

    source = read_json(input_path)
    modules = source.get("modules") if isinstance(source, dict) else None
    if not isinstance(modules, dict) or not modules:
        raise ValidationError("mapped Yosys JSON contains no modules")
    if top is not None:
        if top not in modules:
            raise ValidationError(f"mapped Yosys JSON is missing top {top!r}")
        selected_top = top
    elif len(modules) == 1:
        selected_top = next(iter(modules))
    else:
        marked = [
            name
            for name, candidate in modules.items()
            if isinstance(candidate, dict)
            and str(candidate.get("attributes", {}).get("top", "0"))
            not in {"", "0"}
        ]
        if len(marked) != 1:
            raise ValidationError("mapped Yosys JSON top is ambiguous")
        selected_top = marked[0]

    normalized = copy.deepcopy(source)
    module = normalized["modules"][selected_top]
    cells = module.get("cells")
    if not isinstance(cells, dict):
        raise ValidationError("mapped Yosys JSON cells are invalid")
    _, library = load_xilinx_primitive_library(library_path)
    final_cells = set(library["cells"])
    unknown = sorted(
        {
            cell.get("type")
            for cell in cells.values()
            if isinstance(cell, dict)
            and cell.get("type") not in final_cells
            and cell.get("type") not in XILINX_NORMALIZATION_INPUT_CELLS
        },
        key=str,
    )
    if unknown:
        raise ValidationError(
            "unsupported pre-normalization Xilinx cells: "
            + ", ".join(repr(value) for value in unknown)
        )

    max_net = 1
    for cell in cells.values():
        if not isinstance(cell, dict):
            raise ValidationError("mapped Yosys JSON cell is invalid")
        connections = cell.get("connections")
        if not isinstance(connections, dict):
            raise ValidationError("mapped Yosys JSON cell connections are invalid")
        for bits in connections.values():
            if isinstance(bits, list):
                for bit in bits:
                    if isinstance(bit, int):
                        max_net = max(max_net, bit)

    def allocate_net() -> int:
        nonlocal max_net
        max_net += 1
        return max_net

    carry_names = sorted(
        name for name, cell in cells.items() if cell.get("type") == "CARRY4"
    )
    carry_by_ci: Dict[Any, List[str]] = {}
    for name in carry_names:
        ci = _connection(cells[name], "CI", 1, name)[0]
        cyinit = _connection(cells[name], "CYINIT", 1, name)[0]
        if cyinit == "0":
            carry_by_ci.setdefault(ci, []).append(name)

    replacement: Dict[str, Any] = {}
    removed = set()
    paired = 0
    single = 0
    helpers: Dict[str, Any] = {}
    for name in carry_names:
        if name in removed:
            continue
        first = cells[name]
        first_co = _connection(first, "CO", 4, name)
        successors = [
            candidate
            for candidate in carry_by_ci.get(first_co[3], [])
            if candidate not in removed and candidate != name
        ]
        second_name = successors[0] if len(successors) == 1 else None
        carry_type = "DUAL_CY4"
        di = _connection(first, "DI", 4, name) + ["0"] * 4
        s = _connection(first, "S", 4, name) + ["0"] * 4
        co = first_co + [allocate_net() for _ in range(4)]
        o = _connection(first, "O", 4, name) + [allocate_net() for _ in range(4)]
        if second_name is not None:
            second = cells[second_name]
            carry_type = "SINGLE_CY8"
            di = _connection(first, "DI", 4, name) + _connection(
                second, "DI", 4, second_name
            )
            s = _connection(first, "S", 4, name) + _connection(
                second, "S", 4, second_name
            )
            co = first_co + _connection(second, "CO", 4, second_name)
            o = _connection(first, "O", 4, name) + _connection(
                second, "O", 4, second_name
            )
            removed.add(second_name)
            paired += 1
        else:
            single += 1
        replacement[name] = {
            "hide_name": first.get("hide_name", 0),
            "type": "CARRY8",
            "parameters": {"CARRY_TYPE": carry_type},
            "attributes": {
                **(
                    first.get("attributes")
                    if isinstance(first.get("attributes"), dict)
                    else {}
                ),
                "emuflow_normalized": "yosys-carry4-to-carry8-v1",
            },
            "port_directions": {
                "CI": "input",
                "CI_TOP": "input",
                "DI": "input",
                "S": "input",
                "CO": "output",
                "O": "output",
            },
            "connections": {
                "CI": [
                    _carry_input(
                        first,
                        name,
                        allocate_net=allocate_net,
                        helper_cells=helpers,
                    )
                ],
                "CI_TOP": ["0"],
                "DI": di,
                "S": s,
                "CO": co,
                "O": o,
            },
        }

    inv_count = 0
    for name, cell in sorted(cells.items()):
        if cell.get("type") != "INV":
            continue
        replacement[name] = {
            **cell,
            "type": "LUT1",
            "parameters": {
                **(
                    cell.get("parameters")
                    if isinstance(cell.get("parameters"), dict)
                    else {}
                ),
                "INIT": "01",
            },
            "attributes": {
                **(
                    cell.get("attributes")
                    if isinstance(cell.get("attributes"), dict)
                    else {}
                ),
                "emuflow_normalized": "yosys-inv-to-lut1-v1",
            },
            "port_directions": {"I0": "input", "O": "output"},
            "connections": {
                "I0": _connection(cell, "I", 1, name),
                "O": _connection(cell, "O", 1, name),
            },
        }
        inv_count += 1

    final = {
        name: cell
        for name, cell in cells.items()
        if name not in removed and cell.get("type") not in {"CARRY4", "INV"}
    }
    final.update(replacement)
    final.update(helpers)
    module["cells"] = dict(sorted(final.items()))
    write_json(output_path, normalized)
    audit = audit_xilinx_mapped_json(
        output_path, top=selected_top, library_path=library_path
    )
    return {
        "status": "pass",
        "schema": "emuflow.xilinx-primitive-normalization/v1",
        "top": selected_top,
        "input_sha256": _sha256(input_path),
        "output_sha256": _sha256(output_path),
        "carry4_input_cells": len(carry_names),
        "carry8_paired_cells": paired,
        "carry8_single_cells": single,
        "inv_lowered_cells": inv_count,
        "helper_lut_cells": len(helpers),
        "primitive_audit": audit,
    }


def audit_xilinx_mapped_json(
    path: Path,
    *,
    top: Optional[str] = None,
    library_path: Path = DEFAULT_XILINX_PRIMITIVE_LIBRARY,
) -> Dict[str, Any]:
    """Reject every mapped cell outside the declared Route A namespace."""
    source = read_json(path)
    modules = source.get("modules") if isinstance(source, dict) else None
    if not isinstance(modules, dict) or not modules:
        raise ValidationError("mapped Yosys JSON contains no modules")
    if top is not None:
        module = modules.get(top)
        if not isinstance(module, dict):
            raise ValidationError(f"mapped Yosys JSON is missing top {top!r}")
        selected_top = top
    elif len(modules) == 1:
        selected_top, module = next(iter(modules.items()))
    else:
        marked = [
            name
            for name, candidate in modules.items()
            if isinstance(candidate, dict)
            and str(candidate.get("attributes", {}).get("top", "0"))
            not in {"", "0"}
        ]
        if len(marked) != 1:
            raise ValidationError("mapped Yosys JSON top is ambiguous")
        selected_top = marked[0]
        module = modules[selected_top]
    cells = module.get("cells")
    if not isinstance(cells, dict):
        raise ValidationError("mapped Yosys JSON cells are invalid")

    _, library = load_xilinx_primitive_library(library_path)
    contracts = library["cells"]
    inventory: Counter[str] = Counter()
    hard_blocks: Counter[str] = Counter()
    totals = []
    for instance, raw_cell in sorted(cells.items()):
        if not isinstance(raw_cell, dict):
            raise ValidationError(f"mapped cell {instance!r} is invalid")
        cell_type = raw_cell.get("type")
        if not isinstance(cell_type, str) or not cell_type:
            raise ValidationError(f"mapped cell {instance!r} has no type")
        contract = contracts.get(cell_type)
        if contract is None:
            raise ValidationError(
                f"unsupported Xilinx primitive {cell_type!r} at {instance!r}"
            )
        directions = raw_cell.get("port_directions")
        connections = raw_cell.get("connections")
        if not isinstance(directions, dict) or not isinstance(connections, dict):
            raise ValidationError(
                f"mapped cell {instance!r} lacks explicit port metadata"
            )
        unknown_directions = sorted(
            direction
            for direction in directions.values()
            if direction not in {"input", "output", "inout"}
        )
        if unknown_directions:
            raise ValidationError(
                f"mapped cell {instance!r} has invalid port directions"
            )
        inventory[cell_type] += 1
        if contract["hard_block"]:
            hard_blocks[cell_type] += 1
        totals.append(
            ResourceVector.from_mapping(
                contract["resources"], f"primitive {cell_type!r} resources"
            )
        )
    resources = ResourceVector.sum(totals)
    return {
        "status": "pass",
        "schema": "emuflow.xilinx-primitive-audit/v1",
        "mapping_profile": XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
        "top": selected_top,
        "instances": sum(inventory.values()),
        "cell_types": dict(sorted(inventory.items())),
        "hard_block_cells": dict(sorted(hard_blocks.items())),
        "resource_totals": resources.to_dict(include_zeros=False),
        "unknown_cell_types": {},
        "external_macros": {},
        "mapped_json_sha256": _sha256(path),
        "primitive_library_sha256": _sha256(library_path),
    }
