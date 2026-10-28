"""Independent locator bridge for lineage component regressions."""
from pathlib import Path
from typing import Any, Mapping
from emuflow.common.errors import ValidationError
from emuflow.physical.vivado.vivado_lineage import LINEAGE_SCHEMA, PLACEMENT_COORDINATE_SYSTEM

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
