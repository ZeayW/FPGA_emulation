"""VCU118 QSFP1 board facts, not a qualified multi-board platform.

AMD UG1224 v1.5: Table 3-18 (GTY bank 231), Table 3-22 (QSFP1),
Table 3-7 / Programmable User Clock 2, and Table 3-28 (CPU_RESET).
These helpers deliberately do not infer cable routing or transport latency.
"""

from typing import Mapping

from .board_support import BOARD_SUPPORT_OVERLAY_SCHEMA, validate_board_support_overlay
from .errors import ValidationError
from .platform import Platform

VCU118_PART = "xcvu9p-flga2104-2L-e"
VCU118_MANUAL = "https://docs.amd.com/v/u/en-US/ug1224-vcu118-eval-bd"
VCU118_CABLE_SOURCE = (
    "https://cdn.blackbox.com/cms/docs/datasheets/"
    "data_sheet-qsfp-40g-dac-networking.pdf"
)
# P/N pairs in FPGA lane order, independently matched to the reference XDC.
_TX = (("V7", "V6"), ("T7", "T6"), ("P7", "P6"), ("M7", "M6"))
_RX = (("Y2", "Y1"), ("W4", "W3"), ("V2", "V1"), ("U4", "U3"))


def build_vcu118_pair_boarddb(*, latency_cycles: int) -> dict:
    """Construct the fixed two-board reference candidate, not a qualified BSP.

    Latency is explicitly supplied as a model assumption, never inferred from
    cable length or line rate. The full-flow gate still needs implemented
    transport, module controls, physical timing and a justified delay bound.
    """
    if type(latency_cycles) is not int or latency_cycles <= 0:
        raise ValidationError("VCU118 candidate requires positive explicit latency_cycles")
    ids = ("vcu118_1", "vcu118_2")
    # DS890 v4.10 Table 15. No arbitrary external DUT I/O budget is exposed:
    # package I/O count does not say which PCB pins are free for a user's DUT.
    capacity = {"lut": 1_182_240, "ff": 2_364_480, "bram": 2_160,
                "bram18k": 4_320, "uram288": 960, "dsp": 6_840, "dsp48": 6_840}
    document = {
        "schema": "emuflow.boarddb/v1",
        "platform": {"name": "vcu118_pair_qsfp1", "kind": "hardware",
                     "description": "Two VCU118 boards, one QSFP-H40G-CU1M-BB cable; unqualified reference candidate"},
        "fpgas": [{"id": fpga, "part": VCU118_PART,
                   "utilization_limit": 0.75, "capacity": dict(capacity)} for fpga in ids],
        "links": [{"id": "qsfp1", "endpoints": list(ids),
                   "direction": "full_duplex", "capacity_sharing": "per_direction",
                   "mode": "serial", "data_lanes_per_direction": 4,
                   "payload_bits_per_lane_per_cycle": 64, "fabric_clock_mhz": 50.0,
                   "latency_cycles": latency_cycles,
                   "endpoint_bindings": [vcu118_qsfp1_endpoint(fpga) for fpga in ids]}],
        "board_services": vcu118_board_services(),
        "provenance": {
            "qualification": "source_backed_candidate_not_implemented",
            "board_manual": VCU118_MANUAL,
            "device_capacity": "https://docs.amd.com/v/u/en-US/ds890-ultrascale-overview",
            "device_capacity_locator": "DS890 v4.10 Table 15, VU9P",
            "cable": {"model": "QSFP-H40G-CU1M-BB", "length_m": 1,
                      "source": VCU118_CABLE_SOURCE, "locator": "pages 3-4",
                      "lane_mapping": "TXn to RXn in both directions, n=1..4, polarity preserved"},
            "transport": {"qualification": "configured_unmeasured_model",
                          "latency_cycles": latency_cycles,
                          "latency_basis": "caller_supplied_not_a_hardware_bound",
                          "line_rate_gbps": 10.3125,
                          "pcs_clock_mhz": 156.25,
                          "record_pcs_cycles": 3},
        },
    }
    Platform.from_dict(document)
    return document


def vcu118_qsfp1_endpoint(fpga: str) -> dict:
    """Return all four physical lanes; no GT site or cable mapping is guessed."""
    if not isinstance(fpga, str) or not fpga.strip():
        raise ValidationError("VCU118 endpoint requires an FPGA identity")
    return {
        "fpga": fpga, "connector": "QSFP1_U145", "mgt": "GTY_BANK_231",
        "lanes": [
            {"lane": lane,
             "tx_package_pins": dict(zip(("p", "n"), tx)),
             "rx_package_pins": dict(zip(("p", "n"), rx))}
            for lane, (tx, rx) in enumerate(zip(_TX, _RX))
        ],
    }


