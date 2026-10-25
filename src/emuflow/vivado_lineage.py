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
LINEAGE_SCHEMA = "emuflow.vivado-cell-lineage/v2"
PLACEMENT_COORDINATE_SYSTEM = "native-device-tile-column-row-v1"
INVENTORY_HEADER = ["name", "ref_name", "source_origin", "transformations_hex",
                    "site", "bel", "tile_column", "tile_row", "primitive_level"]


class _UnassociatedTimingSource(ValidationError):
    def __init__(self, name, cell, origin, cell_only):
        super().__init__(f"Vivado optimized timing source has no proven endpoint association: {name}")
        self.name, self.cell, self.origin, self.cell_only = name, cell, origin, cell_only


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
    """Import tool observations in the shared core; host traffic stays bounded."""
    import tempfile
    import subprocess
    from .io import read_json, write_json
    from .native_tools import resolve_native_stage_command

    if retain_stage not in inventories:
        raise ValidationError("Vivado retained lineage stage is absent")
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".vivado-lineage-", dir=output.parent) as temporary:
        root = Path(temporary)
        request, summary = root / "request.json", root / "summary.json"
        write_json(request, {"schema": "emuflow.vivado-lineage-import/v1",
            "mapped_sha256": mapped_sha256, "retain_stage": retain_stage,
            "stages": list(inventories)})
        completed = subprocess.run([
            *resolve_native_stage_command("phase7", "import-vivado-lineage"),
            str(Path(mapped).resolve()), str(Path(functions).resolve()), str(request),
            str(output), str(summary), *(str(Path(p).resolve()) for p in inventories.values()),
        ], capture_output=True, text=True)
        if completed.returncode:
            raise ValidationError("native Vivado lineage import failed: " + completed.stderr.strip())
        result = read_json(summary)
    if (result.get("schema") != LINEAGE_SCHEMA or result.get("coordinate_system") != PLACEMENT_COORDINATE_SYSTEM
            or result.get("mapped_index_sha256") != mapped_sha256 or result.get("status") != "pass"):
        raise ValidationError("native Vivado lineage summary identity is invalid")
    return result


