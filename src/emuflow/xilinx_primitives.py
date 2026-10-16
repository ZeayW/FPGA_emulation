"""Fail-closed primitive contract for the Route A UltraScale+ frontend."""

from __future__ import annotations

import copy
import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .errors import ValidationError
from .io import file_sha256, read_json, write_json
from .resources import ResourceVector, classify_primitive_resources


XILINX_PRIMITIVE_LIBRARY_SCHEMA = (
    "emuflow.xilinx-primitive-library-db/v1"
)
XILINX_ULTRASCALEPLUS_OPEN_PROFILE = "xilinx-ultrascaleplus-open-v1"
DEFAULT_XILINX_PRIMITIVE_LIBRARY = (
    Path(__file__).resolve().parent
    / "data"
    / "rapidwright"
    / "xilinx-ultrascaleplus-open-v1.primitives.json"
)
XILINX_NEGEDGE_FF_ALIASES = {
    "FDCE_1": "FDCE",
    "FDPE_1": "FDPE",
    "FDRE_1": "FDRE",
    "FDSE_1": "FDSE",
}
XILINX_NORMALIZATION_INPUT_CELLS = {
    "CARRY4",
    "INV",
    *XILINX_NEGEDGE_FF_ALIASES,
}


class _IndexedCellMapping(MutableMapping[str, Dict[str, Any]]):
    """Dictionary-like view over indexed Yosys cell records.

    The normalizer needs random access only for carry cells and otherwise walks
    one cell at a time. Keeping those records in SQLite prevents a mapped
    design with millions of cells from becoming one graph-sized Python object.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._type_index_ready = False

    def _ensure_type_index(self) -> None:
        if not self._type_index_ready:
            # The canonical type column already belongs to each staging row.
            # Index it once instead of decoding every LUT/FF for each rare
            # primitive rewrite. This index is ephemeral scratch.
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS staging_instances_type "
                "ON staging_instances(type)"
            )
            self._type_index_ready = True

    def cell_types(self) -> set[str]:
        self._ensure_type_index()
        return {
            str(row[0]) for row in self._connection.execute(
                "SELECT DISTINCT type FROM staging_instances"
            )
        }

    def items_of_types(
        self, cell_types: set[str],
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:
        if not cell_types:
            return
        self._ensure_type_index()
        placeholders = ",".join("?" for _ in cell_types)
        rows = self._connection.execute(
            "SELECT id, record_json FROM staging_instances "
            "INDEXED BY staging_instances_type "
            f"WHERE type IN ({placeholders}) ORDER BY id",
            tuple(sorted(cell_types)),
        )
        for row in rows:
            value = json.loads(row[1])
            if not isinstance(value, dict):
                raise ValidationError(f"mapped cell {row[0]!r} is invalid")
            yield str(row[0]), value

    def __getitem__(self, key: str) -> Dict[str, Any]:
        row = self._connection.execute(
            "SELECT record_json FROM staging_instances WHERE id = ?", (key,)
        ).fetchone()
        if row is None:
            raise KeyError(key)
        value = json.loads(row[0])
        if not isinstance(value, dict):
            raise ValidationError(f"mapped cell {key!r} is invalid")
        return value

    def __setitem__(self, key: str, value: Dict[str, Any]) -> None:
        if not isinstance(value, dict):
            raise ValidationError(f"mapped cell {key!r} is invalid")
        cell_type = value.get("type")
        if not isinstance(cell_type, str) or not cell_type:
            raise ValidationError(f"mapped cell {key!r} has no type")
        resources = classify_primitive_resources(cell_type)
        self._connection.execute(
            """
            INSERT OR REPLACE INTO staging_instances
                (id, type, ff, bram, record_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                key,
                cell_type,
                resources.ff,
                resources.bram,
                json.dumps(
                    value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )

    def __delitem__(self, key: str) -> None:
        cursor = self._connection.execute(
            "DELETE FROM staging_instances WHERE id = ?", (key,)
        )
        if cursor.rowcount == 0:
            raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        rows = self._connection.execute(
            "SELECT id FROM staging_instances ORDER BY id"
        )
        for row in rows:
            yield str(row[0])

    def __len__(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM staging_instances"
        ).fetchone()
        return int(row[0])

    def items(self) -> Iterator[Tuple[str, Dict[str, Any]]]:  # type: ignore[override]
        rows = self._connection.execute(
            "SELECT id, record_json FROM staging_instances ORDER BY id"
        )
        for row in rows:
            value = json.loads(row[1])
            if not isinstance(value, dict):
                raise ValidationError(f"mapped cell {row[0]!r} is invalid")
            yield str(row[0]), value

    def values(self) -> Iterator[Dict[str, Any]]:  # type: ignore[override]
        for _, value in self.items():
            yield value


def _items_of_types(
    cells: MutableMapping[str, Dict[str, Any]], cell_types: set[str],
) -> Iterator[Tuple[str, Dict[str, Any]]]:
    if isinstance(cells, _IndexedCellMapping):
        yield from cells.items_of_types(cell_types)
    else:
        for name, cell in cells.items():
            if cell.get("type") in cell_types:
                yield name, cell


def _sha256(path: Path) -> str:
    return file_sha256(path)


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
    output_path: Optional[Path],
    *,
    top: Optional[str] = None,
    library_path: Path = DEFAULT_XILINX_PRIMITIVE_LIBRARY,
    _source_value: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Lower Yosys synthesis macros into the Route A physical namespace.

    Yosys 0.57 emits CARRY4 and INV even for ``-family xcup``.  XCVU19P has
    CARRY8 and LUT BELs instead.  This deterministic pass merges adjacent
    CARRY4 pairs into one SINGLE_CY8 and maps an unpaired CARRY4 to the lower
    half of one DUAL_CY4 CARRY8.  INV is exactly a LUT1 with INIT=2'b01.
    Every CARRY8 DI/S input is physically sourced by the paired O5/O6 outputs
    of one slice LUT.  Materialize one functionally transparent LUT6_2 per
    carry bit so an arbitrary fabric signal is not incorrectly treated as a
    routable CARRY8 site input.  Yosys may also prune a constant LUT leaf from
    a wide-LUT MUXF7 tree and
    connect the constant directly to I0/I1.  UltraScale+ dedicated F7 inputs
    are driven by their adjacent LUT BELs, so materialize those leaves as
    deterministic constant LUT1 cells instead of weakening site legality.
    """

    source = read_json(input_path) if _source_value is None else _source_value
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

    # This artifact can contain millions of mapped cells.  The input is owned
    # by this normalization transaction, so mutate it in place instead of
    # keeping a second complete object graph in memory.
    normalized = source
    module = normalized["modules"][selected_top]
    cells = module.get("cells")
    if not isinstance(cells, MutableMapping):
        raise ValidationError("mapped Yosys JSON cells are invalid")
    _, library = load_xilinx_primitive_library(library_path)
    final_cells = set(library["cells"])
    input_types = (
        cells.cell_types()
        if isinstance(cells, _IndexedCellMapping)
        else {
            cell.get("type")
            for cell in cells.values()
            if isinstance(cell, dict)
        }
    )
    unknown = sorted(
        input_types - final_cells - XILINX_NORMALIZATION_INPUT_CELLS,
        key=str,
    )
    if unknown:
        raise ValidationError(
            "unsupported pre-normalization Xilinx cells: "
            + ", ".join(repr(value) for value in unknown)
        )

    carry_names = []
    carry_output_nets = set()
    for name, cell in _items_of_types(cells, {"CARRY4"}):
        carry_names.append(name)
        connections = cell.get("connections")
        if isinstance(connections, dict):
            carry_output_nets.update(
                bit for port in ("CO", "O")
                for bit in connections.get(port, [])
                if isinstance(bit, int) and not isinstance(bit, bool)
            )
    carry_names.sort()
    net_consumers: Dict[Any, List[Tuple[str, str, int]]] = defaultdict(list)
    max_net = 1
    for consumer_name, cell in cells.items():
        if not isinstance(cell, dict):
            raise ValidationError("mapped Yosys JSON cell is invalid")
        connections = cell.get("connections")
        if not isinstance(connections, dict):
            raise ValidationError("mapped Yosys JSON cell connections are invalid")
        directions = cell.get("port_directions")
        if not isinstance(directions, dict):
            raise ValidationError(
                f"mapped cell {consumer_name!r} has invalid pin metadata"
            )
        for port, bits in connections.items():
            if isinstance(bits, list):
                for bit_index, bit in enumerate(bits):
                    if isinstance(bit, int):
                        max_net = max(max_net, bit)
                    if (
                        isinstance(bit, int)
                        and not isinstance(bit, bool)
                        and bit in carry_output_nets
                        and directions.get(port) in {"input", "inout"}
                    ):
                        net_consumers[bit].append(
                            (consumer_name, str(port), bit_index)
                        )

    def allocate_net() -> int:
        nonlocal max_net
        max_net += 1
        return max_net

    # CARRY8 exposes O[i] and CO[i] through the same general-fabric site exit
    # for bits 0..6.  The primitive may use both values internally, but both
    # cannot independently leave the slice through that one pin.  Record the
    # real consumers before replacing any CARRY4 cells so the normalizer can
    # legalize only output pairs that are simultaneously observable.  A module
    # output is a consumer from the cell's perspective.
    module_ports = module.get("ports", {})
    if module_ports is not None and not isinstance(module_ports, dict):
        raise ValidationError("mapped Yosys JSON ports are invalid")
    for port, definition in (module_ports or {}).items():
        if not isinstance(definition, dict):
            raise ValidationError(f"mapped top port {port!r} is invalid")
        if definition.get("direction") not in {"output", "inout"}:
            continue
        bits = definition.get("bits")
        if not isinstance(bits, list):
            raise ValidationError(f"mapped top port {port!r} bits are invalid")
        for bit_index, bit in enumerate(bits):
            if (
                isinstance(bit, int)
                and not isinstance(bit, bool)
                and bit in carry_output_nets
            ):
                net_consumers[bit].append(("$top", str(port), bit_index))

    def output_connection(
        cell: Mapping[str, Any], port: str, width: int, instance: str
    ) -> List[Any]:
        connections = cell.get("connections")
        value = connections.get(port) if isinstance(connections, dict) else None
        if value is None:
            # Yosys omits a completely unused output port from JSON.  Preserve
            # the physical primitive width with fresh dead nets so the CARRY8
            # contract remains structurally complete and deterministic.
            return [allocate_net() for _ in range(width)]
        if not isinstance(value, list) or len(value) != width:
            raise ValidationError(
                f"mapped cell {instance!r} port {port!r} must have width {width}"
            )
        if any(not isinstance(bit, (int, str)) for bit in value):
            raise ValidationError(
                f"mapped cell {instance!r} port {port!r} is invalid"
            )
        return list(value)

    negedge_ff_count = 0
    negedge_updates: Dict[str, Any] = {}
    for name, cell in _items_of_types(cells, set(XILINX_NEGEDGE_FF_ALIASES)):
        normalized_type = XILINX_NEGEDGE_FF_ALIASES.get(cell.get("type"))
        if normalized_type is None:
            continue
        parameters = cell.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {}
            cell["parameters"] = parameters
        clock_inverted = parameters.get("IS_C_INVERTED")
        if clock_inverted not in (None, "1", "1 "):
            raise ValidationError(
                f"mapped cell {name!r} has conflicting inverted-clock parameter"
            )
        parameters["IS_C_INVERTED"] = "1"
        attributes = cell.get("attributes")
        if not isinstance(attributes, dict):
            attributes = {}
            cell["attributes"] = attributes
        attributes["emuflow_normalized"] = "yosys-negedge-ff-alias-v1"
        cell["type"] = normalized_type
        negedge_updates[name] = cell
        negedge_ff_count += 1
    cells.update(negedge_updates)

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
    carry_output_legalizers = 0
    for name in carry_names:
        if name in removed:
            continue
        first = cells[name]
        first_co = output_connection(first, "CO", 4, name)
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
        o = output_connection(first, "O", 4, name) + [allocate_net() for _ in range(4)]
        if second_name is not None:
            second = cells[second_name]
            carry_type = "SINGLE_CY8"
            di = _connection(first, "DI", 4, name) + _connection(
                second, "DI", 4, second_name
            )
            s = _connection(first, "S", 4, name) + _connection(
                second, "S", 4, second_name
            )
            co = first_co + output_connection(second, "CO", 4, second_name)
            o = output_connection(first, "O", 4, name) + output_connection(
                second, "O", 4, second_name
            )
            removed.add(second_name)
            paired += 1
        else:
            single += 1
        effective_ci = _carry_input(
            first,
            name,
            allocate_net=allocate_net,
            helper_cells=helpers,
        )
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
                "CI": [effective_ci],
                "CI_TOP": ["0"],
                "DI": di,
                "S": s,
                "CO": co,
                "O": o,
            },
        }

        # O[i] and CO[i] share one fabric exit for CARRY8 bits 0..6.  When both
        # values have non-cascade consumers, keep CO on the hard carry and
        # reconstruct the observable O prefix with exact XOR LUTs.  Rewriting
        # the complete prefix is necessary because O[i] depends on CO[i-1]; it
        # also prevents the newly exposed carry-in from colliding with O[i-1].
        # Bit 7 is excluded because CARRY8 has the dedicated COUT exit in
        # addition to HMUX.
        ignored_cascade = (
            {(second_name, "CI", 0)} if second_name is not None else set()
        )

        def externally_used(signal: Any) -> bool:
            return any(
                endpoint not in ignored_cascade
                for endpoint in net_consumers.get(signal, ())
            )

        conflicts = [
            bit_index for bit_index in range(7)
            if externally_used(co[bit_index]) and externally_used(o[bit_index])
        ]
        if conflicts:
            legalize_through = max(conflicts)
            for bit_index in range(legalize_through + 1):
                original_o = o[bit_index]
                replacement[name]["connections"]["O"][bit_index] = allocate_net()
                if not externally_used(original_o):
                    continue
                if bit_index == 0:
                    carry_in = effective_ci
                elif bit_index == 4 and carry_type == "DUAL_CY4":
                    carry_in = "0"
                else:
                    carry_in = co[bit_index - 1]
                helper_name = f"{name}$carry_o_legalizer_{bit_index}"
                helpers[helper_name] = {
                    "hide_name": 1,
                    "type": "LUT2",
                    "parameters": {"INIT": "0110"},
                    "attributes": {
                        "emuflow_normalized": "carry8-shared-exit-xor-v1",
                        "emuflow_carry_owner": name,
                        "emuflow_carry_bit": str(bit_index),
                    },
                    "port_directions": {
                        "I0": "input", "I1": "input", "O": "output",
                    },
                    "connections": {
                        "I0": [s[bit_index]],
                        "I1": [carry_in],
                        "O": [original_o],
                    },
                }
                carry_output_legalizers += 1

        # UltraScale+ exposes CARRY8 DI[i] through the corresponding LUT O5
        # and S[i] through O6; neither is an inter-site sink pin.  A LUT6_2
        # route-through makes that physical contract explicit while preserving
        # both input signals exactly.  I5=1 selects the upper INIT half for O6;
        # O5 always observes the lower half.
        routed_di: List[Any] = []
        routed_s: List[Any] = []
        for bit_index, (di_input, s_input) in enumerate(zip(di, s)):
            di_output = allocate_net()
            s_output = allocate_net()
            helper_name = f"{name}$carry_lut6_2_{bit_index}"
            helpers[helper_name] = {
                "hide_name": 1,
                "type": "LUT6_2",
                "parameters": {"INIT": "1100" * 8 + "10" * 16},
                "attributes": {
                    "emuflow_normalized": "carry8-di-s-lut6-2-v1",
                    "emuflow_carry_owner": name,
                    "emuflow_carry_bit": str(bit_index),
                },
                "port_directions": {
                    "I0": "input", "I1": "input", "I2": "input",
                    "I3": "input", "I4": "input", "I5": "input",
                    "O5": "output", "O6": "output",
                },
                "connections": {
                    "I0": [di_input], "I1": [s_input], "I2": ["0"],
                    "I3": ["0"], "I4": ["0"], "I5": ["1"],
                    "O5": [di_output], "O6": [s_output],
                },
            }
            routed_di.append(di_output)
            routed_s.append(s_output)
        replacement[name]["connections"]["DI"] = routed_di
        replacement[name]["connections"]["S"] = routed_s

    inv_count = 0
    inv_names: List[str] = []
    for name, cell in _items_of_types(cells, {"INV"}):
        if cell.get("type") != "INV":
            continue
        inv_names.append(name)
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

    mux_constant_lut_count = 0
    for name, cell in _items_of_types(cells, {"MUXF7"}):
        if cell.get("type") != "MUXF7":
            continue
        connections = cell.get("connections")
        if not isinstance(connections, dict):
            raise ValidationError(f"mapped cell {name!r} connections are invalid")
        replacement_cell = copy.deepcopy(cell)
        replacement_connections = replacement_cell["connections"]
        changed = False
        for port in ("I0", "I1"):
            bits = _connection(cell, port, 1, name)
            if bits[0] not in {"0", "1"}:
                continue
            output = allocate_net()
            helper_name = f"{name}${port.lower()}_constant_lut"
            helpers[helper_name] = {
                "hide_name": 1,
                "type": "LUT1",
                "parameters": {"INIT": "11" if bits[0] == "1" else "00"},
                "attributes": {
                    "emuflow_normalized": "muxf7-constant-leaf-lut-v1"
                },
                "port_directions": {"I0": "input", "O": "output"},
                "connections": {"I0": ["0"], "O": [output]},
            }
            replacement_connections[port] = [output]
            mux_constant_lut_count += 1
            changed = True
        if changed:
            replacement[name] = replacement_cell

    # Mutate the owned input dictionary instead of building a second
    # multi-million-entry table.  Replacement/helper traversal is
    # deterministic, and Python preserves insertion order.
    for name in removed:
        cells.pop(name, None)
    for name in carry_names:
        cells.pop(name, None)
    for name in inv_names:
        cells.pop(name, None)
    cells.update(replacement)
    cells.update(helpers)
    # Yosys has already emitted cells deterministically, and every helper is
    # appended in a deterministic traversal.  Re-sorting a multi-million-cell
    # dictionary would create another giant key array without adding semantic
    # stability.
    if output_path is not None:
        write_json(output_path, normalized, compact=True, sort_keys=False)
        output_sha256: Optional[str] = _sha256(output_path)
        audit = audit_xilinx_mapped_json(
            output_path,
            top=selected_top,
            library_path=library_path,
            source=normalized,
        )
        representation = "yosys-json"
    else:
        output_sha256 = None
        audit = audit_xilinx_mapped_json(
            input_path,
            top=selected_top,
            library_path=library_path,
            source=normalized,
            hash_path=False,
        )
        representation = "indexed-cell-records"
    return {
        "status": "pass",
        "schema": "emuflow.xilinx-primitive-normalization/v1",
        "top": selected_top,
        "input_sha256": _sha256(input_path) if input_path.is_file() else None,
        "output_sha256": output_sha256,
        "output_representation": representation,
        "carry4_input_cells": len(carry_names),
        "carry8_paired_cells": paired,
        "carry8_single_cells": single,
        "inv_lowered_cells": inv_count,
        "negedge_ff_normalized_cells": negedge_ff_count,
        "mux_constant_lut_cells": mux_constant_lut_count,
        "carry_output_legalizer_lut_cells": carry_output_legalizers,
        "carry_route_through_lut6_2_cells": 8 * (paired + single),
        "helper_lut_cells": len(helpers),
        "primitive_audit": audit,
    }


def audit_xilinx_mapped_json(
    path: Path,
    *,
    top: Optional[str] = None,
    library_path: Path = DEFAULT_XILINX_PRIMITIVE_LIBRARY,
    source: Optional[Mapping[str, Any]] = None,
    hash_path: bool = True,
) -> Dict[str, Any]:
    """Reject every mapped cell outside the declared Route A namespace."""
    source = read_json(path) if source is None else source
    modules = source.get("modules") if isinstance(source, Mapping) else None
    if not isinstance(modules, Mapping) or not modules:
        raise ValidationError("mapped Yosys JSON contains no modules")
    if top is not None:
        module = modules.get(top)
        if not isinstance(module, Mapping):
            raise ValidationError(f"mapped Yosys JSON is missing top {top!r}")
        selected_top = top
    elif len(modules) == 1:
        selected_top, module = next(iter(modules.items()))
    else:
        marked = [
            name
            for name, candidate in modules.items()
            if isinstance(candidate, Mapping)
            and str(candidate.get("attributes", {}).get("top", "0"))
            not in {"", "0"}
        ]
        if len(marked) != 1:
            raise ValidationError("mapped Yosys JSON top is ambiguous")
        selected_top = marked[0]
        module = modules[selected_top]
    cells = module.get("cells")
    if not isinstance(cells, Mapping):
        raise ValidationError("mapped Yosys JSON cells are invalid")

    _, library = load_xilinx_primitive_library(library_path)
    contracts = library["cells"]
    inventory: Counter[str] = Counter()
    hard_blocks: Counter[str] = Counter()
    indexed_audit = getattr(cells, "audit_type_counts", None)
    if callable(indexed_audit):
        inventory.update(indexed_audit())
        for cell_type, count in inventory.items():
            contract = contracts.get(cell_type)
            if contract is None:
                raise ValidationError(f"unsupported Xilinx primitive {cell_type!r}")
            if contract["hard_block"]:
                hard_blocks[cell_type] = count
    for instance, raw_cell in (() if callable(indexed_audit) else cells.items()):
        if not isinstance(raw_cell, Mapping):
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
        if not isinstance(directions, Mapping) or not isinstance(
            connections, Mapping
        ):
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
    # Requirements are constant per primitive type. Do not retain one
    # identical ResourceVector object for every mapped cell.
    resources = ResourceVector.sum(
        ResourceVector.from_mapping(
            {
                field: value * count
                for field, value in contracts[cell_type]["resources"].items()
            },
            f"primitive {cell_type!r} resources",
        )
        for cell_type, count in inventory.items()
    )
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
        "mapped_json_sha256": _sha256(path) if hash_path else None,
        "mapped_representation": (
            "yosys-json" if hash_path else "indexed-cell-records"
        ),
        "primitive_library_sha256": _sha256(library_path),
    }


def normalize_xilinx_indexed_staging(
    connection: sqlite3.Connection,
    raw_ports: Mapping[str, Any],
    input_path: Path,
    top: str,
    *,
    library_path: Path = DEFAULT_XILINX_PRIMITIVE_LIBRARY,
) -> Dict[str, Any]:
    """Normalize one streamed Xilinx module directly in indexed staging.

    Only per-cell records and the comparatively tiny top-port table enter
    Python memory. The complete mapped graph is never reconstructed or emitted
    as a normalized JSON document.
    """

    cells = _IndexedCellMapping(connection)
    source: Mapping[str, Any] = {
        "modules": {
            top: {
                "ports": dict(raw_ports),
                "cells": cells,
            }
        }
    }
    report = normalize_xilinx_mapped_json(
        input_path,
        None,
        top=top,
        library_path=library_path,
        _source_value=source,
    )
    connection.commit()
    return report
