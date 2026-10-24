"""Source-to-tool lineage without preservation constraints or a Python graph.

The mapped index owns source cells; a small signature table owns the derived
same-input primitive equivalence classes. Tool inventories own observed
objects. An absent source tag is recorded as unretained, not certified as a
functionally correct deletion. Complete timing coverage remains a separate gate.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from .errors import ValidationError

LINEAGE_POLICY = "vivado-optimized-source-lineage-v1"
INVENTORY_HEADER = ["name", "ref_name", "source_origin", "transformations_hex"]


def primitive_function_signature(cell: Mapping[str, Any]) -> str:
    """Only identical primitives, parameters and input bits can alias here.

    Outputs have independent net names, but must have identical pin interfaces.
    This is deliberately not a heuristic equivalence or a retiming proof.
    """
    value = {"type": cell["type"], "parameters": cell.get("parameters", {}),
             "pins": {port: {"direction": direction,
                              "bits": cell["connections"][port] if direction != "output"
                              else len(cell["connections"][port])}
                      for port, direction in cell["port_directions"].items()}}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def import_cell_lineage(mapped: Path, functions: Path, inventories: Mapping[str, Path],
                        output: Path, *, mapped_sha256: str,
                        retain_stage: str = "routed") -> dict[str, Any]:
    """Stream all observed tags, allowing deletion, replication and type changes."""
    if retain_stage not in inventories:
        raise ValidationError("Vivado retained lineage stage is absent")
    connection = sqlite3.connect(output, uri=True)
    try:
        connection.execute("ATTACH DATABASE ? AS mapped", (f"file:{mapped.resolve()}?mode=ro&immutable=1",))
        connection.execute("ATTACH DATABASE ? AS functions", (f"file:{functions.resolve()}?mode=ro&immutable=1",))
        owner = connection.execute("SELECT value FROM functions.metadata WHERE key='mapped_index_sha256'").fetchone()
        if owner != (mapped_sha256,):
            raise ValidationError("Vivado function signatures belong to a different mapped input")
        source_count = connection.execute("SELECT count(*) FROM mapped.cells").fetchone()[0]
        if connection.execute("SELECT count(*) FROM functions.signatures").fetchone()[0] != source_count or connection.execute(
            "SELECT 1 FROM mapped.cells c LEFT JOIN functions.signatures f ON c.seq=f.seq WHERE f.seq IS NULL LIMIT 1"
        ).fetchone():
            raise ValidationError("Vivado function-signature source coverage disagrees")
        connection.executescript("""
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE objects(stage TEXT,name TEXT,ref TEXT,origin INTEGER,transformations TEXT,
                                 PRIMARY KEY(stage,name)) WITHOUT ROWID;
            CREATE INDEX objects_origin ON objects(stage,origin);
        """)
        stages = {}
        for stage, path in inventories.items():
            inventory_digest = hashlib.sha256()
            with path.open("rb") as stream:
                def inventory_lines():
                    for raw in stream:
                        inventory_digest.update(raw)
                        yield raw.decode("utf-8")
                rows = csv.reader(inventory_lines(), delimiter="\t", quoting=csv.QUOTE_NONE)
                if next(rows, None) != INVENTORY_HEADER:
                    raise ValidationError("Vivado lineage inventory header is invalid")
                for row in rows:
                    try:
                        if len(row) != 4 or not row[1] or not row[2].startswith("s"):
                            raise ValueError("invalid row")
                        origin = int(row[2][1:])
                        name = row[0]
                        if not name or origin < 0:
                            raise ValueError("invalid identity")
                        transformations = bytes.fromhex(row[3]).decode("utf-8")
                        connection.execute("INSERT INTO objects VALUES (?,?,?,?,?)", (stage,name,row[1],origin,transformations))
                    except (ValueError, UnicodeError, sqlite3.IntegrityError) as error:
                        raise ValidationError("Vivado lineage contains invalid or duplicate objects") from error
            if connection.execute("SELECT 1 FROM objects a LEFT JOIN mapped.cells c ON a.origin=c.seq WHERE a.stage=? AND c.seq IS NULL LIMIT 1", (stage,)).fetchone():
                raise ValidationError("Vivado lineage contains an unknown source origin")
            objects, observed = connection.execute("SELECT count(*),count(DISTINCT origin) FROM objects WHERE stage=?", (stage,)).fetchone()
            replicated = connection.execute("SELECT count(*) FROM (SELECT origin FROM objects WHERE stage=? GROUP BY origin HAVING count(*)>1)", (stage,)).fetchone()[0]
            changed = connection.execute("SELECT count(*) FROM objects a JOIN mapped.cells c ON c.seq=a.origin WHERE a.stage=? AND a.ref!=c.type", (stage,)).fetchone()[0]
            stages[stage] = {"observed_objects": objects, "source_cells_with_lineage": observed,
                             "unretained_source_cells": source_count-observed,
                             "source_cells_with_multiple_objects": replicated, "changed_type_objects": changed,
                             "inventory_sha256": inventory_digest.hexdigest()}
            # Only routed objects have a downstream name-resolution consumer.
            # Reuse these SQLite pages instead of retaining two full snapshots.
            if stage != retain_stage:
                connection.execute("DELETE FROM objects WHERE stage=?", (stage,))
        connection.executemany("INSERT INTO metadata VALUES (?,?)", (
            ("mapped_index_sha256", mapped_sha256),
            ("stages_json", json.dumps(stages, sort_keys=True)),
            ("retained_stage", retain_stage),
        ))
        connection.commit()
        return {"status": "pass", "provider": LINEAGE_POLICY, "source_cells": source_count,
                "stages": stages, "output": str(output),
                "qualification": "observed-source-dispositions; not-an-equivalence-proof"}
    finally:
        connection.close()


def resolve_timing_query(mapped: Path, functions: Path, lineage: Path, source: Path,
                         output: Path, *, boundary: bool = False,
                         optimization_proofs: Path | None = None,
                         allow_eliminated: bool = False) -> dict[str, Any]:
    """Resolve requested source pins to all typed descendants or identical inputs.

    Candidate collections are measured as a worst physical endpoint bound. A
    changed or missing primitive that cannot be associated is rejected. No
    endpoint is preserved merely to make this query succeed.
    """
    connection = sqlite3.connect(f"file:{lineage.resolve()}?mode=ro", uri=True)
    try:
        connection.execute("ATTACH DATABASE ? AS mapped", (f"file:{mapped.resolve()}?mode=ro&immutable=1",))
        connection.execute("ATTACH DATABASE ? AS functions", (f"file:{functions.resolve()}?mode=ro&immutable=1",))
        if allow_eliminated:
            if optimization_proofs is None:
                raise ValidationError("eliminated endpoint queries require a supported source proof owner")
            connection.execute("ATTACH DATABASE ? AS proofs", (f"file:{optimization_proofs.resolve()}?mode=ro&immutable=1",))
            owner = {key:json.loads(value) for key,value in connection.execute("SELECT key,value FROM proofs.metadata")}
            lineage_owner = dict(connection.execute("SELECT key,value FROM metadata"))
            if owner.get("mapped_index_sha256") != lineage_owner.get("mapped_index_sha256") or owner.get("stage") != "routed":
                raise ValidationError("eliminated timing query proof belongs to a different source or stage")

        @lru_cache(maxsize=4096)
        def candidates(name: str, cell_only: bool, required: bool = True) -> str:
            cell, separator, pin = (name, "", "") if cell_only else name.rpartition("/")
            if not cell_only and not separator:
                raise ValidationError(f"Vivado timing pin has no source cell: {name}")
            # Query names use Vivado's doubled-backslash NAME spelling; the
            # indexed source owner retains the original escaped identifier.
            cell = cell.replace("\\\\", "\\")
            rows = connection.execute("""
                SELECT DISTINCT a.name,a.transformations,a.ref FROM mapped.cells c
                JOIN functions.signatures f ON f.seq=c.seq
                JOIN functions.signatures equivalent ON equivalent.signature=f.signature
                JOIN objects a ON a.origin=equivalent.seq AND a.stage='routed' AND a.ref=c.type
                WHERE c.id=? AND (a.origin=c.seq OR NOT EXISTS (
                    SELECT 1 FROM objects own WHERE own.stage='routed' AND own.origin=c.seq
                )) ORDER BY a.name
            """, (cell,))
            encoded = []
            for row in rows:
                changes = row[1].upper()
                if any(marker in changes for marker in ("RETIM", "REGISTER_OPT", "AUTOPIPELINE", "INSERT_NEGEDGE", "SHIFT_REGISTER_TO_PIPELINE")) or (row[2].startswith("FD") and "MISC_OPT" in changes):
                    raise ValidationError(f"Vivado timing source has an unsupported sequential transformation: {name}")
                actual = row[0] if cell_only else row[0]+"/"+pin
                encoded.append(actual.encode().hex())
            if not encoded and required:
                if allow_eliminated:
                    proof = connection.execute("SELECT p.origin FROM mapped.cells c JOIN proofs.eliminated_sources p ON p.origin=c.seq JOIN proofs.source_names n ON n.origin=p.origin AND n.name=c.id WHERE c.id=?", (cell,)).fetchone()
                    if proof is not None:
                        return f"optimized-away:s{proof[0]}"
                raise ValidationError(f"Vivado optimized timing source has no proven endpoint association: {name}")
            return ",".join(encoded)

        count = 0
        with source.open(encoding="utf-8", newline="") as incoming, output.open("w", encoding="utf-8", newline="") as outgoing:
            reader, writer = csv.reader(incoming, delimiter="\t"), csv.writer(outgoing, delimiter="\t", lineterminator="\n")
            header = next(reader, [])
            if boundary:
                if len(header) != 6 or header[-1] != "boundary_cell_hex":
                    raise ValidationError("Vivado boundary query header is invalid")
                writer.writerow(header+["resolved_boundary_cells_hex"])
            else:
                if len(header) != 8 or header[-1] != "cone_anchor_object_hex":
                    raise ValidationError("Vivado logic query header is invalid")
                writer.writerow(header+["resolved_start_pins_hex", "resolved_end_pins_hex", "resolved_cone_pins_hex"])
            for row in reader:
                if len(row) != len(header):
                    raise ValidationError("Vivado timing query row is invalid")
                if boundary:
                    hints = [candidates(bytes.fromhex(row[5]).decode(), True) if row[5] else ""]
                else:
                    # A cone anchor is consumed only if the endpoint query has
                    # no physical path. An optimized-away, unused anchor must
                    # not reject a valid endpoint measurement. The Tcl exporter
                    # still requires the actual anchor if it takes that branch.
                    hints = [candidates(bytes.fromhex(row[index+1]).decode(), False, required=index != 6) if row[index]=="pin" else "" for index in (2,4,6)]
                writer.writerow(row+hints)
                count += 1
        return {"status": "pass", "queries": count, "output": str(output), "provider": LINEAGE_POLICY}
    finally:
        connection.close()
