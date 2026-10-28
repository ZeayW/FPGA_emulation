"""Shared Vivado query, clock and scalar tool contracts."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from emuflow.common.errors import EmuFlowError, ValidationError


def write_vivado_boundary_timing_query(
    ir_path: Path, identity_path: Path, output_path: Path,
) -> Dict[str, Any]:
    """Publish a source-owned native query generation and return its location."""
    from emuflow.physical.vivado.vivado_timing_query_native import write_vivado_boundary_query
    return write_vivado_boundary_query(ir_path, identity_path, output_path)


def _resolve_vivado(executable: Optional[str]) -> str:
    candidate = executable or "vivado"
    resolved = shutil.which(candidate)
    if resolved is None:
        path = Path(candidate).expanduser()
        if path.is_file():
            resolved = str(path.resolve())
    if resolved is None:
        raise EmuFlowError(
            "Vivado executable was not found; pass --physical-vivado or "
            "select --physical-backend open"
        )
    return resolved


def _read_metrics(path: Path) -> Dict[str, str]:
    if not path.is_file():
        raise ValidationError(
            f"Vivado did not emit implementation metrics: {path}"
        )
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "metric\tvalue":
        raise ValidationError("Vivado implementation metrics header is invalid")
    result: Dict[str, str] = {}
    for line_number, line in enumerate(lines[1:], start=2):
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 2 or not fields[0] or fields[0] in result:
            raise ValidationError(
                f"Vivado implementation metrics line {line_number} is invalid"
            )
        result[fields[0]] = fields[1]
    return result


def _integer(metrics: Mapping[str, str], name: str) -> int:
    try:
        value = int(metrics[name])
    except (KeyError, ValueError) as error:
        raise ValidationError(f"Vivado metric {name!r} is not an integer") from error
    if value < 0:
        raise ValidationError(f"Vivado metric {name!r} is negative")
    return value


def _number(metrics: Mapping[str, str], name: str) -> float:
    try:
        return float(metrics[name])
    except (KeyError, ValueError) as error:
        raise ValidationError(f"Vivado metric {name!r} is not numeric") from error
