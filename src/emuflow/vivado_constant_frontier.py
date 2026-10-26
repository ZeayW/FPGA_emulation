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

CONSTANT_CONFIRMATION_SCHEMA = "emuflow.vivado-selected-constant-confirmations/v4"
CONSTANT_INITIALIZATION_POLICY = "joint-inductive-source-model-with-explicit-vendor-selected-initialization"
CONSTANT_MODEL_SCOPE = "selected-states-inductive-without-unconfirmed-state-assumptions"
CONSTANT_NATIVE_INPUT_POLICY = "same-primitive-pin-bindings-and-direct-output-constants-at-preplacement-v2"
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
    expected = {(item["source_origin"], item["name"]): item.get("object_kind", "cell") for item in expected_objects}
    if not expected or len(expected) != len(expected_objects):
        raise ValidationError("constant-frontier expected object set is empty or duplicated")
    if any(kind not in {"cell", "output-port"} for kind in expected.values()):
        raise ValidationError("constant-frontier object kind is invalid")
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
        ports = json.loads(db.execute("SELECT value FROM metadata WHERE key='ports_json'").fetchone()[0])

        def compare_output(key, group):
            if not require_input_identity or selected_constant_values is None or len(group) != 1:
                raise ValidationError("output port confirmation requires an explicit checked source selection")
            row = group[0]
            origin = int(key[0][1:])
            value = selected_constant_values.get(origin)
            matches = []
            for name, port in ports.items():
                if port["direction"] != "output":
                    continue
                for bit, net in enumerate(port["bits"]):
                    native_name = name if len(port["bits"]) == 1 else f"{name}[{bit}]"
                    if native_name == key[1]:
                        matches.append(net)
            if (value not in (0, 1) or len(matches) != 1 or not isinstance(matches[0], int)
                    or db.execute("SELECT cell_seq,port,port_bit FROM cell_pins WHERE net_bit=? AND direction=1 AND value_kind=0 LIMIT 2", (matches[0],)).fetchall() != [(origin, "Q", 0)]):
                raise ValidationError("output port does not directly expose the selected source Q")
            if (row["ref"] != "OUTPUT_PORT" or row["init"] != "" or row["pin"] != "PORT"
                    or row["driver_kind"] != ("one" if value else "zero")
                    or row["driver_ref"] != ("VCC" if value else "GND")
                    or row["driver_source"] or row["transformations_hex"]):
                raise ValidationError("native output port does not confirm the selected source constant")
            return {"source_origin": key[0], "native_cell": key[1], "object_kind": "output-port",
                    "variables": 0, "assignments": 1}

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
                if expected[key] == "output-port":
                    checked.append(compare_output(key, group))
                    return
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
                checked.append({"source_origin":key[0], "native_cell":key[1], "object_kind":"cell", **result})

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
        if seen != set(expected):
            if any(expected[key] == "output-port" for key in set(expected) - seen):
                raise ValidationError("constant-frontier output port observation coverage is incomplete")
            raise ValidationError("constant-frontier observation object coverage is incomplete")
        return {"status":"pass", "scope":"selected-conditional-source-frontier-function-comparison-only",
                "mapped_index_sha256":mapped_sha256, "frontiers":checked}
    except (ValueError, UnicodeError, OverflowError, sqlite3.DatabaseError) as error:
        raise ValidationError("constant-frontier observation or source index is malformed") from error
    finally:
        db.close()


class _UnconfirmedStateDependencies(ValidationError):
    def __init__(self, source, dependencies):
        self.source = source
        self.dependencies = tuple(sorted(dependencies))
        super().__init__(f"selected source s{source} is not independently inductive; "
                         f"{len(self.dependencies)} unconfirmed state dependencies remain")


