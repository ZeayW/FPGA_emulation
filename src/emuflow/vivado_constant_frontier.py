"""Bounded functional checks for a vendor's conditional constant selection.

An absent register and a ground pin with the same spelling do not establish
this fact. Compare the entire retained LUT function, including its INIT and
the provenance of every dynamic input, so pin permutations cannot masquerade
as evidence of a constant choice. This module assigns no physical delay.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import csv
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from .errors import ValidationError


def _init_bits(value: str, width: int) -> int:
    if not isinstance(value, str):
        raise ValidationError("constant-frontier LUT INIT is invalid")
    native = re.fullmatch(r"([0-9]+)'([hHbB])([0-9a-fA-F]+)", value)
    if native:
        if int(native[1]) != 1 << width:
            raise ValidationError("constant-frontier LUT INIT width differs")
        try:
            result = int(native[3], 16 if native[2].lower() == "h" else 2)
        except ValueError as error:
            raise ValidationError("constant-frontier LUT INIT is invalid") from error
    elif len(value) == 1 << width and set(value) <= {"0", "1"}:
        result = int(value, 2)
    else:
        raise ValidationError("constant-frontier LUT INIT is invalid")
    if result >= 1 << (1 << width):
        raise ValidationError("constant-frontier LUT INIT exceeds its width")
    return result


def _lut(function: Mapping[str, Any]):
    kind = function.get("type")
    if kind not in {f"LUT{i}" for i in range(1, 7)}:
        raise ValidationError("constant-frontier check requires a single-output LUT")
    width = int(kind[-1])
    pins = function.get("inputs")
    if not isinstance(pins, Mapping) or set(pins) != {f"I{i}" for i in range(width)}:
        raise ValidationError("constant-frontier LUT input coverage differs")
    if any(isinstance(x, bool) or not (isinstance(x, int) and x in (0, 1)
                                      or isinstance(x, str) and x) for x in pins.values()):
        raise ValidationError("constant-frontier LUT input identity is invalid")
    return width, _init_bits(function.get("init"), width), pins


def require_equal_frontier_functions(source: Mapping[str, Any], native: Mapping[str, Any]) -> dict[str, int]:
    """Exhaust all assignments to at most twelve primitive input variables.

    Variable strings must already identify checked source-origin output pins
    or explicitly unrelated native objects. They are never inferred from a
    coincidentally matching physical input pin name.
    """
    left, right = _lut(source), _lut(native)
    variables = sorted({value for _, _, pins in (left, right) for value in pins.values()
                        if isinstance(value, str)})
    if len(variables) > 12:
        raise ValidationError("constant-frontier function exceeds its bounded primitive scope")
    indices = {name: i for i, name in enumerate(variables)}

    def value(function, assignment):
        width, init, pins = function
        index = 0
        for i in range(width):
            binding = pins[f"I{i}"]
            bit = binding if isinstance(binding, int) else assignment >> indices[binding] & 1
            index |= bit << i
        return init >> index & 1

    for assignment in range(1 << len(variables)):
        if value(left, assignment) != value(right, assignment):
            raise ValidationError("native frontier function does not confirm the source constant selection")
    return {"variables": len(variables), "assignments": 1 << len(variables)}


def compare_selected_constant_frontiers(mapped_path: Path, functions_path: Path,
                                        candidates_path: Path, observations_path: Path, *,
                                        mapped_sha256: str, expected_objects) -> dict[str, Any]:
    """Compare selected native observations with the conditional source model.

    This diagnostic does not authorize eliminating a source: the caller still
    needs complete source fanout coverage, initialization qualification, and a
    bound implementation-stage proof before publishing such a disposition.
    Only one source/native LUT pair is reconstructed at a time.
    """
    from .xilinx_netlist import open_xilinx_mapped_index
    if (not isinstance(expected_objects, Sequence) or isinstance(expected_objects, (str, bytes))
            or not 1 <= len(expected_objects) <= 4096
            or any(not isinstance(item, Mapping) or not isinstance(item.get("source_origin"), str)
                   or not isinstance(item.get("name"), str) for item in expected_objects)):
        raise ValidationError("constant-frontier diagnostic requires at most 4096 selected objects")
    expected = {(item["source_origin"], item["name"]) for item in expected_objects}
    if not expected or len(expected) != len(expected_objects):
        raise ValidationError("constant-frontier expected object set is empty or duplicated")
    db = sqlite3.connect(f"file:{mapped_path.resolve()}?mode=ro&immutable=1", uri=True)
    try:
        db.execute("ATTACH DATABASE ? AS functions", (f"file:{functions_path.resolve()}?mode=ro&immutable=1",))
        db.execute("ATTACH DATABASE ? AS proofs", (f"file:{candidates_path.resolve()}?mode=ro&immutable=1",))
        owner = dict(db.execute("SELECT key,value FROM proofs.metadata"))
        if (json.loads(owner.get("mapped_index_sha256", "null")) != mapped_sha256 or
                db.execute("SELECT value FROM functions.metadata WHERE key='mapped_index_sha256'").fetchone() != (mapped_sha256,)):
            raise ValidationError("constant-frontier inputs belong to different mapped sources")

        def canonical_variable(origin, pin):
            row = db.execute("SELECT MIN(e.seq) FROM functions.signatures f JOIN functions.signatures e ON e.signature=f.signature WHERE f.seq=?", (origin,)).fetchone()
            if row is None or row[0] is None:
                raise ValidationError("constant-frontier dynamic source lacks a function signature")
            return f"source:s{row[0]}/{pin}"

        def source_binding(bit):
            if isinstance(bit, str) and bit in {"0", "1"}:
                return int(bit)
            if not isinstance(bit, int):
                raise ValidationError("constant-frontier source input is unknown")
            drivers = db.execute("SELECT cell_seq,port,port_bit FROM cell_pins WHERE net_bit=? AND direction=1 AND value_kind=0 LIMIT 2", (bit,)).fetchall()
            if len(drivers) != 1:
                return f"source-net:{bit}"
            origin, pin, pin_bit = drivers[0]
            constant = db.execute("SELECT constant_value FROM proofs.constant_candidates WHERE origin=? UNION ALL SELECT constant_value FROM proofs.eliminated_sources WHERE origin=? AND reason='constant-state'", (origin,origin)).fetchone()
            if constant is not None and pin == "Q" and pin_bit == 0:
                return int(constant[0])
            return canonical_variable(origin, pin if pin_bit == 0 else f"{pin}[{pin_bit}]")

        def native_binding(row):
            kind = row["driver_kind"]
            if kind in {"zero", "one"}:
                return int(kind == "one")
            name = bytes.fromhex(row["driver_name_hex"]).decode()
            if kind == "opaque":
                return "native-opaque:" + name
            if kind != "source" or not re.fullmatch(r"s(?:0|[1-9][0-9]*)", row["driver_source"]):
                raise ValidationError("constant-frontier native driver source is invalid")
            origin = int(row["driver_source"][1:])
            source_type = db.execute("SELECT type FROM cells WHERE seq=?", (origin,)).fetchone()
            changes = bytes.fromhex(row["driver_transformations_hex"]).decode().upper()
            if (source_type != (row["driver_ref"],)
                    or any(x in changes for x in ("RETIM", "REGISTER_OPT", "AUTOPIPELINE", "INSERT_NEGEDGE", "SHIFT_REGISTER_TO_PIPELINE"))
                    or (row["driver_ref"].startswith("FD") and "MISC_OPT" in changes)):
                raise ValidationError("constant-frontier dynamic source has an unsupported transformation")
            pin = row["driver_pin"]
            match = re.fullmatch(r"(.+)\[([0-9]+)\]", pin)
            port, bit = (match[1], int(match[2])) if match else (pin, 0)
            if db.execute("SELECT 1 FROM cell_pins WHERE cell_seq=? AND port=? AND port_bit=? AND direction=1", (origin,port,bit)).fetchone() is None:
                raise ValidationError("constant-frontier driver is not a source output pin")
            return canonical_variable(origin, port if bit == 0 else f"{port}[{bit}]")

        checked = []
        seen = set()
        with open_xilinx_mapped_index(mapped_path) as mapped, observations_path.open(newline="") as stream:
            rows = csv.DictReader(stream, delimiter="\t")
            columns = {"source_origin","native_cell_hex","ref","init","pin","driver_kind","driver_source","driver_ref","driver_pin","driver_name_hex","driver_transformations_hex","transformations_hex"}
            if rows.fieldnames is None or set(rows.fieldnames) != columns or len(rows.fieldnames) != len(columns):
                raise ValidationError("constant-frontier observation header is invalid")
            current, group = None, []

            def compare(key, group):
                if key not in expected or key in seen or not re.fullmatch(r"s(?:0|[1-9][0-9]*)", key[0]):
                    raise ValidationError("constant-frontier observation object coverage differs")
                seen.add(key)
                source_name = db.execute("SELECT id FROM cells WHERE seq=?", (int(key[0][1:]),)).fetchone()
                if source_name is None:
                    raise ValidationError("constant-frontier source object is unknown")
                cell = mapped.cells[source_name[0]]
                for row in group:
                    if any(row[field] != group[0][field] for field in ("ref","init","transformations_hex")):
                        raise ValidationError("constant-frontier primitive facts disagree")
                if len({row["pin"] for row in group}) != len(group):
                    raise ValidationError("constant-frontier input pin is duplicated")
                source_inputs = {}
                for pin, direction in cell["port_directions"].items():
                    if direction == "input":
                        if len(cell["connections"][pin]) != 1:
                            raise ValidationError("constant-frontier source LUT input is not scalar")
                        source_inputs[pin] = source_binding(cell["connections"][pin][0])
                source = {"type":cell["type"], "init":cell["parameters"].get("INIT"), "inputs":source_inputs}
                native = {"type":group[0]["ref"], "init":group[0]["init"], "inputs":{row["pin"]:native_binding(row) for row in group}}
                result = require_equal_frontier_functions(source, native)
                checked.append({"source_origin":key[0], "native_cell":key[1], **result})

            for row in rows:
                if set(row) != columns or any(value is None for value in row.values()):
                    raise ValidationError("constant-frontier observation row is malformed")
                key = (row["source_origin"], bytes.fromhex(row["native_cell_hex"]).decode())
                if current is not None and current != key:
                    compare(current, group)
                    group = []
                current = key
                group.append(row)
                if len(group) > 6:
                    raise ValidationError("constant-frontier primitive exceeds its bounded input scope")
            if current is not None:
                compare(current, group)
        if seen != expected:
            raise ValidationError("constant-frontier observation object coverage is incomplete")
        return {"status":"pass", "scope":"selected-conditional-source-frontier-function-comparison-only",
                "mapped_index_sha256":mapped_sha256, "frontiers":checked}
    except (ValueError, UnicodeError, OverflowError, sqlite3.DatabaseError) as error:
        raise ValidationError("constant-frontier observation or source index is malformed") from error
    finally:
        db.close()
