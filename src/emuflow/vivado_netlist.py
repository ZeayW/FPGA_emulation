"""Lower provider-neutral mapped EmuIR into Vivado primitive Verilog."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from .errors import ValidationError


def emit_vivado_mapped_verilog(
    ir_path: Path,
    output_path: Path,
    report_path: Optional[Path] = None,
) -> Mapping[str, Any]:
    """Export the current indexed Xilinx input; no whole-EmuIR fallback."""
    from .emuir_compact import is_compact_emuir
    from .emuir_storage import is_emuir_sqlite
    from .vivado_indexed_netlist import emit_indexed_vivado_verilog

    if not is_emuir_sqlite(ir_path) or not is_compact_emuir(ir_path):
        raise ValidationError("Vivado export requires current indexed EmuIR")
    return emit_indexed_vivado_verilog(ir_path, output_path, report_path)
