"""Finite identity contract for prepared NVDLA validation inputs."""
import json
from pathlib import Path
from typing import Any, Dict

from emuflow.common.paths import DATA_ROOT

CATALOG_PATH = DATA_ROOT / "designs/nvdla-catalog.json"
COMPAT_PATH = DATA_ROOT / "designs/nvdla_compat.v"
PHYSICAL_MEMORY_POLICY = "physically-implementable-shared-memory-model-v1"


def _catalog_entry(catalog_path: Path) -> Dict[str, Any]:
    value = json.loads(catalog_path.read_text(encoding="utf-8"))
    if value.get("schema") != "emuflow.rtl-catalog/v1":
        raise ValueError("RTL catalog schema is invalid")
    matches = [item for item in value.get("designs", []) if item.get("id") == "nvdla"]
    if len(matches) != 1:
        raise ValueError("RTL catalog must contain exactly one NVDLA entry")
    return matches[0]
