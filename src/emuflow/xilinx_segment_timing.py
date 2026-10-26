"""Current typed RapidWright routed-timing projection entry."""
from pathlib import Path
from typing import Any, Mapping, Optional

from .errors import ValidationError

XILINX_SEGMENT_QUALIFICATION = (
    "rapidwright-lightweight-routed-setup-no-hold-fixed-lvds-iob-boundary"
)


def build_xilinx_segment_timing_bundle(
    *, boundary_identity_path: Path, mapped_path: Path, timing_path: Path,
    boundary_output_path: Path, logic_identity_path: Optional[Path] = None,
    logic_output_path: Optional[Path] = None, local_identity_path: Optional[Path] = None,
    local_output_path: Optional[Path] = None, mapped_value: Optional[Mapping[str, Any]] = None,
    timing_value: Optional[Mapping[str, Any]] = None,
    timing_validation: Optional[Mapping[str, Any]] = None, source_ir_path: Optional[Path] = None,
) -> dict[str, Any]:
    if ((logic_identity_path is None) != (logic_output_path is None)
            or (local_identity_path is None) != (local_output_path is None)):
        raise ValidationError("routed timing identity and output must be provided together")
    if getattr(mapped_value, "store_path", None) is None:
        raise ValidationError("routed timing requires the current typed mapped-index owner")
    from .routed_segments_native import build_routed_segments
    return build_routed_segments(
        boundary_identity_path=boundary_identity_path, mapped_path=mapped_path,
        timing_path=timing_path, boundary_output_path=boundary_output_path,
        logic_identity_path=logic_identity_path, logic_output_path=logic_output_path,
        local_identity_path=local_identity_path, local_output_path=local_output_path,
        mapped_value=mapped_value, timing_value=timing_value,
        timing_validation=timing_validation, source_ir_path=source_ir_path,
    )