def vcu118_board_services() -> dict:
    """Logical services used by the explicit package-pin overlay below."""
    return {
        "clocks": [{
            "id": "qsfp1_refclk", "signal": "QSFP_SI570_CLOCK",
            "kind": "differential_reference_pool", "frequency_mhz": 156.25,
            "frequency_qualification": "documented_default", "count": 1,
            "destination": "GTY_BANK_231",
            "binding_status": "logical_source_without_package_pins",
            "qualification": "source_backed_board_manual",
        }],
        "resets": [{
            "id": "cpu_reset", "signal": "CPU_RESET", "polarity": "active_high",
            "purpose": "user_pushbutton_reset",
            "binding_status": "logical_source_without_package_pins",
            "qualification": "source_backed_board_manual",
        }],
    }


def build_vcu118_qsfp1_overlay(
    platform: Platform, sites: Mapping[tuple[str, int], str]
) -> dict:
    """Bind verified board pins to externally resolved GT sites.

    The caller must supply the device-derived sites. The later Phase 6C gate
    must still compare them to its independently validated Vivado site map.
    This helper does not qualify those site strings or cable connectivity.
    """
    if any(fpga.part != VCU118_PART for fpga in platform.fpgas):
        raise ValidationError("VCU118 overlay requires the exact VCU118 part")
    clocks, resets, bindings = [], [], []
    expected = set()
    for fpga in platform.fpgas:
        clocks.append({
            "id": f"{fpga.id}_qsfp1_refclk", "fpga": fpga.id,
            "board_service": "qsfp1_refclk", "selected_signal": "QSFP_SI570_CLOCK",
            "package_pins": {"p": "W9", "n": "W8"},
            "frequency_mhz": 156.25, "frequency_basis": "documented",
        })
        resets.append({
            "id": f"{fpga.id}_cpu_reset", "fpga": fpga.id,
            "board_service": "cpu_reset", "package_pin": "L19",
            "iostandard": "LVCMOS12",
        })
    for link in platform.links:
        for endpoint in link.endpoint_bindings:
            reference = vcu118_qsfp1_endpoint(endpoint.fpga)
            if (endpoint.connector != reference["connector"]
                    or endpoint.mgt != reference["mgt"]
                    or len(endpoint.lanes) != 4):
                raise ValidationError("VCU118 overlay supports exactly QSFP1's four lanes")
            for lane, pins in zip(endpoint.lanes, reference["lanes"]):
                if lane.lane != pins["lane"]:
                    raise ValidationError("VCU118 endpoint lane order disagrees")
                if (lane.tx_package_pin_p, lane.tx_package_pin_n,
                    lane.rx_package_pin_p, lane.rx_package_pin_n) != (
                    pins["tx_package_pins"]["p"], pins["tx_package_pins"]["n"],
                    pins["rx_package_pins"]["p"], pins["rx_package_pins"]["n"]):
                    raise ValidationError("VCU118 endpoint disagrees with documented pins")
                key = (endpoint.fpga, pins["lane"])
                if key in expected or key not in sites:
                    raise ValidationError("VCU118 GT site coverage is missing or duplicated")
                expected.add(key)
                bindings.append({
                    "fpga": endpoint.fpga, "link": link.id,
                    "connector": endpoint.connector, "mgt_group": endpoint.mgt,
                    "physical_lane": pins["lane"], "site": sites[key],
                    "reference_clock_binding": f"{endpoint.fpga}_qsfp1_refclk",
                    "reset_binding": f"{endpoint.fpga}_cpu_reset",
                })
    if not expected or set(sites) != expected:
        raise ValidationError("VCU118 GT site coverage must match used QSFP1 endpoints")
    overlay = {
        "schema": BOARD_SUPPORT_OVERLAY_SCHEMA, "platform": platform.name,
        "qualification": "source_backed_hardware_definition",
        "provenance": {"sources": [{
            "title": "AMD VCU118 UG1224 v1.5", "uri": VCU118_MANUAL,
            "locator": "Tables 3-7, 3-18, 3-22, 3-28; Programmable User Clock 2",
        }]},
        "reference_clocks": clocks, "resets": resets, "transceiver_sites": bindings,
    }
    return validate_board_support_overlay(overlay, platform)["normalized"]
