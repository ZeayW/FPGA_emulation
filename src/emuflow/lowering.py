"""Public bridge to the sole indexed placement-lowering implementation."""
from pathlib import Path
from typing import Any, Dict, Optional

from .placement_lowering_native import lower_placement


PLACEMENT_IR_REPORT_SCHEMA = "emuflow.placement-ir-report/v1"


def run_placement_ir_lowering_from_split(
    split_path: Path,
    fpga: str,
    transport_path: Path,
    transport_ir_path: Path,
    output_path: Path,
    report_path: Optional[Path] = None,
    boundary_identity_path: Optional[Path] = None,
) -> Dict[str, Any]:
    return lower_placement(
        split_path, fpga, transport_path, transport_ir_path,
        output_path, report_path, boundary_identity_path,
    )
