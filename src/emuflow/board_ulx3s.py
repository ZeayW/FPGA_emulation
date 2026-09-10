"""Source-backed open-toolchain board wiring, not a measured link model.

Keep this separate from BoardDB until the asynchronous transport has a checked
timing contract. A cable connection alone cannot supply latency_cycles.
"""

from .errors import ValidationError

SOURCE_COMMIT = "6a92cec6b177191c5b0f80e260013a1f8ec147dd"
SOURCE_ROOT = f"https://github.com/emard/ulx3s/blob/{SOURCE_COMMIT}"
PART = "LFE5U-85F-6BG381C"


def ulx3s_pair_profile() -> dict:
    """Fixed GPIO harness; each board uses its own independent oscillator."""
    # GP0/1 are not among the ESP32/ADC-shared signals listed in MANUAL.md.
    # Use single-ended LVCMOS33: these are NOT differential LVDS pairs.
    pins = {
        "clk_25mhz": {"site": "G2", "direction": "input"},
        "reset_n": {"site": "D6", "direction": "input"},
        "link_tx": {"site": "B11", "direction": "output", "board_net": "gp[0]"},
        "link_rx": {"site": "C11", "direction": "input", "board_net": "gn[0]"},
    }
    return {
        "schema": "emuflow.open-reference-wiring/v1",
        "id": "ulx3s-85f-v3.0.x-pair-gpio",
        "status": "source_backed_candidate",
        "boards": [{"id": name, "part": PART, "pcb_revision": "3.0.x",
                    "utilization_limit": 0.75,
                    "pins": {k: dict(v) for k, v in pins.items()}}
                   for name in ("board0", "board1")],
        "harness": {
            "origin": "emuflow_reference_assembly_not_an_off_the_shelf_multifpga_board",
            "signals": [["board0.link_tx", "board1.link_rx"],
                        ["board1.link_tx", "board0.link_rx"]],
            "common_ground_required": True,
            "power_rails_connected": False,
            "io_standard": "LVCMOS33",
            "supply_voltage_v": 3.3,
        },
        "clocks": {"local_mhz": 25, "relationship": "asynchronous",
                   "external_delay_bound_ns": None},
        "tools": {"synthesis": "yosys:synth_ecp5",
                  "place_route": "nextpnr-ecp5", "bitstream": "ecppack",
                  "device": "85k", "package": "CABGA381", "speed": "6",
                  "commercial_tools_required": False},
        "sources": {"revision": SOURCE_COMMIT,
                    "pins": f"{SOURCE_ROOT}/doc/constraints/ulx3s_v20.lpf",
                    "manual": f"{SOURCE_ROOT}/doc/MANUAL.md"},
        "qualification": {"electrical_measurement": False,
                          "routed_communication": False,
                          "full_emuflow_phase7": False,
                          "hardware_tested": False},
    }


def ulx3s_endpoint_lpf(board: str) -> str:
    """Emit only audited bindings; do not copy blanket timing exceptions."""
    profile = ulx3s_pair_profile()
    record = next((b for b in profile["boards"] if b["id"] == board), None)
    if record is None:
        raise ValidationError(f"Unknown ULX3S board: {board}")
    lines = ["# Source-backed pin bindings; external timing/CDC not qualified."]
    for name, pin in record["pins"].items():
        lines.append(f'LOCATE COMP "{name}" SITE "{pin["site"]}";')
        pull = "UP" if name == "reset_n" else "NONE"
        drive = " DRIVE=4" if pin["direction"] == "output" else ""
        lines.append(f'IOBUF PORT "{name}" IO_TYPE=LVCMOS33 PULLMODE={pull}{drive};')
    lines.append('FREQUENCY PORT "clk_25mhz" 25 MHZ;')
    return "\n".join(lines) + "\n"


def require_ulx3s_full_flow_qualification() -> None:
    """Never let wiring facts silently stand in for a physical timing result."""
    raise ValidationError(
        "ULX3S full-flow qualification is pending: transport/CDC, ECP5 physical "
        "binding, and complete global timing coverage are not yet validated"
    )
