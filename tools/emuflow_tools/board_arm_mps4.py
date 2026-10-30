"""Source-traceable BoardDB materializer for a three-board Arm MPS4 ring.

The hardware facts below come from Arm document 102577_0000_02_en,
Issue 02 (2024), which is explicitly marked Non-Confidential.  The manual's
Figure 3-22 shows three MPS4 boards connected pairwise through the two 25-Gbps
board-to-board ARC6 ports.  Table A-18 supplies the FPGA package-pin mapping.
Table 3-3 documents the board-to-board reference-clock pool and the reset
section documents the board reset semantics, but neither gives their FPGA
package pins or a clock-to-GTY-site mapping.
Transport word width, user clock, and latency are deliberately caller-provided
because the board manual specifies the physical line-rate ceiling, not a link
protocol or GTY user-interface configuration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from emuflow.common.errors import ValidationError
from emuflow_tools.document_io import read_json
from emuflow.common.json_io import write_json
from emuflow.platform.platform import Platform


ARM_MPS4_BOARDDB_MATERIALIZATION_SCHEMA = (
    "emuflow.arm-mps4-boarddb-materialization/v1"
)
ARM_MPS4_TRM_URL = (
    "https://documentation-service.arm.com/static/669a306a43b8ec1e18652768"
)
AMD_DS890_URL = (
    "https://docs.amd.com/r/en-US/ds890-ultrascale-overview/"
    "Virtex-UltraScale-FPGA-Feature-Summary"
)
MPS4_PART = "xcvu13p-fhga2104-1-e"
MPS4_MANUFACTURER_PART = "XCVU13P-1FHGA2104E"
MPS4_PHYSICAL_LANES = 12
MPS4_B2B_MAX_LINE_RATE_GBPS = 25.0
MPS4_B2B_REFCLK_DEFAULT_MHZ = 156.25


def _connector(
    connector: str,
    mgt: str,
    txp: Sequence[str],
    txn: Sequence[str],
    rxp: Sequence[str],
    rxn: Sequence[str],
) -> Dict[str, Any]:
    if not all(len(pins) == MPS4_PHYSICAL_LANES for pins in (txp, txn, rxp, rxn)):
        raise AssertionError("MPS4 connector table must contain twelve GTY lanes")
    return {
        "connector": connector,
        "mgt": mgt,
        "lanes": [
            {
                "lane": lane,
                "tx_package_pins": {"p": txp[lane], "n": txn[lane]},
                "rx_package_pins": {"p": rxp[lane], "n": rxn[lane]},
            }
            for lane in range(MPS4_PHYSICAL_LANES)
        ],
    }


CONNECTORS: Mapping[str, Mapping[str, Any]] = {
    "J49": _connector(
        "J49",
        "MGT0",
        ("BD42", "BB42", "AY42", "AV42", "AT42", "AP42", "AM42", "AL40", "AK42", "AJ40", "AG40", "AE40"),
        ("BD43", "BB43", "AY43", "AV43", "AT43", "AP43", "AM43", "AL41", "AK43", "AJ41", "AG41", "AE41"),
        ("BC45", "BA45", "AW45", "AU45", "AR45", "AN45", "AL45", "AJ45", "AG45", "AF43", "AE45", "AD43"),
        ("BC46", "BA46", "AW46", "AU46", "AR46", "AN46", "AL46", "AJ46", "AG46", "AF44", "AE46", "AD44"),
    ),
    "J48": _connector(
        "J48",
        "MGT1",
        ("AC40", "AA40", "W40", "U40", "T42", "P42", "M42", "K42", "H42", "F42", "D42", "B42"),
        ("AC41", "AA41", "W41", "U41", "T43", "P43", "M43", "K43", "H43", "F43", "D43", "B43"),
        ("AC45", "AB43", "AA45", "Y43", "W45", "U45", "R45", "N45", "L45", "J45", "G45", "E45"),
        ("AC46", "AB44", "AA46", "Y44", "W46", "U46", "R46", "N46", "L46", "J46", "G46", "E46"),
    ),
}


MPS4_CAPACITY = {
    "lut": 1_728_000,
    "ff": 3_456_000,
    "bram": 2_688,
    "dsp": 12_288,
    "bram18k": 5_376,
    "uram288": 1_280,
    "dsp48": 12_288,
    "io": 832,
}


def _endpoint_binding(fpga: str, connector: str) -> Dict[str, Any]:
    return {"fpga": fpga, **CONNECTORS[connector]}


def materialize_arm_mps4_boarddb(
    output_path: Path,
    *,
    name: str,
    fabric_clock_mhz: float,
    payload_bits_per_lane_per_cycle: int,
    latency_cycles: int,
    utilization_limit: float = 0.75,
) -> Dict[str, Any]:
    """Write a hardware BoardDB for Arm's documented three-MPS4 example."""
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("name: expected a non-empty string")
    if (
        isinstance(fabric_clock_mhz, bool)
        or not isinstance(fabric_clock_mhz, (int, float))
        or float(fabric_clock_mhz) <= 0.0
    ):
        raise ValidationError("fabric_clock_mhz: expected a positive number")
    if (
        isinstance(payload_bits_per_lane_per_cycle, bool)
        or not isinstance(payload_bits_per_lane_per_cycle, int)
        or payload_bits_per_lane_per_cycle <= 0
    ):
        raise ValidationError(
            "payload_bits_per_lane_per_cycle: expected a positive integer"
        )
    if (
        isinstance(latency_cycles, bool)
        or not isinstance(latency_cycles, int)
        or latency_cycles < 0
    ):
        raise ValidationError("latency_cycles: expected a non-negative integer")
    if (
        isinstance(utilization_limit, bool)
        or not isinstance(utilization_limit, (int, float))
        or not 0.0 < float(utilization_limit) <= 1.0
    ):
        raise ValidationError("utilization_limit: expected 0 < value <= 1")

    configured_rate = (
        float(fabric_clock_mhz) * payload_bits_per_lane_per_cycle / 1000.0
    )
    if configured_rate > MPS4_B2B_MAX_LINE_RATE_GBPS * (1.0 + 1e-9):
        raise ValidationError(
            f"configured user-side rate {configured_rate:g} Gbps/lane exceeds "
            f"the documented {MPS4_B2B_MAX_LINE_RATE_GBPS:g} Gbps/lane ceiling"
        )

    fpga_ids = ("mps4_1", "mps4_2", "mps4_3")
    # Figure 3-22 uses both board-to-board ports on every board.  This explicit
    # connector assignment forms the pairwise three-board ring shown there.
    wiring = (
        ("mps4_1", "J49", "mps4_2", "J48"),
        ("mps4_2", "J49", "mps4_3", "J48"),
        ("mps4_3", "J49", "mps4_1", "J48"),
    )
    links = []
    for index, (left, left_connector, right, right_connector) in enumerate(wiring):
        links.append(
            {
                "id": f"mps4_b2b_{index + 1}",
                "endpoints": [left, right],
                "direction": "full_duplex",
                "capacity_sharing": "per_direction",
                "mode": "serial",
                "data_lanes_per_direction": MPS4_PHYSICAL_LANES,
                "payload_bits_per_lane_per_cycle": (
                    payload_bits_per_lane_per_cycle
                ),
                "fabric_clock_mhz": float(fabric_clock_mhz),
                "max_line_rate_gbps_per_lane": MPS4_B2B_MAX_LINE_RATE_GBPS,
                "latency_cycles": latency_cycles,
                "endpoint_bindings": [
                    _endpoint_binding(left, left_connector),
                    _endpoint_binding(right, right_connector),
                ],
            }
        )

    boarddb = {
        "schema": "emuflow.boarddb/v1",
        "platform": {
            "name": name,
            "kind": "hardware",
            "description": (
                "Three Arm MPS4 XCVU13P boards connected pairwise through "
                "the documented J48/J49 ARC6 GTY interfaces"
            ),
            "board": "Arm MPS4 FPGA Prototyping Board",
            "documented_example": "Figure 3-22",
        },
        "fpgas": [
            {
                "id": fpga_id,
                "part": MPS4_PART,
                "manufacturer_part_number": MPS4_MANUFACTURER_PART,
                "utilization_limit": float(utilization_limit),
                "capacity": dict(MPS4_CAPACITY),
            }
            for fpga_id in fpga_ids
        ],
        "links": links,
        "board_services": {
            "clocks": [
                {
                    "id": "b2b_mgt_refclk_pool",
                    "signal": "B2B_CLK[9:0]",
                    "kind": "differential_reference_pool",
                    "frequency_mhz": MPS4_B2B_REFCLK_DEFAULT_MHZ,
                    "frequency_qualification": "documented_default",
                    "count": 10,
                    "destination": "fpga_board_to_board_mgt",
                    "binding_status": "logical_source_without_package_pins",
                    "qualification": "source_backed_board_manual",
                }
            ],
            "resets": [
                {
                    "id": "cb_npor",
                    "signal": "CB_nPOR",
                    "polarity": "active_low",
                    "purpose": "fpga_image_logic_cold_reset",
                    "binding_status": "logical_source_without_package_pins",
                    "qualification": "source_backed_board_manual",
                },
                {
                    "id": "iofpga_nrst",
                    "signal": "IOFPGA_nRST",
                    "polarity": "active_low",
                    "purpose": "board_peripherals_fpga_and_plls",
                    "binding_status": "logical_source_without_package_pins",
                    "qualification": "source_backed_board_manual",
                },
            ],
        },
        "provenance": {
            "board_manual": {
                "title": (
                    "Arm MPS4 FPGA Prototyping Board Technical Reference Manual"
                ),
                "document_id": "102577_0000_02_en",
                "issue": "02",
                "url": ARM_MPS4_TRM_URL,
                "facts": {
                    "device_and_capacity": "pages 9, 23",
                    "board_to_board_clocks": "pages 28-30, Table 3-3",
                    "reset_semantics": "pages 30-32, Figures 3-5 and 3-6",
                    "three_board_wiring": "pages 50-51, Figure 3-22",
                    "connector_pin_map": "pages 93-94, Table A-18",
                },
            },
            "device_capacity": {
                "document": "AMD DS890 Table 15",
                "url": AMD_DS890_URL,
            },
            "transport_profile": {
                "qualification": "configured_model_not_hardware_measured",
                "fabric_clock_mhz": float(fabric_clock_mhz),
                "payload_bits_per_lane_per_cycle": (
                    payload_bits_per_lane_per_cycle
                ),
                "latency_cycles": latency_cycles,
                "configured_raw_gbps_per_physical_lane": configured_rate,
                "encoding_or_protocol_overhead": "not_modeled",
            },
        },
    }
    validated = Platform.from_dict(boarddb)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_path, boarddb)
    if Platform.from_dict(read_json(output_path)) != validated:
        raise ValidationError("written MPS4 BoardDB failed deterministic validation")

    return {
        "schema": ARM_MPS4_BOARDDB_MATERIALIZATION_SCHEMA,
        "status": "pass",
        "output": str(output_path),
        "platform": name,
        "fpgas": len(validated.fpgas),
        "links": len(validated.links),
        "physical_lanes_per_direction_per_link": MPS4_PHYSICAL_LANES,
        "transport_bits_per_cycle_per_direction_per_link": (
            validated.links[0].transport_bits_per_cycle_per_direction
        ),
        "configured_raw_gbps_per_direction_per_link": (
            validated.links[0].raw_bits_per_second_per_direction / 1e9
        ),
        "source": ARM_MPS4_TRM_URL,
    }
