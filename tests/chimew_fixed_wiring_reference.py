"""Small-fixture electrical-map oracle; never a production exporter."""
from collections.abc import Mapping
from typing import Any
from emuflow.platform.platform import Platform
from emuflow_tools.transport.virtual_board import INVENTORY_PATH
from emuflow.common.json_io import file_sha256
from emuflow.common.errors import ValidationError
from emuflow_tools.transport.chimew.chimew_fixed_wiring import FixedWiringChannels, FIXED_WIRING_MAP_SCHEMA, FIXED_WIRING_CLASS

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
    from emuflow_tools.transport.chimew.chimew_bank_channel import validate_chimew_bank_channel_input
    from emuflow_tools.transport.chimew.chimew_phase6 import CHIMEW_ELECTRICAL_MAP_PROVIDER
    from tests.pin_contract_fixture import validate_chimew_electrical_map

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