def resolve_timing_query(mapped: Path, functions: Path, lineage: Path, source: Path,
                         output: Path, *, boundary: bool = False,
                         optimization_proofs: Path | None = None,
                         allow_eliminated: bool = False,
                         stage: str = "routed",
                         diagnostic_unresolved_path: Path | None = None) -> dict[str, Any]:
    """Resolve requested source pins to all typed descendants or identical inputs.

    Candidate collections are measured as a worst physical endpoint bound. A
    changed or missing primitive that cannot be associated is rejected. No
    endpoint is preserved merely to make this query succeed.
    """
    if stage not in {"synthesized", "routed"}:
        raise ValidationError("unsupported Vivado timing-query lineage stage")
    connection = sqlite3.connect(f"file:{lineage.resolve()}?mode=ro&immutable=1", uri=True)
    diagnostic = None
    diagnostic_complete = False
    try:
        connection.execute("ATTACH DATABASE ? AS mapped", (f"file:{mapped.resolve()}?mode=ro&immutable=1",))
        connection.execute("ATTACH DATABASE ? AS functions", (f"file:{functions.resolve()}?mode=ro&immutable=1",))
        lineage_owner = dict(connection.execute("SELECT key,value FROM metadata"))
        if lineage_owner.get("retained_stage") != stage:
            raise ValidationError("Vivado timing query belongs to a different source or stage")
        if diagnostic_unresolved_path is not None:
            if diagnostic_unresolved_path.exists():
                raise ValidationError("Vivado unresolved-source diagnostic already exists")
            if diagnostic_unresolved_path.resolve() == output.resolve():
                raise ValidationError("Vivado unresolved-source report must differ from the query output")
            diagnostic = sqlite3.connect(diagnostic_unresolved_path)
            diagnostic.executescript("""
                PRAGMA journal_mode=OFF;
                PRAGMA synchronous=OFF;
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
                CREATE TABLE unresolved_sources(
                    name TEXT NOT NULL,role TEXT NOT NULL,source_cell TEXT NOT NULL,
                    source_origin INTEGER,cell_only INTEGER NOT NULL CHECK(cell_only IN (0,1)),
                    uses INTEGER NOT NULL CHECK(uses>0),PRIMARY KEY(name,role)
                ) WITHOUT ROWID;
                CREATE INDEX unresolved_by_origin ON unresolved_sources(source_origin,role);
            """)
            diagnostic.executemany("INSERT INTO metadata VALUES (?,?)", ((key,json.dumps(value)) for key,value in {
                "schema":"emuflow.vivado-unassociated-timing-sources/v1", "status":"running", "stage":stage,
                "mapped_index_sha256":lineage_owner["mapped_index_sha256"],
                "stage_inventory_sha256":json.loads(lineage_owner["stages_json"])[stage]["inventory_sha256"],
            }.items()))
        if allow_eliminated:
            if optimization_proofs is None:
                raise ValidationError("eliminated endpoint queries require a supported source proof owner")
            connection.execute("ATTACH DATABASE ? AS proofs", (f"file:{optimization_proofs.resolve()}?mode=ro&immutable=1",))
            owner = {key:json.loads(value) for key,value in connection.execute("SELECT key,value FROM proofs.metadata")}
            if owner.get("mapped_index_sha256") != lineage_owner.get("mapped_index_sha256") or owner.get("stage") != stage:
                raise ValidationError("eliminated timing query proof belongs to a different source or stage")

        @lru_cache(maxsize=4096)
        def candidates(name: str, cell_only: bool, required: bool = True) -> str:
            cell, separator, pin = (name, "", "") if cell_only else name.rpartition("/")
            if not cell_only and not separator:
                raise ValidationError(f"Vivado timing pin has no source cell: {name}")
            # Query names use Vivado's doubled-backslash NAME spelling; the
            # indexed source owner retains the original escaped identifier.
            cell = cell.replace("\\\\", "\\")
            source_cell = connection.execute("""
                SELECT c.seq,c.type,f.signature FROM mapped.cells c
                JOIN functions.signatures f ON f.seq=c.seq WHERE c.id=?
            """, (cell,)).fetchone()
            if source_cell is None:
                if not required:
                    return ""
                raise _UnassociatedTimingSource(name,cell,None,cell_only)
            origin, reference, signature = source_cell
            own = connection.execute("SELECT 1 FROM objects INDEXED BY objects_origin WHERE stage=? AND origin=? LIMIT 1", (stage,origin)).fetchone()
            if own is not None:
                # Point lookup for retained origins. A disjunction with the
                # alias branch lets SQLite scan every stage object per pin.
                rows = connection.execute("SELECT name,transformations,ref FROM objects INDEXED BY objects_origin WHERE stage=? AND origin=? AND ref=? ORDER BY name", (stage,origin,reference))
            else:
                # Force the small signature class before the stage/origin
                # index lookup, preserving all actual equivalent descendants.
                rows = connection.execute("""
                    SELECT DISTINCT a.name,a.transformations,a.ref
                    FROM functions.signatures equivalent
                    CROSS JOIN objects a INDEXED BY objects_origin ON a.origin=equivalent.seq AND a.stage=?
                    WHERE equivalent.signature=? AND a.ref=? ORDER BY a.name
                """, (stage,signature,reference))
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
                raise _UnassociatedTimingSource(name,cell,origin,cell_only)
            return ",".join(encoded)

        def hint(name, cell_only, role, required=True):
            try:
                return candidates(name,cell_only,required)
            except _UnassociatedTimingSource as error:
                if diagnostic is None:
                    raise
                diagnostic.execute("""INSERT INTO unresolved_sources VALUES (?,?,?,?,?,1)
                    ON CONFLICT(name,role) DO UPDATE SET uses=uses+1""",
                    (error.name,role,error.cell,error.origin,int(error.cell_only)))
                # Deliberately invalid as a native pin/cell hint. A caller
                # ignoring the incomplete status still cannot measure this TSV.
                return "unresolved-source"

        count = 0
        unresolved_rows = 0
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
                    hints = [hint(bytes.fromhex(row[5]).decode(),True,"boundary") if row[5] else ""]
                else:
                    # A cone anchor is consumed only if the endpoint query has
                    # no physical path. An optimized-away, unused anchor must
                    # not reject a valid endpoint measurement. The Tcl exporter
                    # still requires the actual anchor if it takes that branch.
                    hints = [hint(bytes.fromhex(row[index+1]).decode(),False,{2:"start",4:"end",6:"cone"}[index],required=index != 6) if row[index]=="pin" else "" for index in (2,4,6)]
                writer.writerow(row+hints)
                count += 1
                unresolved_rows += int("unresolved-source" in hints)
        result = {"status":"incomplete" if unresolved_rows else "pass", "queries":count,
                  "output":str(output), "provider":LINEAGE_POLICY, "stage":stage}
        if diagnostic is not None:
            uses, sources = diagnostic.execute("SELECT COALESCE(SUM(uses),0),COUNT(*) FROM unresolved_sources").fetchone()
            result.update(unresolved_query_rows=unresolved_rows, unresolved_required_endpoint_uses=uses,
                          unresolved_source_pins=sources, unresolved_source_report=str(diagnostic_unresolved_path))
            diagnostic.executemany("INSERT OR REPLACE INTO metadata VALUES (?,?)", ((key,json.dumps(value)) for key,value in
                {"status":result["status"],"queries":count,"unresolved_query_rows":unresolved_rows,"complete":True}.items()))
            diagnostic_complete = True
        return result
    finally:
        if diagnostic is not None:
            if not diagnostic_complete:
                diagnostic.execute("INSERT OR REPLACE INTO metadata VALUES ('status','\"failed\"')")
            diagnostic.commit()
            diagnostic.close()
        connection.close()