def _require_inductive_selected_states(db, mapped_path: Path, values: Mapping[int, int]):
    """Prove the selected joint states without assuming other INIT choices."""
    from .vivado_optimization import _RESET, _parameter_bit, _lut_value
    from .xilinx_netlist import open_xilinx_mapped_index
    with open_xilinx_mapped_index(mapped_path) as mapped:
        @lru_cache(maxsize=256)
        def primitive(name):
            return mapped.cells[name]

        visiting = set()
        evaluation_steps = 0

        @lru_cache(maxsize=4096)
        def evaluate(bit, depth=0):
            nonlocal evaluation_steps
            evaluation_steps += 1
            if evaluation_steps > 65536:
                raise ValidationError("constant confirmation induction exceeds bounded expression scope")
            if isinstance(bit,str) and bit in {"0","1"}:
                return int(bit), frozenset()
            if not isinstance(bit,int) or depth >= 64 or bit in visiting:
                return None, frozenset()
            drivers = db.execute("SELECT cell_seq,cell_id,port FROM cell_pins WHERE net_bit=? AND direction=1 AND value_kind=0 LIMIT 2", (bit,)).fetchall()
            if len(drivers) != 1:
                return None, frozenset()
            origin, name, output = drivers[0]
            if origin in values and output == "Q":
                return values[origin], frozenset()
            strict = db.execute("SELECT constant_value FROM proofs.eliminated_sources WHERE origin=? AND reason='constant-state' AND initialization='specified'", (origin,)).fetchone()
            cell = primitive(name)
            if strict is not None and output == "Q":
                if cell["type"] not in _RESET:
                    raise ValidationError("strict constant source is not a supported state primitive")
                initial = cell.get("parameters",{}).get("INIT",str(_RESET[cell["type"]][1]))
                if initial not in {"0","1"} or int(initial) != strict[0]:
                    raise ValidationError("strict source initial value differs from its constant proof")
                return int(strict[0]), frozenset()
            if output == "Q" and db.execute("SELECT 1 FROM proofs.constant_candidates WHERE origin=?",(origin,)).fetchone():
                return None, frozenset({origin})
            if not (cell["type"].startswith("LUT") or cell["type"] in {"MUXF7","MUXF8","MUXF9"}):
                return None, frozenset()
            visiting.add(bit)
            try:
                inputs = {pin:evaluate(bits[0],depth+1) for pin,bits in cell["connections"].items()
                          if cell["port_directions"][pin] == "input" and len(bits)==1}
                pins = {pin:result[0] for pin,result in inputs.items()}
                if cell["type"].startswith("LUT"):
                    result = _lut_value(cell,output,pins)
                else:
                    select = pins.get("S")
                    result = pins.get("I"+str(select)) if select is not None else pins.get("I0") if pins.get("I0") == pins.get("I1") else None
                dependencies = frozenset().union(*(value[1] for value in inputs.values())) if result is None else frozenset()
                return result, dependencies
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
                return evaluate(bits[0]) if len(bits)==1 else (None,frozenset())
            evaluated = [pin_value(pin) for pin in ("D","CE",reset)]
            data, enable, control = (value[0] for value in evaluated)
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
                dependencies = frozenset().union(*(value[1] for value in evaluated))
                if dependencies:
                    raise _UnconfirmedStateDependencies(origin,dependencies)
                raise ValidationError(f"selected source s{origin} is not independently inductive")


def prepare_constant_frontier_query(mapped_path: Path, candidates_path: Path,
                                    inventory_path: Path, *, mapped_sha256: str,
                                    candidate_proofs_sha256: str, checkpoint_sha256: str,
                                    fpga: str, selected_sources, source_stage: str = "synthesized") -> dict[str, Any]:
    """Close required state dependencies and fanouts before native inspection.

    Additional INIT choices become explicit source records with complete fanout
    coverage. They are never silently borrowed from the hypothetical model.
    """
    if not isinstance(selected_sources,Sequence) or isinstance(selected_sources,(str,bytes)):
        raise ValidationError("constant confirmation needs bounded unique source identities and an FPGA")
    requested = list(selected_sources)
    selected = list(requested)
    while True:
        try:
            plan = _prepare_constant_frontier_query_once(mapped_path,candidates_path,inventory_path,
                mapped_sha256=mapped_sha256,candidate_proofs_sha256=candidate_proofs_sha256,
                checkpoint_sha256=checkpoint_sha256,fpga=fpga,selected_sources=selected,source_stage=source_stage)
        except _UnconfirmedStateDependencies as error:
            additional = [f"s{i}" for i in error.dependencies if f"s{i}" not in selected]
            if not additional or len(selected)+len(additional)>4096:
                raise ValidationError("constant confirmation state dependencies exceed bounded scope") from error
            selected.extend(additional)
            continue
        plan["requested_sources"] = requested
        return plan


