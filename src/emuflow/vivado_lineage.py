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
                         output: Path, *, boundary: bool = False) -> dict[str, Any]:
    """Resolve requested source pins to all typed descendants or identical inputs.

    Candidate collections are measured as a worst physical endpoint bound. A
    changed or missing primitive that cannot be associated is rejected. No
    endpoint is preserved merely to make this query succeed.
    """
    connection = sqlite3.connect(f"file:{lineage.resolve()}?mode=ro", uri=True)
    try:
        connection.execute("ATTACH DATABASE ? AS mapped", (f"file:{mapped.resolve()}?mode=ro&immutable=1",))
        connection.execute("ATTACH DATABASE ? AS functions", (f"file:{functions.resolve()}?mode=ro&immutable=1",))

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
