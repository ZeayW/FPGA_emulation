"""Source-to-tool lineage without preservation constraints or a Python graph.

The mapped index owns source cells; a small signature table owns the derived
same-input primitive equivalence classes. Tool inventories own observed
objects. An absent source tag is recorded as unretained, not certified as a
functionally correct deletion. Complete timing coverage remains a separate gate.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from emuflow.common.errors import ValidationError

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
    from emuflow.common.io import read_json, write_json
    from emuflow.common.native_tools import resolve_native_stage_command

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
    """Publish all proven native descendant associations in one generation."""
    from emuflow.physical.vivado.vivado_timing_query_native import resolve_vivado_query
    return resolve_vivado_query(mapped, functions, lineage, source, output, boundary=boundary)