def _prepare_constant_frontier_query_once(mapped_path: Path, candidates_path: Path,
                                    inventory_path: Path, *, mapped_sha256: str,
                                    candidate_proofs_sha256: str, checkpoint_sha256: str,
                                    fpga: str, selected_sources, source_stage: str = "synthesized") -> dict[str, Any]:
    """Plan a bounded closed state component before opening a native checkpoint.

    Follow state-to-state fanouts only through conditional candidates. Every
    selected state must independently remain constant under the joint values,
    and all observable exits must be retained LUTs or directly observed output
    ports. No native observations are
    inferred here; the returned query is only an inspection plan.
    """
    from .io import file_sha256
    from .vivado_lineage import INVENTORY_HEADER
    if source_stage not in {"synthesized", "optimized"}:
        raise ValidationError("constant confirmation needs an explicit preplacement observation stage")
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
        if (owner.get("stage") != source_stage or owner.get("mapped_index_sha256") != mapped_sha256
                or owner.get("fpga") not in (None, fpga)):
            raise ValidationError("constant confirmation needs the matching preplacement source model")
        source_rows = []
        frontier_origins = set()
        selected_q = {}
        pending = list(int(identity[1:]) for identity in selected_sources)
        discovered = set(pending)
        direct_frontiers, state_successors = {}, {}
        uses = 0
        for origin in pending:
            candidate = db.execute("SELECT constant_value,initialization FROM proofs.constant_candidates WHERE origin=?", (origin,)).fetchone()
            outputs = db.execute("SELECT port,port_bit,net_bit,value_kind FROM cell_pins WHERE cell_seq=? AND direction=1", (origin,)).fetchall()
            if (candidate is None or candidate[0] not in (0, 1)
                    or candidate[1] not in {"specified", "vendor-dont-care-selection"}
                    or len(outputs) != 1 or outputs[0][:2] != ("Q", 0) or outputs[0][3] != 0):
                raise ValidationError("selected source is not a supported conditional constant state")
            q = outputs[0][2]
            if q in selected_q:
                raise ValidationError("conditional states share an output net")
            selected_q[q] = origin
            direct_frontiers[origin], state_successors[origin] = set(), set()
            for target,kind,pin,bit in db.execute("SELECT s.cell_seq,c.type,s.port,s.port_bit FROM cell_pins s JOIN cells c ON c.seq=s.cell_seq WHERE s.net_bit=? AND s.value_kind=0 AND s.direction=0 ORDER BY s.cell_seq,s.port,s.port_bit", (q,)):
                uses += 1
                if uses > 65536:
                    raise ValidationError("conditional source component exceeds bounded fanout scope")
                if kind in {f"LUT{i}" for i in range(1, 7)} and re.fullmatch(r"I[0-5]",pin) and bit == 0:
                    direct_frontiers[origin].add(target)
                    frontier_origins.add(target)
                elif kind in {"FDRE","FDSE","FDCE","FDPE"} and pin in {"D","CE","R","S","CLR","PRE"} and bit == 0:
                    # An unsupported/noncandidate successor fails on its own
                    # row. Never silently treat it as an internal constant.
                    state_successors[origin].add(target)
                    if target not in discovered:
                        discovered.add(target)
                        pending.append(target)
                else:
                    raise ValidationError("conditional source has an unsupported observable fanout")
                if len(discovered) > 4096 or len(frontier_origins) > 4096:
                    raise ValidationError("constant confirmation component exceeds diagnostic scope")
            source_rows.append({"source":f"s{origin}", "constant_value":int(candidate[0]),
                                "source_initialization":candidate[1]})
        # A state may have no LUT fanout at all and still be observable at a
        # module output. Inspect that exact bit in the vendor snapshot; neither
        # the absent state tag nor its source-model candidate proves the port.
        ports = json.loads(db.execute("SELECT value FROM metadata WHERE key='ports_json'").fetchone()[0])
        output_objects = []
        for name, port in ports.items():
            if port["direction"] != "output":
                continue
            for bit, net in enumerate(port["bits"]):
                if not isinstance(net, int) or net not in selected_q:
                    continue
                origin = selected_q[net]
                direct_frontiers[origin].add(origin)
                output_objects.append({"source_origin":f"s{origin}",
                    "name":name if len(port["bits"]) == 1 else f"{name}[{bit}]",
                    "object_kind":"output-port"})
                if len(output_objects) + len(frontier_origins) > 4096:
                    raise ValidationError("constant confirmation output port coverage exceeds bounded scope")
        # Each row references all reachable external LUT frontiers, including
        # exits through other selected states. Bound both traversal and output.
        visits, references = 0, 0
        for row in source_rows:
            queue, seen, reachable = [int(row["source"][1:])], set(), set()
            for origin in queue:
                if origin in seen:
                    continue
                seen.add(origin)
                visits += 1
                if visits > 4_194_304:
                    raise ValidationError("constant confirmation closure exceeds traversal scope")
                reachable.update(direct_frontiers[origin])
                queue.extend(target for target in state_successors[origin] if target not in seen)
            if not reachable:
                raise ValidationError("conditional source lacks an observed functional frontier")
            references += len(reachable)
            if references > 65536:
                raise ValidationError("constant confirmation closure exceeds frontier-reference scope")
            row["frontier_sources"] = [f"s{i}" for i in sorted(reachable)]
        selected_values = {int(row["source"][1:]):row["constant_value"] for row in source_rows}
        _require_inductive_selected_states(db,mapped_path,selected_values)
        expected_objects = []
        selected_origins = set(selected_values)
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
                if len(row) != len(INVENTORY_HEADER) or not re.fullmatch(r"s(?:0|[1-9][0-9]*)", row[2]):
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
        expected_objects.extend(output_objects)
        if len(expected_objects) > 4096:
            raise ValidationError("constant confirmation replication and output ports exceed bounded scope")
        return {"schema":"emuflow.vivado-constant-frontier-query-plan/v2", "status":"inspection-required",
                "fpga":fpga, "mapped_index_sha256":mapped_sha256, "source_stage":source_stage,
                "synthesized_checkpoint_sha256":checkpoint_sha256,
                "source_candidate_proofs_sha256":candidate_proofs_sha256,
                "source_inventory_sha256":inventory_digest.hexdigest(),
                "requested_sources":list(selected_sources),
                "sources":sorted(source_rows,key=lambda row:row["source"]),
                "query_objects":expected_objects}
    except (ValueError, UnicodeError, OverflowError, sqlite3.DatabaseError) as error:
        raise ValidationError("constant confirmation source evidence is malformed") from error
    finally:
        db.close()


