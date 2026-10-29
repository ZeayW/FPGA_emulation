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
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Mapping

from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json, write_json
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


def _inventory() -> Dict[str, Any]:
    return read_json(INVENTORY_PATH)


def _name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", value)


def _bank_pairs(bank: Mapping[str, Any]) -> list:
    """Use verified vendor pair IDs and polarities, never infer from pin ordering."""
    pairs = defaultdict(dict)
    for pin in bank["pins"]:
        pair = pin.get("differential_pair_id")
        polarity = pin.get("polarity")
        if pair is None:
            continue
        if not pin["eligible_data"] or polarity not in ("p", "n"):
            raise ValidationError("invalid differential inventory polarity or eligibility")
        if polarity in pairs[pair]:
            raise ValidationError("duplicate differential inventory polarity")
        # Independent check against the package pin function from AMD's pinout.
        match = re.match(r"IO_L(\d+)([PN])_.*_(\d+)$", pin["pin_function"] or "")
        if (match is None or f"L{match[1]}" != pair or match[2].lower() != polarity
                or match[3] != str(bank["id"])):
            raise ValidationError("differential pair does not match vendor pin function")
        pairs[pair][polarity] = {k: pin[k] for k in ("package_pin", "site", "pin_function")}
    if any(set(pair) != {"p", "n"} for pair in pairs.values()):
        raise ValidationError("incomplete differential pair in device inventory")
    # Preserve the four global-clock pairs at the default 20-pair data width.
    order = sorted(pairs, key=lambda key: ("_GC_" in pairs[key]["p"]["pin_function"], int(key[1:])))
    return [{"pair_id": key, **pairs[key]} for key in order]


