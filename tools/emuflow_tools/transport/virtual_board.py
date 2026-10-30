"""Fixed LVDS P/N wiring for a hypothetical board using real device I/O.

Each scheduled wire is one differential channel with two physical conductors.
Phase 5 owns whole-bank direction and TDM scheduling. This module validates
the fixed board inventory; the native transport/physical owners emit and verify
the data-pin constraints, differential adapter and physical handoff.
"""
from __future__ import annotations

from emuflow.common.paths import DATA_ROOT

from emuflow.common.paths import REPOSITORY_ROOT

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping

from emuflow.common.errors import ValidationError
from emuflow.common.json_io import write_json
from emuflow.platform.platform import Platform
from emuflow.scheduling.tdm_schedule_storage import canonical_tdm_schedule_sha256

MODEL = "xcvu19p-hp-lvds-cable-v1"
WIRING_SCHEMA = "emuflow.virtual-board-wiring/v1"
BINDING_SCHEMA = "emuflow.virtual-board-binding/v1"
IO_CONTRACT_SCHEMA = "emuflow.virtual-io-physical-contract/v1"
IO_CONTRACT_STORAGE_SCHEMA = "emuflow.virtual-io-physical-contract/v2"
INVENTORY_PATH = DATA_ROOT / "io/xcvu19p-fsva3824-2-e.hp-pins.json"
FIXED_WIRING_ROOT = REPOSITORY_ROOT / "platforms/virtual"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_virtual_board(platform: Platform) -> Dict[str, Any]:
    """Consume the shared native fixed-wiring owner from the optional API."""
    from emuflow_tools.transport.board_configuration import build_wiring
    return build_wiring(platform.to_dict(), inventory=INVENTORY_PATH,
                        wiring_root=FIXED_WIRING_ROOT)


def validate_virtual_board(platform: Platform, board: Mapping[str, Any]) -> Dict[str, Any]:
    expected = build_virtual_board(platform)
    if board != expected:
        raise ValidationError("virtual board wiring differs from the declared model or device inventory")
    return {"status": "pass", "qualification": expected["qualification"], **expected["metrics"]}




def validate_virtual_binding(platform: Platform, board: Mapping[str, Any], schedule: Mapping[str, Any],
                             artifacts: Mapping[str, Any], binding: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate current typed authorities without replaying hop records in Python."""
    from emuflow_tools.transport.transport_native import audit_transport_result
    validate_virtual_board(platform, board)
    lane_map = artifacts["lane_map"]
    if (getattr(binding, "store_schema", None) != "transport-store-v2"
            or getattr(lane_map, "store_schema", None) != "transport-store-v2"
            or binding.store_path != lane_map.store_path):
        raise ValidationError("virtual binding validation requires a current typed transport owner")
    audit = audit_transport_result(binding)
    for role, view in (("virtual_board_binding", binding), ("lane_map", lane_map)):
        expected = audit["selectors"][role]
        if (any(view.get(key) != value for key, value in expected["header"].items())
                or any(view.manifest.get(key) != value for key, value in expected.items())):
            raise ValidationError("virtual binding selector header or record seals disagree")
    header = audit["selectors"]["virtual_board_binding"]["header"]
    if (header["platform"] != platform.name
            or header["board_sha256"] != _digest(board)
            or header["schedule_sha256"] != canonical_tdm_schedule_sha256(schedule)):
        raise ValidationError("virtual binding original board or schedule differs")
    return {"status": "pass", "qualification": header["qualification"],
            "physical_backend_status": header["physical_backend_status"], **header["metrics"]}


def write_wiring_csv(path: Path, board: Mapping[str, Any]) -> None:
    common = ["link", "wire", "cable", "bank_group", "bank_in_cable", "wire_in_bank"]
    fields = common + ["connector_data_contact_p", "connector_data_contact_n"]
    for tag in ("a", "b"):
        fields += [f"{key}_{tag}" for key in ("fpga", "bank", "pair_id")]
        fields += [f"{key}_{tag}_{polarity}" for polarity in ("p", "n")
                   for key in ("package_pin", "site", "pin_function")]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for wire in board["wires"]:
            row = {k: wire[k] for k in common}
            row.update({f"connector_data_contact_{pol}": wire["connector_data_contacts"][pol] for pol in ("p", "n")})
            for tag, end in zip(("a", "b"), wire["endpoints"]):
                for key in ("fpga", "bank", "pair_id"):
                    row[f"{key}_{tag}"] = end[key]
                for polarity in ("p", "n"):
                    for key in ("package_pin", "site", "pin_function"):
                        row[f"{key}_{tag}_{polarity}"] = end[polarity][key]
            writer.writerow(row)


def main() -> int:
    from emuflow.platform.default_platform import DEFAULT_PLATFORM_PATH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", type=Path, default=DEFAULT_PLATFORM_PATH)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    platform = Platform.load(args.platform)
    board = build_virtual_board(platform)
    report = validate_virtual_board(platform, board)
    write_json(args.out / "virtual_board_wiring.json", board)
    write_wiring_csv(args.out / "virtual_board_wiring.csv", board)
    write_json(args.out / "validation.json", report)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