def confirm_selected_constant_sources(mapped_path: Path, functions_path: Path,
                                      candidates_path: Path, observations_path: Path,
                                      inventory_path: Path, *, mapped_sha256: str,
                                      candidate_proofs_sha256: str, checkpoint_sha256: str,
                                      fpga: str, selected_sources, source_stage: str = "synthesized") -> dict[str, Any]:
    """Confirm all retained LUT exits of a closed inductive state component.

    This refines unspecified initialization under an explicit vendor choice;
    it does not establish equivalence for every initial state or assign delay.
    """
    from .io import file_sha256
    plan = prepare_constant_frontier_query(mapped_path,candidates_path,inventory_path,
        mapped_sha256=mapped_sha256,candidate_proofs_sha256=candidate_proofs_sha256,
        checkpoint_sha256=checkpoint_sha256,fpga=fpga,selected_sources=selected_sources,source_stage=source_stage)
    source_rows, expected_objects = plan["sources"], plan["query_objects"]
    selected_values = {int(row["source"][1:]):row["constant_value"] for row in source_rows}
    comparison = compare_selected_constant_frontiers(mapped_path, functions_path, candidates_path, observations_path,
        mapped_sha256=mapped_sha256, expected_objects=expected_objects,
        selected_constant_values=selected_values,require_input_identity=True)
    return {"schema":CONSTANT_CONFIRMATION_SCHEMA, "status":"pass", "fpga":fpga,
            "mapped_index_sha256":mapped_sha256, "source_stage":source_stage,
            "initialization_policy":CONSTANT_INITIALIZATION_POLICY,
            "source_model_scope":CONSTANT_MODEL_SCOPE, "native_input_policy":CONSTANT_NATIVE_INPUT_POLICY,
            "synthesized_checkpoint_sha256":checkpoint_sha256,
            "source_candidate_proofs_sha256":candidate_proofs_sha256,
            "source_inventory_sha256":plan["source_inventory_sha256"],
            "observations_sha256":file_sha256(observations_path),
            "sources":sorted(source_rows, key=lambda row:row["source"]),
            "frontiers":comparison["frontiers"]}