def _build_virtual_board_model(platform: Platform) -> Dict[str, Any]:
    """Assign disjoint real HP banks and connect each P to P and N to N."""
    if platform.virtual_board_model != MODEL or platform.kind != "virtual":
        raise ValidationError(f"virtual board requires the declared {MODEL} model")
    inventory = _inventory()
    if any(fpga.part != inventory["part"] for fpga in platform.fpgas):
        raise ValidationError("virtual board inventory does not match the FPGA part")
    banks = sorted(inventory["banks"], key=lambda b: int(b["id"]))
    if len({str(b["id"]) for b in banks}) != len(banks):
        raise ValidationError("duplicate inventory HP bank")
    eligible = {}
    seen = set()
    for bank in banks:
        if bank["type"] != "HP":
            raise ValidationError("virtual data wiring requires HP I/O banks")
        for pin in bank["pins"]:
            if pin["package_pin"] in seen or not pin["site"]:
                raise ValidationError("invalid inventory package pin or I/O site")
            seen.add(pin["package_pin"])
        eligible[str(bank["id"])] = _bank_pairs(bank)
    # Bank 65 has configuration-multiplexed I/O. Reserve it in this board model.
    data_banks = [bank for bank in banks if str(bank["id"]) != "65"]
    next_bank = {fpga.id: 0 for fpga in platform.fpgas}
    allocated = defaultdict(list)
    wires = []
    for link in sorted(platform.links, key=lambda item: item.id):
        if (link.capacity_sharing != "shared_bidirectional" or link.direction != "full_duplex"
                or link.mode != "abstract" or link.cable_count != 6 or link.hp_banks_per_cable != 3
                or link.bank_count != 18 or link.wires_per_bank is None):
            raise ValidationError("virtual board model requires 6 cables, 3 paired HP banks per cable and shared differential channels")
        ends = sorted(link.endpoints)
        pair_banks = {}
        for fpga in ends:
            begin = next_bank[fpga]
            if begin + link.bank_count > len(data_banks):
                raise ValidationError(f"{fpga} has insufficient disjoint HP banks for virtual cables")
            pair_banks[fpga] = data_banks[begin:begin + link.bank_count]
            next_bank[fpga] += link.bank_count
        for group in range(link.bank_count):
            cable_index, bank_in_cable = divmod(group, link.hp_banks_per_cable)
            cable = f"{link.id}:cable{cable_index}"
            for fpga in ends:
                bank = pair_banks[fpga][group]
                if len(eligible[str(bank["id"])]) < link.wires_per_bank:
                    raise ValidationError(f"bank {bank['id']} has insufficient eligible differential pairs")
                allocated[fpga].append({"link": link.id, "bank_group": group, "bank": str(bank["id"]),
                                        "cable": cable, "bank_in_cable": bank_in_cable,
                                        "differential_pairs_used": link.wires_per_bank,
                                        "data_pins_used": 2 * link.wires_per_bank})
            for index in range(link.wires_per_bank):
                wire = group * link.wires_per_bank + index
                endpoints = []
                for fpga in ends:
                    bank = str(pair_banks[fpga][group]["id"])
                    pair = eligible[bank][index]
                    endpoints.append({"fpga": fpga, "bank": bank, **pair})
                contact = 2 * (bank_in_cable * link.wires_per_bank + index) + 1
                wires.append({"id": f"{link.id}:wire{wire}", "link": link.id, "wire": wire,
                              "bank_group": group, "cable": cable, "cable_index": cable_index,
                              "bank_in_cable": bank_in_cable, "wire_in_bank": index,
                              "connector_data_contacts": {"p": contact, "n": contact + 1},
                              "endpoints": endpoints})
    return {
        "schema": WIRING_SCHEMA, "model": MODEL, "platform": platform.name,
        "qualification": "virtual_board_real_device_pins",
        "source": {"inventory_file": INVENTORY_PATH.name, "inventory_sha256": _digest(inventory),
                   "device_provenance": inventory["provenance"], "pin_screening": inventory["screening"]},
        "assumptions": {"signaling": "differential", "iostandard": "LVDS", "vcco_v": 1.8,
                        "wire_unit": "one differential data channel: two conductors, one bit per fabric cycle",
                        "connectivity": "fixed P-to-P and N-to-N; direction chosen per bank by Phase 5",
                        "connector_contacts": "data-contact ordinals, not a connector product pinout",
                        "board_services": "clock, reset, power and ground connections are outside this data-wire table",
                        "electrical_qualification": "assumed 1.8V bank supply and internal 100-ohm receiver termination; no board SI or vendor DRC claim",
                        "receiver_termination": "internal_100_ohm_at_receiver",
                        "direction_configuration": "static per generated schedule/build; not switched during a TDM frame",
                        "pin_selection": "reserve bank 65 for configuration; prefer non-GC differential pairs within data banks",
                        "link_timing": "uses BoardDB TDM model; no extra invented cable delay"},
        "fpgas": [{"id": fpga.id, "part": fpga.part, "bank_assignments": allocated[fpga.id],
                   "unallocated_hp_banks": [str(b["id"]) for b in banks
                                            if str(b["id"]) not in {a["bank"] for a in allocated[fpga.id]}]}
                  for fpga in sorted(platform.fpgas, key=lambda f: f.id)],
        "wires": wires,
        "metrics": {"links": len(platform.links), "cables": sum(l.cable_count for l in platform.links),
                    "differential_pairs": len(wires), "physical_conductors": 2 * len(wires),
                    "endpoint_data_pins": 4 * len(wires), "hp_banks_per_fpga": next_bank},
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_fixed_wiring(path: Path) -> list[Dict[str, Any]]:
    expected_fields = [
        "link", "wire", "cable", "bank_group", "bank_in_cable",
        "wire_in_bank", "connector_data_contact_p",
        "connector_data_contact_n",
    ]
    for tag in ("a", "b"):
        expected_fields += [f"{key}_{tag}" for key in ("fpga", "bank", "pair_id")]
        expected_fields += [
            f"{key}_{tag}_{polarity}"
            for polarity in ("p", "n")
            for key in ("package_pin", "site", "pin_function")
        ]
    if not path.is_file():
        raise ValidationError(f"fixed virtual-board wiring table is missing: {path}")
    wires: list[Dict[str, Any]] = []
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != expected_fields:
            raise ValidationError("fixed virtual-board wiring table header is invalid")
        for line_number, row in enumerate(reader, start=2):
            if set(row) != set(expected_fields) or any(value is None for value in row.values()):
                raise ValidationError(
                    f"fixed virtual-board wiring row {line_number} is malformed"
                )
            try:
                wire_index = int(row["wire"])
                bank_group = int(row["bank_group"])
                bank_in_cable = int(row["bank_in_cable"])
                wire_in_bank = int(row["wire_in_bank"])
                contact_p = int(row["connector_data_contact_p"])
                contact_n = int(row["connector_data_contact_n"])
            except ValueError as error:
                raise ValidationError(
                    f"fixed virtual-board wiring row {line_number} has a non-integer index"
                ) from error
            endpoints = []
            for tag in ("a", "b"):
                endpoint: Dict[str, Any] = {
                    key: row[f"{key}_{tag}"] for key in ("fpga", "bank", "pair_id")
                }
                for polarity in ("p", "n"):
                    endpoint[polarity] = {
                        key: row[f"{key}_{tag}_{polarity}"]
                        for key in ("package_pin", "site", "pin_function")
                    }
                endpoints.append(endpoint)
            cable_match = re.fullmatch(r"(.+):cable(\d+)", row["cable"])
            if cable_match is None or cable_match.group(1) != row["link"]:
                raise ValidationError(
                    f"fixed virtual-board wiring row {line_number} has an invalid cable"
                )
            wires.append({
                "id": f"{row['link']}:wire{wire_index}",
                "link": row["link"],
                "wire": wire_index,
                "bank_group": bank_group,
                "cable": row["cable"],
                "cable_index": int(cable_match.group(2)),
                "bank_in_cable": bank_in_cable,
                "wire_in_bank": wire_in_bank,
                "connector_data_contacts": {"p": contact_p, "n": contact_n},
                "endpoints": endpoints,
            })
    return wires


def _fixed_wiring_path(reference: str) -> Path:
    candidates = (
        FIXED_WIRING_ROOT / reference,
        Path(sys.prefix) / "share/emuflow/platforms" / reference,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ValidationError(
        "fixed virtual-board wiring table is missing: "
        + ", ".join(str(path) for path in candidates)
    )


def build_virtual_board(platform: Platform) -> Dict[str, Any]:
    """Load a declared fixed wiring table and independently verify its model."""
    board = _build_virtual_board_model(platform)
    reference = platform.virtual_board_wiring
    if reference is None:
        # Custom research platforms retain the deterministic model generator;
        # the distributed defaults always declare a fixed source table.
        return board
    path = _fixed_wiring_path(reference)
    wires = _read_fixed_wiring(path)
    if wires != board["wires"]:
        raise ValidationError(
            "fixed virtual-board wiring table differs from the declared model "
            "or device pin inventory"
        )
    board["wires"] = wires
    board["source"] = {
        **board["source"],
        "wiring_table": reference,
        "wiring_table_sha256": _file_sha256(path),
        "wiring_authority": "checked-in-fixed-table",
    }
    return board


def validate_virtual_board(platform: Platform, board: Mapping[str, Any]) -> Dict[str, Any]:
    expected = build_virtual_board(platform)
    if board != expected:
        raise ValidationError("virtual board wiring differs from the declared model or device inventory")
    return {"status": "pass", "qualification": expected["qualification"], **expected["metrics"]}




def validate_virtual_binding(platform: Platform, board: Mapping[str, Any], schedule: Mapping[str, Any],
                             artifacts: Mapping[str, Any], binding: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate current typed authorities without replaying hop records in Python."""
    from emuflow.transport.transport_native import audit_transport_result
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
