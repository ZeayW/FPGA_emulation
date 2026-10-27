"""Bind Chimew channels to the existing, fixed HP-bank LVDS wire inventory.

The board owns P/N pairing, package pins and bank identities. Chimew may select
among eligible whole channels; it cannot invent pins or reverse a bank during a
frame. Placement geometry remains owned by the source-qualified lookahead.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from emuflow.common.errors import ValidationError
from emuflow.platform.platform import Platform
from emuflow.transport.virtual_board import build_virtual_board, _digest, INVENTORY_PATH
from emuflow.common.io import file_sha256

FIXED_WIRING_MAP_SCHEMA = "emuflow.chimew-electrical-channel-map/v3"
FIXED_WIRING_BINDING_SCHEMA = "emuflow.chimew-phase6-electrical-binding/v3"
FIXED_WIRING_CLASS = "fixed-lvds-pair"


class FixedWiringChannels:
    """A board-sized identity index, independent of the schedule population."""

    def __init__(self, platform: Platform):
        self.board = build_virtual_board(platform)
        self.wires = {(w["link"], w["wire"]): w for w in self.board["wires"]}
        self.source = {
            "board_sha256": _digest(self.board),
            "inventory_sha256": self.board["source"]["inventory_sha256"],
            "wiring_table_sha256": self.board["source"].get("wiring_table_sha256"),
        }
        self.bank_directions: dict[tuple[str, str], tuple[str, str]] = {}

    def validate_source(self, source):
        if source != self.source:
            raise ValidationError("Chimew fixed wiring source differs from the BoardDB")

    def channel(self, link: str, lane: int, fpga_a: str, fpga_b: str):
        if not isinstance(link, str):
            raise ValidationError("Chimew fixed wire link is invalid")
        wire = self.wires.get((link, lane))
        if wire is None:
            raise ValidationError("Chimew channel has no fixed differential wire")
        ends = {end["fpga"]: end for end in wire["endpoints"]}
        if {fpga_a, fpga_b} != set(ends):
            raise ValidationError("Chimew fixed wire FPGA endpoints disagree")
        a, b = ends[fpga_a], ends[fpga_b]
        return {
            "fixed_wire": wire["id"],
            "bank_a": f"{fpga_a}:HP{a['bank']}",
            "bank_b": f"{fpga_b}:HP{b['bank']}",
            "package_pin_a": a["p"]["package_pin"],
            "package_pin_b": b["p"]["package_pin"],
            "differential_pair_a": a,
            "differential_pair_b": b,
        }

    def validate_channel(self, record, fpga_a: str, fpga_b: str):
        lane = record.get("physical_lane")
        if isinstance(lane, bool) or not isinstance(lane, int):
            raise ValidationError("Chimew fixed wire lane is invalid")
        expected = self.channel(record.get("link"), lane, fpga_a, fpga_b)
        if any(key in record for key in ("differential_pair_a", "differential_pair_b")):
            raise ValidationError("Chimew fixed wire references must not duplicate P/N inventory")
        if any(record.get(key) != value for key, value in expected.items()
               if key not in {"differential_pair_a", "differential_pair_b"}):
            raise ValidationError("Chimew differential P/N, bank or fixed wire identity disagrees")
        if (record.get("electrical_class") != FIXED_WIRING_CLASS
                or record.get("iostandard") != self.board["assumptions"]["iostandard"]
                or record.get("supported_iostandards") != [self.board["assumptions"]["iostandard"]]
                or record.get("bank_voltage") != self.board["assumptions"]["vcco_v"]
                or record.get("placement_anchor") is not True):
            raise ValidationError("Chimew fixed LVDS electrical contract is invalid")
        direction = record.get("direction")
        if direction not in {"a_to_b", "b_to_a"}:
            raise ValidationError("Chimew fixed LVDS wire requires one static direction")
        source, sink = (fpga_a, fpga_b) if direction == "a_to_b" else (fpga_b, fpga_a)
        for end in (expected["differential_pair_a"], expected["differential_pair_b"]):
            key = end["fpga"], end["bank"]
            if self.bank_directions.setdefault(key, (source, sink)) != (source, sink):
                raise ValidationError("Chimew cannot reverse a fixed HP bank within a build")
        return {"fixed_wire": expected["fixed_wire"]}


def electrical_channel_pin_keys(record, fixed: FixedWiringChannels | None = None):
    """Count both conductors of a validated differential channel exactly once."""
    keys = [(record["fpga_a"], record["package_pin_a"]),
            (record["fpga_b"], record["package_pin_b"])]
    if record.get("electrical_class") == FIXED_WIRING_CLASS:
        if fixed is None:
            raise ValidationError("Chimew differential pins require their fixed wiring owner")
        wire = fixed.channel(record["link"], record["physical_lane"], record["fpga_a"], record["fpga_b"])
        keys.extend((record[f"fpga_{side}"], wire[f"differential_pair_{side}"]["n"]["package_pin"])
                    for side in ("a", "b"))
    return tuple(keys)


def build_fixed_wiring_chimew_electrical_map(
    platform: Platform, bank_channel_input: Mapping[str, Any], *,
    channel_lanes: Mapping[str, Mapping[str, Any]], fpga_y_bounds: list[dict],
    boarddb_sha256: str,
) -> dict[str, Any]:
    """Produce v3 from existing wires; coordinates stay in the lookahead owner.

    ``channel_lanes`` supplies link/lane/static direction only, never package
    pins. The existing adapter validates channel geometry and grouping against
    its separately sealed bank/channel input.
    """
    from emuflow.transport.chimew.chimew_bank_channel import validate_chimew_bank_channel_input
    from emuflow.transport.chimew.chimew_phase6 import CHIMEW_ELECTRICAL_MAP_PROVIDER, validate_chimew_electrical_map

    problem = validate_chimew_bank_channel_input(bank_channel_input)
    fixed = FixedWiringChannels(platform)
    records = []
    for bank in problem["banks"]:
        domain = problem["domains"][bank["domain"]]
        for channel in bank["channels"]:
            choice = channel_lanes.get(channel["id"])
            if not isinstance(choice, Mapping):
                raise ValidationError("Chimew fixed channel selection is incomplete")
            lane = choice.get("physical_lane")
            if isinstance(lane, bool) or not isinstance(lane, int):
                raise ValidationError("Chimew fixed channel lane is invalid")
            record = {
                "chimew_channel": channel["id"], "link": choice.get("link"),
                "physical_lane": lane, "direction": choice.get("direction"),
                **{key: value for key, value in fixed.channel(
                    choice.get("link"), lane, domain["fpga_a"], domain["fpga_b"]
                ).items() if key not in {"differential_pair_a", "differential_pair_b"}},
                "iostandard": fixed.board["assumptions"]["iostandard"],
                "supported_iostandards": [fixed.board["assumptions"]["iostandard"]],
                "bank_voltage": fixed.board["assumptions"]["vcco_v"],
                "electrical_class": FIXED_WIRING_CLASS,
                "reserved": False, "placement_anchor": True,
            }
            records.append(record)
    if set(channel_lanes) != {r["chimew_channel"] for r in records}:
        raise ValidationError("Chimew fixed channel selection has unknown channels")
    document = {
        "schema": FIXED_WIRING_MAP_SCHEMA, "provider": CHIMEW_ELECTRICAL_MAP_PROVIDER,
        "design": problem["design"], "platform": platform.name,
        "fixed_wiring": fixed.source,
        "provenance": {"producer": "fixed-hp-bank-lvds-wiring-v1", "producer_version": "1",
                       "boarddb_sha256": boarddb_sha256,
                       "package_pin_inventory_sha256": file_sha256(INVENTORY_PATH)},
        "fpga_y_bounds": fpga_y_bounds, "channels": records,
        "metrics": {"channels": len(records), "package_pins": 4 * len(records),
                    "concrete_lanes": len(records)},
    }
    validate_chimew_electrical_map(document, platform, problem)
    return document
