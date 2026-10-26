"""Indexed proofs for source logic removed by vendor optimization.

The compact proof store owns source-cell dispositions. Timing rows reference
these facts; absence of a source tag alone is never an elimination proof.
This module neither assigns delays nor changes the original path population.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from .errors import ValidationError
from .store_publication import (
    bind_store_source,
    StoreSource,
    publish_sqlite_store,
    validate_store_destination,
    require_store_sources_unchanged,
)
from .xilinx_netlist import open_xilinx_mapped_index

OPTIMIZATION_PROOF_SCHEMA = "emuflow.vivado-optimization-proofs/v1"
_RESET = {"FDRE": ("R", 0), "FDSE": ("S", 1),
          "FDCE": ("CLR", 0), "FDPE": ("PRE", 1)}


def _literal(value: Any) -> int | None:
    return int(value) if isinstance(value, str) and value in {"0", "1"} else None


def _parameter_bit(cell: Mapping[str, Any], key: str) -> int | None:
    value = cell.get("parameters", {}).get(key, "0")
    if not isinstance(value, str) or not value or any(c not in "01" for c in value):
        return None
    number = int(value, 2)
    return number if number in (0, 1) else None


def _lut_value(cell: Mapping[str, Any], output: str, values: Mapping[str, int | None]) -> int | None:
    kind = cell["type"]
    if kind == "LUT6_2":
        width = 5 if output == "O5" else 6 if output == "O6" else 0
    else:
        width = int(kind[-1]) if kind in {f"LUT{i}" for i in range(1, 7)} and output == "O" else 0
    init = cell.get("parameters", {}).get("INIT", "")
    if not width or not isinstance(init, str) or len(init) < (1 << width):
        return None
    observed = None
    for index in range(1 << width):
        if any(values.get(f"I{i}") is not None and (index >> i) & 1 != values[f"I{i}"] for i in range(width)):
            continue
        digit = init[-1-index]
        if digit not in "01":
            return None
        bit = int(digit)
        if observed is not None and observed != bit:
            return None
        observed = bit
    return observed


def build_optimization_proofs(mapped_path: Path, lineage_path: Path, output: Path, *,
                              mapped_sha256: str, stage: str = "routed",
                              fpga: str | None = None, checked_constants=None,
                              synthesized_checkpoint_sha256: str | None = None) -> dict[str, Any]:
    """Prove closed unobservable regions and simultaneous constant states.

    Graph traversal and state hypotheses live in SQLite. Python reconstructs
    only the primitive currently needed by a bounded expression evaluation.
    Unknown INIT is recorded as a legal initialization choice, rather than
    represented as equivalence for every possible initial state.
    """
    if stage not in {"synthesized", "optimized", "routed"}:
        raise ValidationError("unsupported Vivado optimization proof stage")
    if len(mapped_sha256) != 64 or any(c not in "0123456789abcdef" for c in mapped_sha256):
        raise ValidationError("invalid Vivado optimization mapped identity")
    if output.exists():
        raise ValidationError("Vivado optimization proof output already exists")
    confirmation = None
    if checked_constants is not None:
        from .vivado_constant_frontier import CheckedConstantSources
        if not isinstance(checked_constants, CheckedConstantSources):
            raise ValidationError("constant confirmation must come from the source/native semantic checker")
        confirmation = checked_constants.to_document()
        if (confirmation["mapped_index_sha256"] != mapped_sha256 or confirmation["fpga"] != fpga
                or confirmation["synthesized_checkpoint_sha256"] != synthesized_checkpoint_sha256):
            raise ValidationError("constant confirmation source, FPGA, or synthesized checkpoint differs")
    source_bindings = validate_store_destination(
        output,
        (
            bind_store_source(mapped_path, "xilinx-mapped-index-v2"),
            bind_store_source(lineage_path, "vivado-lineage-store-v2"),
        ),
    )
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    db = sqlite3.connect(temporary, uri=True)
    try:
        db.execute("PRAGMA temp_store=MEMORY")
        db.execute("ATTACH DATABASE ? AS source", (f"file:{mapped_path.resolve()}?mode=ro&immutable=1",))
        db.execute("ATTACH DATABASE ? AS observed", (f"file:{lineage_path.resolve()}?mode=ro&immutable=1",))
        owner = dict(db.execute("SELECT key,value FROM observed.metadata"))
        if owner.get("mapped_index_sha256") != mapped_sha256 or owner.get("retained_stage") != stage:
            raise ValidationError("Vivado optimization proof lineage belongs to a different source or stage")
        stage_inventory_sha256 = json.loads(owner["stages_json"])[stage]["inventory_sha256"]
        db.executescript("""
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE eliminated_sources(
                origin INTEGER PRIMARY KEY, reason TEXT NOT NULL
                    CHECK(reason IN ('unobservable-source-closure','constant-state')),
                constant_value INTEGER CHECK(constant_value IN (0,1)),
                initialization TEXT CHECK(initialization IN ('specified','vendor-dont-care-selection','vendor-confirmed')),
                CHECK((reason='unobservable-source-closure' AND constant_value IS NULL AND initialization IS NULL)
                   OR (reason='constant-state' AND constant_value IS NOT NULL AND initialization IS NOT NULL))
            );
            CREATE TEMP TABLE absent(origin INTEGER PRIMARY KEY);
            CREATE TEMP TABLE edges(source INTEGER,target INTEGER,PRIMARY KEY(source,target)) WITHOUT ROWID;
            CREATE INDEX temp.edges_target ON edges(target,source);
            CREATE TEMP TABLE observable(origin INTEGER PRIMARY KEY);
        """)
        db.execute("INSERT INTO absent SELECT c.seq FROM source.cells c WHERE NOT EXISTS (SELECT 1 FROM observed.objects o WHERE o.stage=? AND o.origin=c.seq)", (stage,))
        db.execute("""INSERT OR IGNORE INTO edges
            SELECT o.cell_seq,s.cell_seq FROM absent a
            CROSS JOIN source.cell_pins o ON o.cell_seq=a.origin AND o.direction=1 AND o.value_kind=0
            CROSS JOIN source.cell_pins s ON s.net_bit=o.net_bit AND s.direction=0 AND s.value_kind=0""")
        db.execute("INSERT OR IGNORE INTO observable SELECT DISTINCT source FROM edges WHERE target NOT IN (SELECT origin FROM absent)")
        ports = json.loads(db.execute("SELECT value FROM source.metadata WHERE key='ports_json'").fetchone()[0])
        for port in ports.values():
            if port["direction"] == "output":
                for bit in port["bits"]:
                    if isinstance(bit, int):
                        db.execute("INSERT OR IGNORE INTO observable SELECT cell_seq FROM source.cell_pins WHERE net_bit=? AND direction=1 AND value_kind=0 AND cell_seq IN (SELECT origin FROM absent)", (bit,))
        db.execute("""INSERT INTO eliminated_sources(origin,reason)
            WITH RECURSIVE live(origin) AS (
                SELECT origin FROM observable
                UNION SELECT e.source FROM edges e JOIN live ON e.target=live.origin
            ) SELECT origin,'unobservable-source-closure' FROM absent WHERE origin NOT IN (SELECT origin FROM live)""")
        unobservable = db.execute("SELECT count(*) FROM eliminated_sources").fetchone()[0]
        db.executescript("""
            CREATE TEMP TABLE states(origin INTEGER PRIMARY KEY,q INTEGER UNIQUE,value INTEGER,initialization TEXT);
            CREATE TEMP TABLE rejected(origin INTEGER PRIMARY KEY);
        """)
        with open_xilinx_mapped_index(mapped_path) as mapped:
            for origin, name in db.execute("SELECT c.seq,c.id FROM absent a JOIN source.cells c ON c.seq=a.origin WHERE c.type IN ('FDRE','FDSE','FDCE','FDPE') AND c.seq NOT IN (SELECT origin FROM eliminated_sources) ORDER BY c.seq"):
                cell = mapped.cells[name]
                q = cell["connections"].get("Q", [])
                initial = cell.get("parameters", {}).get("INIT", str(_RESET[cell["type"]][1]))
                if len(q) != 1 or not isinstance(q[0], int) or initial not in {"0", "1", "x", "X"}:
                    continue
                literal_d = _literal(cell["connections"].get("D", [None])[0])
                value = int(initial) if initial in {"0", "1"} else literal_d if literal_d is not None else _RESET[cell["type"]][1]
                db.execute("INSERT INTO states VALUES (?,?,?,?)", (origin,q[0],value,"specified" if initial in {"0","1"} else "vendor-dont-care-selection"))
            hypotheses = db.execute("SELECT count(*) FROM states").fetchone()[0]

            @lru_cache(maxsize=256)
            def primitive(name: str) -> Mapping[str, Any]:
                return mapped.cells[name]

            visiting: set[int] = set()

            @lru_cache(maxsize=4096)
            def evaluate(bit: Any, depth: int = 0) -> int | None:
                literal = _literal(bit)
                if literal is not None:
                    return literal
                if not isinstance(bit, int) or depth >= 64 or bit in visiting:
                    return None
                state = db.execute("SELECT value FROM states WHERE q=?", (bit,)).fetchone()
                if state:
                    return int(state[0])
                drivers = db.execute("SELECT cell_id,port FROM source.cell_pins WHERE net_bit=? AND direction=1 AND value_kind=0 LIMIT 2", (bit,)).fetchall()
                if len(drivers) != 1:
                    return None
                cell = primitive(drivers[0][0])
                if not (cell["type"].startswith("LUT") or cell["type"] in {"MUXF7","MUXF8","MUXF9"}):
                    return None
                visiting.add(bit)
                try:
                    values = {pin: evaluate(bits[0], depth+1) for pin,bits in cell["connections"].items()
                              if cell["port_directions"][pin] == "input" and len(bits)==1}
                    if cell["type"].startswith("LUT"):
                        return _lut_value(cell, drivers[0][1], values)
                    select = values.get("S")
                    if select is not None:
                        return values.get("I"+str(select))
                    return values.get("I0") if values.get("I0") == values.get("I1") else None
                finally:
                    visiting.remove(bit)

            def refine_states():
                iterations = 0
                while True:
                    iterations += 1
                    evaluate.cache_clear()
                    db.execute("DELETE FROM rejected")
                    for origin, name, value in db.execute("SELECT s.origin,c.id,s.value FROM states s JOIN source.cells c ON c.seq=s.origin ORDER BY s.origin"):
                        cell = primitive(name)
                        reset, reset_value = _RESET[cell["type"]]
                        def input_value(pin: str) -> int | None:
                            bits = cell["connections"].get(pin, [])
                            return evaluate(bits[0]) if len(bits)==1 else None
                        data, enable, control = input_value("D"), input_value("CE"), input_value(reset)
                        inversions = [_parameter_bit(cell, "IS_"+pin+"_INVERTED") for pin in ("D","CE",reset)]
                        if any(item is None for item in inversions):
                            stable = False
                        else:
                            data = None if data is None else data ^ inversions[0]
                            enable = None if enable is None else enable ^ inversions[1]
                            control = None if control is None else control ^ inversions[2]
                            stable = ((value == reset_value) if control == 1 else
                                      (control == 0 or value == reset_value) and (enable == 0 or data == value))
                        if not stable:
                            db.execute("INSERT INTO rejected VALUES (?)", (origin,))
                    rejected = db.execute("SELECT count(*) FROM rejected").fetchone()[0]
                    if not rejected:
                        break
                    db.execute("DELETE FROM states WHERE origin IN (SELECT origin FROM rejected)")
                return iterations

            iterations = refine_states()
            db.execute("CREATE TABLE constant_candidates(origin INTEGER PRIMARY KEY,constant_value INTEGER NOT NULL CHECK(constant_value IN (0,1)),initialization TEXT NOT NULL)")
            db.execute("INSERT INTO constant_candidates SELECT origin,value,initialization FROM states")
            # A legal choice of unknown INIT is only a candidate until the
            # vendor implementation confirms that choice. Re-prove the strict
            # subset without assuming any of those conditional states.
            db.execute("DELETE FROM states WHERE initialization != 'specified'")
            strict_iterations = refine_states()
            db.execute("INSERT INTO eliminated_sources SELECT origin,'constant-state',value,initialization FROM states")
            db.execute("DELETE FROM constant_candidates WHERE origin IN (SELECT origin FROM eliminated_sources)")
        vendor_confirmed = 0
        if confirmation is not None:
            if stage != "routed" and (confirmation["source_stage"] != stage or confirmation["source_inventory_sha256"] != stage_inventory_sha256):
                raise ValidationError("constant confirmation belongs to a different preplacement snapshot")
            for source in confirmation["sources"]:
                origin = int(source["source"][1:])
                if db.execute("SELECT 1 FROM eliminated_sources WHERE origin=?", (origin,)).fetchone():
                    continue
                if not db.execute("SELECT 1 FROM absent WHERE origin=?", (origin,)).fetchone():
                    continue
                candidate = db.execute("SELECT constant_value FROM constant_candidates WHERE origin=?", (origin,)).fetchone()
                if candidate != (source["constant_value"],):
                    raise ValidationError("vendor selection does not satisfy the current inductive source model")
                db.execute("INSERT INTO eliminated_sources VALUES (?,'constant-state',?,'vendor-confirmed')", (origin,source["constant_value"]))
                db.execute("DELETE FROM constant_candidates WHERE origin=?", (origin,))
                vendor_confirmed += 1
            if vendor_confirmed:
                db.execute("CREATE TABLE vendor_confirmation_document(id INTEGER PRIMARY KEY CHECK(id=0),document TEXT NOT NULL)")
                db.execute("INSERT INTO vendor_confirmation_document VALUES (0,?)", (json.dumps(confirmation,sort_keys=True,separators=(",",":"),allow_nan=False),))
        constant = db.execute("SELECT count(*) FROM eliminated_sources WHERE reason='constant-state'").fetchone()[0]
        unretained = db.execute("SELECT count(*) FROM absent").fetchone()[0]
        db.execute("CREATE TABLE source_names(origin INTEGER PRIMARY KEY,name TEXT NOT NULL)")
        db.execute("INSERT INTO source_names SELECT seq,id FROM source.cells WHERE seq IN (SELECT origin FROM eliminated_sources UNION SELECT origin FROM constant_candidates)")
        names_digest = hashlib.sha256()
        for row in db.execute("SELECT origin,name FROM source_names ORDER BY origin"):
            names_digest.update((json.dumps(row,separators=(",",":"))+"\n").encode())
        digest = hashlib.sha256()
        for row in db.execute("SELECT origin,reason,constant_value,initialization FROM eliminated_sources ORDER BY origin"):
            digest.update((json.dumps(row,separators=(",",":"))+"\n").encode())
        header = {"schema": OPTIMIZATION_PROOF_SCHEMA, "stage": stage, "mapped_index_sha256": mapped_sha256,
                  "fpga": fpga,
                  "stage_inventory_sha256": stage_inventory_sha256,
                  "qualification": "source-closure-and-inductive-constant-state; conditional-initialization-candidates-are-not-proofs",
                  "source_cells": db.execute("SELECT count(*) FROM source.cells").fetchone()[0],
                  "unretained_source_cells": unretained, "unobservable_source_cells": unobservable,
                  "constant_state_cells": constant, "unproved_source_cells": unretained-unobservable-constant,
                  "constant_hypotheses": hypotheses, "constant_refinement_iterations": iterations,
                  "strict_constant_refinement_iterations": strict_iterations,
                  "conditional_constant_state_candidates": db.execute("SELECT count(*) FROM constant_candidates").fetchone()[0],
                  "proof_rows_sha256": digest.hexdigest()}
        header["source_names_sha256"] = names_digest.hexdigest()
        if vendor_confirmed:
            document = json.dumps(confirmation,sort_keys=True,separators=(",",":"),allow_nan=False)
            header.update(vendor_confirmed_constant_state_cells=vendor_confirmed,
                          vendor_confirmation_document_sha256=hashlib.sha256(document.encode()).hexdigest(),
                          synthesized_checkpoint_sha256=synthesized_checkpoint_sha256,
                          vendor_initialization_policy=confirmation["initialization_policy"])
            header["qualification"] += "; selected-vendor-initialization-confirmed-at-complete-retained-frontiers"
        db.executemany("INSERT INTO metadata VALUES (?,?)", ((key,json.dumps(value,separators=(",",":"))) for key,value in header.items()))
        db.commit()
        db.close()
        publish_sqlite_store(
            temporary, output, "vivado-optimization-proofs-v1", sources=source_bindings
        )
        return {"status": "pass", **header, "output": str(output)}
    except BaseException:
        db.close()
        temporary.unlink(missing_ok=True)
        raise
    finally:
        db.close()


class OptimizationProofView(Mapping[str, Any]):
    """One read-only, source-bound proof owner for a physical partition."""

    def __init__(self, path: Path, *, expected_fpga: str,
                 expected_sha256: str | None = None, stage: str = "routed"):
        if not isinstance(expected_fpga,str) or not expected_fpga:
            raise ValidationError("Vivado optimization proof needs an explicit FPGA identity")
        self._source_binding = bind_store_source(path,"vivado-optimization-proofs-v1")
        if expected_sha256 is not None:
            from .io import file_sha256
            if file_sha256(path) != expected_sha256:
                raise ValidationError("Vivado optimization proof artifact digest disagrees")
        self.connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro&immutable=1", uri=True)
        self._closed = False
        try:
            self.header = {key:json.loads(value) for key,value in self.connection.execute("SELECT key,value FROM metadata")}
            if self.header.get("schema") != OPTIMIZATION_PROOF_SCHEMA or self.header.get("stage") != stage or self.header.get("fpga") != expected_fpga:
                raise ValidationError("Vivado optimization proof partition or stage disagrees")
            confirmed = {}
            if "vendor_confirmation_document_sha256" in self.header:
                from .vivado_constant_frontier import (CONSTANT_CONFIRMATION_SCHEMA, CONSTANT_INITIALIZATION_POLICY,
                                                       CONSTANT_MODEL_SCOPE, CONSTANT_NATIVE_INPUT_POLICY)
                rows = self.connection.execute("SELECT id,document FROM vendor_confirmation_document").fetchall()
                if len(rows) != 1 or rows[0][0] != 0 or len(rows[0][1]) > 16 * 1024 * 1024:
                    raise ValidationError("Vivado vendor confirmation document is invalid")
                raw = rows[0][1]
                if hashlib.sha256(raw.encode()).hexdigest() != self.header["vendor_confirmation_document_sha256"]:
                    raise ValidationError("Vivado vendor confirmation document seal differs")
                document = json.loads(raw)
                keys = {"schema","status","fpga","mapped_index_sha256","source_stage","initialization_policy",
                        "synthesized_checkpoint_sha256","source_candidate_proofs_sha256","source_inventory_sha256",
                        "observations_sha256","sources","frontiers","source_model_scope","native_input_policy"}
                if (not isinstance(document,dict) or set(document) != keys
                        or document["schema"] != CONSTANT_CONFIRMATION_SCHEMA or document["status"] != "pass"
                        or document["fpga"] != expected_fpga or document["source_stage"] not in {"synthesized", "optimized"}
                        or (stage != "routed" and document["source_stage"] != stage)
                        or (stage != "routed" and document["source_inventory_sha256"] != self.header["stage_inventory_sha256"])
                        or document["mapped_index_sha256"] != self.header["mapped_index_sha256"]
                        or document["synthesized_checkpoint_sha256"] != self.header.get("synthesized_checkpoint_sha256")
                        or document["initialization_policy"] != CONSTANT_INITIALIZATION_POLICY
                        or document["source_model_scope"] != CONSTANT_MODEL_SCOPE
                        or document["native_input_policy"] != CONSTANT_NATIVE_INPUT_POLICY
                        or self.header.get("vendor_initialization_policy") != CONSTANT_INITIALIZATION_POLICY
                        or not isinstance(document["sources"],list) or not 1 <= len(document["sources"]) <= 4096
                        or not isinstance(document["frontiers"],list) or not 1 <= len(document["frontiers"]) <= 4096):
                    raise ValidationError("Vivado vendor confirmation source or initialization policy differs")
                import re
                for key in ("synthesized_checkpoint_sha256","source_candidate_proofs_sha256","source_inventory_sha256","observations_sha256"):
                    if not isinstance(document[key],str) or not re.fullmatch(r"[0-9a-f]{64}",document[key]):
                        raise ValidationError("Vivado vendor confirmation digest is invalid")
                required_frontiers = set()
                for source in document["sources"]:
                    if (not isinstance(source,dict) or set(source) != {"source","constant_value","source_initialization","frontier_sources"}
                            or not isinstance(source["source"],str) or not re.fullmatch(r"s(?:0|[1-9][0-9]*)",source["source"])
                            or int(source["source"][1:]) > 2**63-1 or isinstance(source["constant_value"],bool)
                            or not isinstance(source["constant_value"],int) or source["constant_value"] not in (0,1)
                            or source["source_initialization"] not in {"specified","vendor-dont-care-selection"}
                            or not isinstance(source["frontier_sources"],list) or not 1 <= len(source["frontier_sources"]) <= 4096
                            or any(not isinstance(s,str) or not re.fullmatch(r"s(?:0|[1-9][0-9]*)",s) for s in source["frontier_sources"])):
                        raise ValidationError("Vivado vendor constant source record is invalid")
                    origin = int(source["source"][1:])
                    if origin in confirmed or len(set(source["frontier_sources"])) != len(source["frontier_sources"]):
                        raise ValidationError("Vivado vendor constant source or frontier is duplicated")
                    confirmed[origin] = source["constant_value"]
                    required_frontiers.update(source["frontier_sources"])
                observed_frontiers = set()
                observed_objects = set()
                for item in document["frontiers"]:
                    if (not isinstance(item,dict) or set(item) != {"source_origin","native_cell","variables","assignments"}
                            or item["source_origin"] not in required_frontiers or not isinstance(item["native_cell"],str) or not item["native_cell"]
                            or isinstance(item["variables"],bool) or not isinstance(item["variables"],int) or not 0 <= item["variables"] <= 12
                            or isinstance(item["assignments"],bool) or not isinstance(item["assignments"],int)
                            or item["assignments"] != 1 << item["variables"]):
                        raise ValidationError("Vivado vendor functional frontier record is invalid")
                    key = (item["source_origin"],item["native_cell"])
                    if key in observed_objects:
                        raise ValidationError("Vivado vendor functional frontier is duplicated")
                    observed_objects.add(key)
                    observed_frontiers.add(item["source_origin"])
                if observed_frontiers != required_frontiers:
                    raise ValidationError("Vivado vendor functional frontier coverage differs")
            digest = hashlib.sha256()
            counts = {"unobservable-source-closure":0,"constant-state":0}
            vendor_count = 0
            for row in self.connection.execute("SELECT origin,reason,constant_value,initialization FROM eliminated_sources ORDER BY origin"):
                origin,reason,value,initialization = row
                if (not isinstance(origin,int) or origin < 0 or reason not in counts or
                    (reason == "unobservable-source-closure" and (value is not None or initialization is not None)) or
                    (reason == "constant-state" and (value not in (0,1) or initialization not in {"specified","vendor-confirmed"}))):
                    raise ValidationError("Vivado elimination source proof is invalid or conditional")
                if initialization == "vendor-confirmed":
                    if confirmed.get(origin) != value:
                        raise ValidationError("Vivado vendor constant selection differs from its confirmation")
                    vendor_count += 1
                counts[reason] += 1
                digest.update((json.dumps(row,separators=(",",":"))+"\n").encode())
            if (digest.hexdigest() != self.header.get("proof_rows_sha256") or
                counts["unobservable-source-closure"] != self.header.get("unobservable_source_cells") or
                counts["constant-state"] != self.header.get("constant_state_cells")):
                raise ValidationError("Vivado optimization proof row seal disagrees")
            declared_vendor_count = self.header.get("vendor_confirmed_constant_state_cells",0)
            if (isinstance(declared_vendor_count,bool) or not isinstance(declared_vendor_count,int)
                    or vendor_count != declared_vendor_count or (confirmed and not vendor_count)):
                raise ValidationError("Vivado vendor confirmed source count differs")
            digest = hashlib.sha256()
            for row in self.connection.execute("SELECT origin,name FROM source_names ORDER BY origin"):
                if not isinstance(row[1],str) or not row[1]:
                    raise ValidationError("Vivado optimization proof source name is invalid")
                digest.update((json.dumps(row,separators=(",",":"))+"\n").encode())
            if digest.hexdigest() != self.header.get("source_names_sha256"):
                raise ValidationError("Vivado optimization proof source-name seal disagrees")
            self.require_unchanged()
        except (sqlite3.DatabaseError, ValueError, TypeError, KeyError, OverflowError) as error:
            self.close()
            raise ValidationError("Vivado optimization proof document is malformed") from error
        except BaseException:
            self.close()
            raise

    def require_unchanged(self):
        if self._closed:
            raise ValidationError("Vivado optimization proof reader is closed")
        require_store_sources_unchanged((self._source_binding,))

    def require_eliminated_source(self, identity: str) -> dict[str, Any]:
        if self._closed:
            raise ValidationError("Vivado optimization proof reader is closed")
        if not isinstance(identity,str) or not identity.startswith("s") or not identity[1:].isdigit():
            raise ValidationError("unsupported Vivado eliminated-source identity")
        origin = int(identity[1:])
        if identity != f"s{origin}" or origin > 2**63-1:
            raise ValidationError("invalid Vivado eliminated-source identity")
        row = self.connection.execute("SELECT p.reason,p.constant_value,p.initialization,n.name FROM eliminated_sources p JOIN source_names n USING(origin) WHERE p.origin=?", (origin,)).fetchone()
        if row is None:
            raise ValidationError("timing elimination references an unproved or conditional source")
        return {"source":identity,"reason":row[0],"constant_value":row[1],"initialization":row[2],"source_cell":row[3]}

    def close(self):
        if not self._closed:
            self.connection.close()
            self._closed = True

    def __getitem__(self, key):
        if self._closed:
            raise ValidationError("Vivado optimization proof reader is closed")
        return self.header[key]

    def __iter__(self):
        return iter(self.header)

    def __len__(self):
        return len(self.header)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def checked_native_optimization_context(proof, mapped_binding, lineage_binding, *, fpga,
        mapped_sha256, synthesized_checkpoint_sha256=""):
    """Pass an already checked physical owner to the native data plane.

    The proof reader performs the canonical publication/row checks once. Its
    retained generation, rather than a new snapshot, binds that validation.
    The supplied mapped/lineage leases come from the same physical assembler;
    native point queries check endpoint, mapped and routed inventory identity.
    This is not an importer for an arbitrary saved status document.
    """
    if not isinstance(proof,OptimizationProofView):
        raise ValidationError("native optimization context requires the checked proof reader")
    proof.require_unchanged()
    if not isinstance(mapped_binding,StoreSource)or not isinstance(lineage_binding,StoreSource):
        raise ValidationError("native optimization context requires physical generation leases")
    if (mapped_binding.schema!="xilinx-mapped-index-v2" or lineage_binding.schema!="vivado-lineage-store-v2"
            or proof['fpga']!=fpga or proof['stage']!='routed' or proof['mapped_index_sha256']!=mapped_sha256):
        raise ValidationError("native optimization context belongs to another physical input")
    if 'vendor_confirmation_document_sha256'in proof:
        if proof['synthesized_checkpoint_sha256']!=synthesized_checkpoint_sha256:
            raise ValidationError("native optimization context requires the checked synthesized checkpoint")
    bindings=(proof._source_binding,mapped_binding,lineage_binding)
    require_store_sources_unchanged(bindings)
    return dict(fpga=fpga,mapped_sha256=mapped_sha256,synthesized_checkpoint_sha256=synthesized_checkpoint_sha256,
        inputs=[dict(path=str(binding.path),generation=list(binding.generation))for binding in bindings])
