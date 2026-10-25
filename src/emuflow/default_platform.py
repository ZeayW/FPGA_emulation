"""Packaged research platforms, shared by the CLI and Python flow API."""

from pathlib import Path
from typing import Any, Dict

from .errors import ValidationError
from .io import read_json, write_json
from .platform import Platform


RESEARCH_PLATFORM_PROVIDER = "rapidwright-hp-cable-bank-linear-ratio-v1"
DEFAULT_WIRES_PER_BANK = 20
RESEARCH_TRANSPORT_FORMATS = ("abstract-bit-v1", "lvds-word-v2")
_PLATFORM_FILES = {
    2: "rapidwright_xcvu19p_2fpga_p2p.json",
    4: "rapidwright_xcvu19p_4fpga_mesh.json",
}
_PLATFORM_DIRECTORY = Path(__file__).resolve().parent / "data" / "platforms"
DEFAULT_PLATFORM_PATH = _PLATFORM_DIRECTORY / _PLATFORM_FILES[4]


def research_platform_path(fpga_count: int = 4) -> Path:
    """Return a bundled BoardDB without depending on a source checkout."""

    if (isinstance(fpga_count, bool) or not isinstance(fpga_count, int)
            or fpga_count not in _PLATFORM_FILES):
        raise ValidationError("research platform FPGA count must be 2 or 4")
    return _PLATFORM_DIRECTORY / _PLATFORM_FILES[fpga_count]


def materialize_research_platform(
    output_path: Path, *, fpga_count: int = 4,
    wires_per_bank: int = DEFAULT_WIRES_PER_BANK,
    transport_format: str = "abstract-bit-v1",
) -> Dict[str, Any]:
    """Write the selected model for standalone phases or explicit selection."""

    if (isinstance(wires_per_bank, bool) or not isinstance(wires_per_bank, int)
            or wires_per_bank <= 0):
        raise ValidationError("effective differential data pairs per HP bank must be a positive integer")
    if transport_format not in RESEARCH_TRANSPORT_FORMATS:
        raise ValidationError("unsupported research transport format")
    if transport_format == "lvds-word-v2" and wires_per_bank != 20:
        raise ValidationError("native LVDS word research model requires twenty data pairs per bank")
    value = read_json(research_platform_path(fpga_count))
    if wires_per_bank != DEFAULT_WIRES_PER_BANK:
        value["platform"].pop("virtual_board_wiring", None)
        value["platform"]["name"] += f"_wires{wires_per_bank}"
        value["platform"]["description"] = value["platform"]["description"].replace(
            f"{DEFAULT_WIRES_PER_BANK} effective differential data pairs per bank",
            f"{wires_per_bank} effective differential data pairs per bank",
        )
    for link in value["links"]:
        link["wires_per_bank"] = wires_per_bank
        link["data_lanes_per_direction"] = link["bank_count"] * wires_per_bank
    if transport_format == "lvds-word-v2":
        value["platform"]["virtual_board_model"] = "xcvu19p-hp-lvds-word-cable-v2"
        value["platform"]["name"] += "_word8"
        value["platform"]["description"] += "; explicit native 8-bit LVDS word format with separate forwarded clocks; physical integration under qualification"
        for link in value["links"]:
            link.update(mode="source_synchronous",payload_bits_per_lane_per_cycle=8,
                        max_line_rate_gbps_per_lane=1.6,latency_cycles=6)
    platform = Platform.from_dict(value)
    write_json(output_path, platform.to_dict())
    return {
        "status": "pass",
        "provider": RESEARCH_PLATFORM_PROVIDER if transport_format=="abstract-bit-v1" else "rapidwright-hp-cable-bank-lvds-word-v2",
        "qualification": "model-only",
        "platform": platform.name,
        "fpga_count": len(platform.fpgas),
        "wires_per_bank": wires_per_bank,
        "transport_format": transport_format,
        "output": str(output_path),
    }
