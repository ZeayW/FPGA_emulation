"""Bounded functional checks for a vendor's conditional constant selection.

An absent register and a ground pin with the same spelling do not establish
this fact. Compare the entire retained LUT function, including its INIT and
the provenance of every dynamic input, so pin permutations cannot masquerade
as evidence of a constant choice. This module assigns no physical delay.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import csv
import hashlib
from functools import lru_cache
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from .errors import ValidationError

CONSTANT_CONFIRMATION_SCHEMA = "emuflow.vivado-selected-constant-confirmations/v2"
CONSTANT_INITIALIZATION_POLICY = "joint-inductive-source-model-with-explicit-vendor-selected-initialization"
CONSTANT_MODEL_SCOPE = "selected-states-inductive-without-unconfirmed-state-assumptions"
CONSTANT_NATIVE_INPUT_POLICY = "same-primitive-and-pin-bindings-at-synthesis"
_CHECKED_CONSTANT_TOKEN = object()


class CheckedConstantSources(Mapping[str, Any]):
    """Immutable in-process result of the source/native semantic checks.

    A saved JSON status report is not this interface. A consumer importing
    external evidence must run the checker against its bound source first.
    """
    def __init__(self, document, *, _token=None):
        if _token is not _CHECKED_CONSTANT_TOKEN:
            raise ValidationError("constant sources must be created by the semantic checker")
        self._canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def to_document(self):
        return json.loads(self._canonical)

    def __getitem__(self, key):
        return self.to_document()[key]

    def __iter__(self):
        return iter(self.to_document())

    def __len__(self):
        return len(self.to_document())


def check_constant_sources(*args, **kwargs) -> CheckedConstantSources:
    return CheckedConstantSources(confirm_selected_constant_sources(*args, **kwargs), _token=_CHECKED_CONSTANT_TOKEN)


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
                                        mapped_sha256: str, expected_objects,
                                        selected_constant_values=None,
                                        require_input_identity: bool = False) -> dict[str, Any]:
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
            if selected_constant_values is None:
                # Hypothetical-model comparison is diagnostic only. A proof
                # must pass its independently checked, explicitly selected set.
                constant = db.execute("SELECT constant_value FROM proofs.constant_candidates WHERE origin=? UNION ALL SELECT constant_value FROM proofs.eliminated_sources WHERE origin=? AND reason='constant-state' AND initialization='specified'", (origin,origin)).fetchone()
            elif origin in selected_constant_values:
                constant = (selected_constant_values[origin],)
            else:
                constant = db.execute("SELECT constant_value FROM proofs.eliminated_sources WHERE origin=? AND reason='constant-state' AND initialization='specified'", (origin,)).fetchone()
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
                if require_input_identity:
                    left, right = _lut(source), _lut(native)
                    if left != right:
                        raise ValidationError("native frontier primitive or pin bindings do not prove the selected source projection")
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


def _require_inductive_selected_states(db, mapped_path: Path, values: Mapping[int, int]):
    """Prove the selected joint states without assuming other INIT choices."""
    from .vivado_optimization import _RESET, _parameter_bit, _lut_value
    from .xilinx_netlist import open_xilinx_mapped_index
    with open_xilinx_mapped_index(mapped_path) as mapped:
        @lru_cache(maxsize=256)
        def primitive(name):
            return mapped.cells[name]

        visiting = set()

        @lru_cache(maxsize=4096)
        def evaluate(bit, depth=0):
            if isinstance(bit,str) and bit in {"0","1"}:
                return int(bit)
            if not isinstance(bit,int) or depth >= 64 or bit in visiting:
                return None
            drivers = db.execute("SELECT cell_seq,cell_id,port FROM cell_pins WHERE net_bit=? AND direction=1 AND value_kind=0 LIMIT 2", (bit,)).fetchall()
            if len(drivers) != 1:
                return None
            origin, name, output = drivers[0]
            if origin in values and output == "Q":
                return values[origin]
            strict = db.execute("SELECT constant_value FROM proofs.eliminated_sources WHERE origin=? AND reason='constant-state' AND initialization='specified'", (origin,)).fetchone()
            cell = primitive(name)
            if strict is not None and output == "Q":
                if cell["type"] not in _RESET:
                    raise ValidationError("strict constant source is not a supported state primitive")
                initial = cell.get("parameters",{}).get("INIT",str(_RESET[cell["type"]][1]))
                if initial not in {"0","1"} or int(initial) != strict[0]:
                    raise ValidationError("strict source initial value differs from its constant proof")
                return int(strict[0])
            if not (cell["type"].startswith("LUT") or cell["type"] in {"MUXF7","MUXF8","MUXF9"}):
                return None
            visiting.add(bit)
            try:
                pins = {pin:evaluate(bits[0],depth+1) for pin,bits in cell["connections"].items()
                        if cell["port_directions"][pin] == "input" and len(bits)==1}
                if cell["type"].startswith("LUT"):
                    return _lut_value(cell,output,pins)
                select = pins.get("S")
                return pins.get("I"+str(select)) if select is not None else pins.get("I0") if pins.get("I0") == pins.get("I1") else None
            finally:
                visiting.remove(bit)

        for origin,value in values.items():
            row = db.execute("SELECT id FROM cells WHERE seq=?", (origin,)).fetchone()
            if row is None:
                raise ValidationError("selected constant source is absent from its mapped owner")
            cell = primitive(row[0])
            if cell["type"] not in _RESET:
                raise ValidationError("selected constant source is not a supported state primitive")
            reset, reset_value = _RESET[cell["type"]]
            initial = cell.get("parameters",{}).get("INIT",str(reset_value))
            if initial not in {"0","1","x","X"} or (initial in {"0","1"} and int(initial) != value):
                raise ValidationError("selected constant source initial value is not a legal choice")
            def pin_value(pin):
                bits = cell["connections"].get(pin,[])
                return evaluate(bits[0]) if len(bits)==1 else None
            data, enable, control = pin_value("D"), pin_value("CE"), pin_value(reset)
            inversions = [_parameter_bit(cell,"IS_"+pin+"_INVERTED") for pin in ("D","CE",reset)]
            if any(v is None for v in inversions):
                stable = False
            else:
                data = None if data is None else data ^ inversions[0]
                enable = None if enable is None else enable ^ inversions[1]
                control = None if control is None else control ^ inversions[2]
                stable = ((value == reset_value) if control == 1 else
                          (control == 0 or value == reset_value) and (enable == 0 or data == value))
            if not stable:
                raise ValidationError("selected source states are not independently inductive; unconfirmed state assumptions remain")


def confirm_selected_constant_sources(mapped_path: Path, functions_path: Path,
                                      candidates_path: Path, observations_path: Path,
                                      inventory_path: Path, *, mapped_sha256: str,
                                      candidate_proofs_sha256: str, checkpoint_sha256: str,
                                      fpga: str, selected_sources) -> dict[str, Any]:
    """Check every observable use of selected conditional constant states.

    The source model chooses a legal joint initialization for unspecified
    states. This certificate confirms that replacing the selected absent state
    outputs by those constants agrees with every retained direct LUT frontier.
    It is a vendor refinement under that explicit initialization policy, not
    equivalence for every possible initial state. Unsupported fanouts remain
    unresolved. A routed consumer must additionally bind the synthesized DCP.
    """
    from .io import file_sha256
    from .vivado_lineage import INVENTORY_HEADER
    if (not isinstance(fpga, str) or not fpga or not isinstance(selected_sources, Sequence)
            or isinstance(selected_sources, (str, bytes)) or not 1 <= len(selected_sources) <= 4096
            or any(not isinstance(s, str) or not re.fullmatch(r"s(?:0|[1-9][0-9]*)", s) for s in selected_sources)
            or len(set(selected_sources)) != len(selected_sources)):
        raise ValidationError("constant confirmation needs bounded unique source identities and an FPGA")
    for value in (mapped_sha256, candidate_proofs_sha256, checkpoint_sha256):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValidationError("constant confirmation input digest is invalid")
    if file_sha256(candidates_path) != candidate_proofs_sha256:
        raise ValidationError("constant confirmation candidate proof artifact differs")
    db = sqlite3.connect(f"file:{mapped_path.resolve()}?mode=ro&immutable=1", uri=True)
    try:
        db.execute("ATTACH DATABASE ? AS proofs", (f"file:{candidates_path.resolve()}?mode=ro&immutable=1",))
        owner = {key: json.loads(value) for key, value in db.execute("SELECT key,value FROM proofs.metadata")}
        if (owner.get("stage") != "synthesized" or owner.get("mapped_index_sha256") != mapped_sha256
                or owner.get("fpga") not in (None, fpga)):
            raise ValidationError("constant confirmation needs the matching synthesized source model")
        source_rows = []
        frontier_origins = set()
        selected_q = set()
        for identity in selected_sources:
            origin = int(identity[1:])
            candidate = db.execute("SELECT constant_value,initialization FROM proofs.constant_candidates WHERE origin=?", (origin,)).fetchone()
            outputs = db.execute("SELECT port,port_bit,net_bit,value_kind FROM cell_pins WHERE cell_seq=? AND direction=1", (origin,)).fetchall()
            if (candidate is None or candidate[0] not in (0, 1)
                    or candidate[1] not in {"specified", "vendor-dont-care-selection"}
                    or len(outputs) != 1 or outputs[0][:2] != ("Q", 0) or outputs[0][3] != 0):
                raise ValidationError("selected source is not a supported conditional constant state")
            q = outputs[0][2]
            selected_q.add(q)
            fanouts = []
            for row in db.execute("SELECT s.cell_seq,c.type,s.port,s.port_bit FROM cell_pins s JOIN cells c ON c.seq=s.cell_seq WHERE s.net_bit=? AND s.value_kind=0 AND s.direction=0 ORDER BY s.cell_seq,s.port,s.port_bit", (q,)):
                if row[1] not in {f"LUT{i}" for i in range(1, 7)} or not re.fullmatch(r"I[0-5]", row[2]) or row[3] != 0:
                    raise ValidationError("conditional source has an unsupported observable fanout")
                fanouts.append(row[0])
                if len(fanouts) > 4096:
                    raise ValidationError("conditional source fanout exceeds diagnostic scope")
            if not fanouts:
                raise ValidationError("conditional source lacks an observed functional frontier")
            frontiers = sorted(set(fanouts))
            frontier_origins.update(frontiers)
            if len(frontier_origins) > 4096:
                raise ValidationError("constant confirmation frontier exceeds diagnostic scope")
            source_rows.append({"source":identity, "constant_value":int(candidate[0]),
                                "source_initialization":candidate[1],
                                "frontier_sources":[f"s{i}" for i in frontiers]})
        ports = json.loads(db.execute("SELECT value FROM metadata WHERE key='ports_json'").fetchone()[0])
        if any(bit in selected_q for port in ports.values() if port["direction"] == "output"
               for bit in port["bits"] if isinstance(bit, int)):
            raise ValidationError("conditional source directly reaches an unconfirmed output port")
        selected_values = {int(row["source"][1:]):row["constant_value"] for row in source_rows}
        _require_inductive_selected_states(db,mapped_path,selected_values)
        expected_objects = []
        selected_origins = {int(s[1:]) for s in selected_sources}
        inventory_digest = hashlib.sha256()
        with inventory_path.open("rb") as stream:
            def lines():
                for raw in stream:
                    inventory_digest.update(raw)
                    yield raw.decode("utf-8")
            rows = csv.reader(lines(), delimiter="\t", quoting=csv.QUOTE_NONE)
            if next(rows, None) != INVENTORY_HEADER:
                raise ValidationError("constant confirmation inventory header differs")
            for row in rows:
                if len(row) != 4 or not re.fullmatch(r"s(?:0|[1-9][0-9]*)", row[2]):
                    raise ValidationError("constant confirmation inventory row is invalid")
                origin = int(row[2][1:])
                if origin in selected_origins:
                    raise ValidationError("conditional source is still implemented in the selected snapshot")
                if origin in frontier_origins:
                    expected_objects.append({"source_origin":row[2], "name":row[0]})
                    if len(expected_objects) > 4096:
                        raise ValidationError("constant confirmation replication exceeds diagnostic scope")
        if inventory_digest.hexdigest() != owner.get("stage_inventory_sha256"):
            raise ValidationError("constant confirmation inventory belongs to a different source model")
        if {int(item["source_origin"][1:]) for item in expected_objects} != frontier_origins:
            raise ValidationError("conditional source has an unobserved or optimized frontier")
        comparison = compare_selected_constant_frontiers(mapped_path, functions_path, candidates_path, observations_path,
            mapped_sha256=mapped_sha256, expected_objects=expected_objects,
            selected_constant_values=selected_values,require_input_identity=True)
        return {"schema":CONSTANT_CONFIRMATION_SCHEMA, "status":"pass", "fpga":fpga,
                "mapped_index_sha256":mapped_sha256, "source_stage":"synthesized",
                "initialization_policy":CONSTANT_INITIALIZATION_POLICY,
                "source_model_scope":CONSTANT_MODEL_SCOPE, "native_input_policy":CONSTANT_NATIVE_INPUT_POLICY,
                "synthesized_checkpoint_sha256":checkpoint_sha256,
                "source_candidate_proofs_sha256":candidate_proofs_sha256,
                "source_inventory_sha256":inventory_digest.hexdigest(),
                "observations_sha256":file_sha256(observations_path),
                "sources":sorted(source_rows, key=lambda row:row["source"]),
                "frontiers":comparison["frontiers"]}
    except (ValueError, UnicodeError, OverflowError, sqlite3.DatabaseError) as error:
        raise ValidationError("constant confirmation source evidence is malformed") from error
    finally:
        db.close()
