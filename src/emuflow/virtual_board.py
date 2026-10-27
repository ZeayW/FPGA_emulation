"""Fixed LVDS P/N wiring for a hypothetical board using real device I/O.

Each scheduled wire is one differential channel with two physical conductors.
Phase 5 owns whole-bank direction and TDM scheduling. This module emits data
pin constraints and a separate I/O adapter, not a complete board implementation.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import uuid
from collections import defaultdict
from itertools import zip_longest
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Protocol

from .errors import ValidationError
from .io import read_json, write_json
from .netlist import transport_endpoint_port
from .platform import Platform
from .tdm_schedule_storage import canonical_tdm_schedule_sha256

MODEL = "xcvu19p-hp-lvds-cable-v1"
WIRING_SCHEMA = "emuflow.virtual-board-wiring/v1"
BINDING_SCHEMA = "emuflow.virtual-board-binding/v1"
IO_CONTRACT_SCHEMA = "emuflow.virtual-io-physical-contract/v1"
IO_CONTRACT_STORAGE_SCHEMA = "emuflow.virtual-io-physical-contract/v2"
INVENTORY_PATH = Path(__file__).parent / "data/io/xcvu19p-fsva3824-2-e.hp-pins.json"
FIXED_WIRING_ROOT = Path(__file__).resolve().parents[2] / "platforms/virtual"


class _TextWriter(Protocol):
    def write(self, value: str) -> int:
        ...


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _artifact_digest(value: Any) -> str:
    """Use a sealed relation digest without expanding an indexed artifact."""
    canonical_sha256 = getattr(value, "canonical_sha256", None)
    if isinstance(canonical_sha256, str):
        return canonical_sha256
    logical_sha256 = getattr(value, "logical_sha256", None)
    return logical_sha256 if isinstance(logical_sha256, str) else _digest(value)


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
    relation_validator = getattr(binding, "validate_transport_relations", None)
    if relation_validator is not None:
        validate_virtual_board(platform, board)
        relation_validator()
        lane_records = artifacts["lane_map"]["entries"]
        schedule_records = schedule["entries"]
        schedule_identity_records = getattr(
            schedule_records, "iter_by_identity", None
        )
        ordered_schedule = (
            schedule_identity_records()
            if schedule_identity_records is not None
            else iter(sorted(schedule_records, key=lambda item: item["id"]))
        )
        wire_by_key = {
            (wire["link"], wire["wire"]): wire for wire in board["wires"]
        }
        links = {link.id: link for link in platform.links}
        occupied = set()
        direction_by_wire = {}
        pin_ports = {}
        port_pins = {}
        adapter_ports: Dict[str, Dict[str, Mapping[str, Any]]] = defaultdict(dict)
        count = 0
        missing = object()
        for entry, mapped, observed in zip_longest(
            ordered_schedule,
            lane_records,
            binding["entries"],
            fillvalue=missing,
        ):
            if missing in (entry, mapped, observed):
                raise ValidationError(
                    "virtual binding schedule/lane/entry coverage differs"
                )
            if mapped.get("schedule_entry") != entry.get("id"):
                raise ValidationError(
                    "virtual binding lane-map identity differs from schedule"
                )
            if any(
                mapped.get(key) != entry.get(key)
                for key in ("link", "from", "to", "slot")
            ):
                raise ValidationError(
                    "virtual binding lane-map fields differ from schedule"
                )
            wire = wire_by_key.get((entry["link"], mapped["lane"]))
            if wire is None:
                raise ValidationError(
                    "scheduled wire has no virtual board connection"
                )
            key = (wire["id"], entry["slot"])
            if key in occupied:
                raise ValidationError(
                    "multiple communications occupy one physical wire in "
                    "the same slot"
                )
            occupied.add(key)
            direction = (entry["from"], entry["to"])
            if direction_by_wire.setdefault(wire["id"], direction) != direction:
                raise ValidationError(
                    "one physical wire cannot change direction within the schedule"
                )
            pins = {pin["fpga"]: pin for pin in wire["endpoints"]}
            bound = {}
            for role, kind, fpga, peer in (
                ("source", "tx", entry["from"], entry["to"]),
                ("sink", "rx", entry["to"], entry["from"]),
            ):
                logical_port = (
                    f"{transport_endpoint_port({'kind': kind, 'link': entry['link'], 'peer': peer})}"
                    f"[{mapped['lane']}]"
                )
                pair = pins[fpga]
                bound_end = {
                    key: pair[key] for key in ("fpga", "bank", "pair_id")
                }
                bound_end.update(
                    logical_port=logical_port,
                    logical_port_width=links[
                        entry["link"]
                    ].transport_bits_per_cycle_per_direction,
                )
                for polarity in ("p", "n"):
                    pin = pair[polarity]
                    port = (
                        f"emuflow_io_{kind}_{_name(entry['link'])}_"
                        f"wire{wire['wire']}_{polarity}"
                    )
                    identity = (fpga, pin["package_pin"])
                    if pin_ports.setdefault(identity, port) != port:
                        raise ValidationError(
                            "package pin is assigned to more than one RTL port"
                        )
                    if port_pins.setdefault((fpga, port), identity) != identity:
                        raise ValidationError(
                            "physical RTL port name collides after identifier "
                            "normalization"
                        )
                    bound_end[polarity] = {**pin, "port": port}
                bound[role] = bound_end
                adapter_record = _adapter_port_record(
                    role, bound_end, board["assumptions"]["iostandard"]
                )
                adapter_key = bound_end["p"]["port"]
                previous_adapter = adapter_ports[fpga].setdefault(
                    adapter_key, adapter_record
                )
                if previous_adapter != adapter_record:
                    raise ValidationError(
                        "one virtual adapter port has inconsistent bindings"
                    )
            expected = {
                "schedule_entry": entry["id"],
                "net": entry.get("net"),
                "slot": entry["slot"],
                "arrival_slot": entry.get("arrival_slot"),
                "tdm_ratio": entry.get("tdm_ratio"),
                "wire_id": wire["id"],
                "wire": wire["wire"],
                "link": wire["link"],
                "bank_group": wire["bank_group"],
                "cable": wire["cable"],
                "iostandard": board["assumptions"]["iostandard"],
                **bound,
            }
            if observed != expected:
                raise ValidationError(
                    "virtual board binding entry differs from schedule, lane, "
                    "or fixed pin pair"
                )
            count += 1
        expected_header = {
            "schema": BINDING_SCHEMA,
            "platform": platform.name,
            "design": schedule.get("design"),
            "qualification": "virtual_board_real_device_pins",
            "status": "pass",
            "integration_status": (
                "differential_data_constraints_and_adapter_generated"
            ),
            "physical_backend_status": "phase7_consumption_required",
            "board_services_status": "clock_reset_and_control_pins_not_bound",
            "io_adapter_status": "phase7_top_level_integration_required",
            "board_sha256": _digest(board),
            "schedule_sha256": canonical_tdm_schedule_sha256(schedule),
            "lane_map_sha256": _artifact_digest(artifacts["lane_map"]),
            "metrics": {
                "scheduled_hops": count,
                "used_differential_pairs": len(direction_by_wire),
                "bound_data_ports": len(pin_ports),
                "total_differential_pairs": len(board["wires"]),
            },
        }
        if any(binding.get(key) != value for key, value in expected_header.items()):
            raise ValidationError(
                "virtual board binding header does not match schedule, ports, "
                "and fixed pin pairs"
            )
        indexed_adapter_fpgas = getattr(
            binding, "iter_adapter_port_fpgas", None
        )
        if indexed_adapter_fpgas is not None and set(
            indexed_adapter_fpgas()
        ) != set(adapter_ports):
            raise ValidationError(
                "virtual adapter port index has unexpected FPGA ownership"
            )
        for fpga in sorted(adapter_ports):
            observed_ports = list(_iter_binding_adapter_ports(binding, fpga))
            expected_ports = [
                adapter_ports[fpga][key]
                for key in sorted(adapter_ports[fpga])
            ]
            if observed_ports != expected_ports:
                raise ValidationError(
                    "virtual adapter port index differs from hop bindings"
                )
        return {
            "status": "pass",
            "qualification": expected_header["qualification"],
            "physical_backend_status": expected_header[
                "physical_backend_status"
            ],
            **expected_header["metrics"],
        }
    raise ValidationError("virtual binding validation requires a current typed transport owner")


def _emit_virtual_binding_xdc(
    stream: _TextWriter, binding: Mapping[str, Any], fpga: str
) -> None:
    for line in (
        "# Hypothetical LVDS board: vendor P/N pairs, assumed P-to-P and N-to-N cables.",
        "# Use with virtual_differential_io.sv exposed at the physical top level.",
        "# Data ports only; 1.8 V VCCO and internal 100-ohm receiver termination assumed.",
        "# Clock/reset/control constraints and physical backend integration remain required.",
    ):
        stream.write(line + "\n")
    records = {}
    for end in _iter_binding_adapter_ports(binding, fpga):
        for polarity in ("p", "n"):
            pin = end[polarity]
            records[pin["port"]] = (pin["package_pin"], end["iostandard"])
    for port, (pin, standard) in sorted(records.items()):
        if any(c in port + pin + standard for c in "{}\n\r;"):
            raise ValidationError("unsafe name in virtual data-pin constraint")
        stream.write(f"set_property PACKAGE_PIN {pin} [get_ports {{{port}}}]\n")
        stream.write(
            f"set_property IOSTANDARD {standard} [get_ports {{{port}}}]\n"
        )




def _emit_virtual_binding_systemverilog(
    stream: _TextWriter, binding: Mapping[str, Any], fpga: str
) -> None:
    """Emit a standalone data adapter; the backend must integrate it at top level."""
    records = {
        end["p"]["port"]: (end["role"], end)
        for end in _iter_binding_adapter_ports(binding, fpga)
    }
    buses = {}
    used_rx_bits = defaultdict(set)
    for role, end in records.values():
        logical = end["logical_port"]
        match = re.fullmatch(r"([a-zA-Z_][a-zA-Z0-9_]*)\[(\d+)\]", logical)
        if match is None:
            raise ValidationError("invalid differential adapter logical port")
        bus, bit = match[1], int(match[2])
        direction = "input" if role == "source" else "output"
        config = (direction, end["logical_port_width"])
        if buses.setdefault(bus, config) != config:
            raise ValidationError("inconsistent differential adapter bus")
        p, n = end["p"]["port"], end["n"]["port"]
        if any(re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name) is None for name in (p, n)):
            raise ValidationError("invalid differential adapter pin port")
        if role == "sink":
            used_rx_bits[bus].add(bit)

    stream.write(
        "// Standalone LVDS data adapter. Connect logical buses to "
        "emuflow_transport_<fpga>.\n"
    )
    stream.write(
        "// Expose P/N ports at the physical top level before applying "
        "virtual_data_pins.xdc.\n"
    )
    stream.write(
        "// Whole-bank directions are static for this build; internal "
        "receiver termination enabled.\n"
    )
    stream.write(f"module emuflow_virtual_io_{_name(fpga)} (\n")
    first_port = True

    def port(value: str) -> None:
        nonlocal first_port
        if not first_port:
            stream.write(",\n")
        stream.write(value)
        first_port = False

    for bus, (direction, width) in sorted(buses.items()):
        port(f"  {direction} wire [{width - 1}:0] {bus}")
    for role, end in records.values():
        physical_direction = "output" if role == "source" else "input"
        port(f"  {physical_direction} wire {end['p']['port']}")
        port(f"  {physical_direction} wire {end['n']['port']}")
    stream.write("\n);\n")

    for index, (_, (role, end)) in enumerate(sorted(records.items())):
        logical = end["logical_port"]
        p, n = end["p"]["port"], end["n"]["port"]
        if role == "source":
            stream.write(
                f'  OBUFDS #(.IOSTANDARD("LVDS")) tx_{index} '
                f"(.I({logical}), .O({p}), .OB({n}));\n"
            )
        else:
            stream.write(
                f'  IBUFDS #(.IOSTANDARD("LVDS"), .DIFF_TERM("TRUE")) '
                f"rx_{index} (.I({p}), .IB({n}), .O({logical}));\n"
            )
    for bus, bits in sorted(used_rx_bits.items()):
        for bit in range(buses[bus][1]):
            if bit not in bits:
                stream.write(f"  assign {bus}[{bit}] = 1'b0;\n")
    stream.write("endmodule\n")




def _write_virtual_binding_artifact(
    path: Path,
    binding: Mapping[str, Any],
    fpga: str,
    *,
    systemverilog: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            if systemverilog:
                _emit_virtual_binding_systemverilog(stream, binding, fpga)
            else:
                _emit_virtual_binding_xdc(stream, binding, fpga)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_virtual_binding_xdc(
    path: Path, binding: Mapping[str, Any], fpga: str
) -> None:
    _write_virtual_binding_artifact(
        path, binding, fpga, systemverilog=False
    )


def write_virtual_binding_systemverilog(
    path: Path, binding: Mapping[str, Any], fpga: str
) -> None:
    _write_virtual_binding_artifact(
        path, binding, fpga, systemverilog=True
    )


def _virtual_binding_artifact_sha256(
    binding: Mapping[str, Any], fpga: str, *, systemverilog: bool
) -> str:
    digest = hashlib.sha256()

    class DigestStream:
        def write(self, value: str) -> int:
            digest.update(value.encode("utf-8"))
            return len(value)

    stream = DigestStream()
    if systemverilog:
        _emit_virtual_binding_systemverilog(stream, binding, fpga)
    else:
        _emit_virtual_binding_xdc(stream, binding, fpga)
    return digest.hexdigest()


def virtual_binding_xdc_matches(
    path: Path, binding: Mapping[str, Any], fpga: str
) -> bool:
    return path.is_file() and _file_sha256(path) == _virtual_binding_artifact_sha256(
        binding, fpga, systemverilog=False
    )


def virtual_binding_systemverilog_matches(
    path: Path, binding: Mapping[str, Any], fpga: str
) -> bool:
    return path.is_file() and _file_sha256(path) == _virtual_binding_artifact_sha256(
        binding, fpga, systemverilog=True
    )


def _iter_binding_adapter_ports(
    binding: Mapping[str, Any], fpga: str
) -> Iterator[Mapping[str, Any]]:
    """Iterate unique physical adapter endpoints in stable P-port order."""

    indexed = getattr(binding, "iter_adapter_ports", None)
    if indexed is not None:
        yield from indexed(fpga)
        return

    records: Dict[str, Mapping[str, Any]] = {}
    for entry in binding["entries"]:
        for role in ("source", "sink"):
            end = entry[role]
            if end["fpga"] == fpga:
                records[end["p"]["port"]] = _adapter_port_record(
                    role, end, entry["iostandard"]
                )
    yield from (records[key] for key in sorted(records))


def _adapter_port_record(
    role: str, end: Mapping[str, Any], iostandard: str
) -> Mapping[str, Any]:
    return {
        "role": role,
        "logical_port": end["logical_port"],
        "logical_port_width": end["logical_port_width"],
        "iostandard": iostandard,
        "p": {
            "port": end["p"]["port"],
            "package_pin": end["p"]["package_pin"],
        },
        "n": {
            "port": end["n"]["port"],
            "package_pin": end["n"]["package_pin"],
        },
    }


def build_virtual_io_physical_contract(
    binding: Mapping[str, Any],
    fpga: str,
    *,
    xdc_path: Path,
    adapter_path: Path,
) -> Dict[str, Any]:
    """Seal the exact Phase-6 XDC/adapter inputs consumed by Phase 7."""
    if binding.get("schema") != BINDING_SCHEMA or binding.get("status") != "pass":
        raise ValidationError("virtual I/O physical contract has an invalid binding")
    if not virtual_binding_xdc_matches(xdc_path, binding, fpga):
        raise ValidationError(f"{fpga} virtual data-pin XDC differs from its binding")
    if not virtual_binding_systemverilog_matches(adapter_path, binding, fpga):
        raise ValidationError(f"{fpga} differential I/O adapter differs from its binding")

    indexed_entries = getattr(binding, "iter_physical_contract_entries", None)
    if indexed_entries is not None:
        entries = [dict(record) for record in indexed_entries(fpga)]
        entries.sort(key=lambda item: item.get("id", ""))
        adapter_by_port = {
            str(record["p"]["port"]): record
            for record in _iter_binding_adapter_ports(binding, fpga)
        }
        seen = set()
        seen_ports = set()
        scheduled_hops = 0
        for record in entries:
            identity = record.get("id")
            direction = record.get("direction")
            count = record.get("schedule_entry_count")
            digest = record.get("schedule_entries_sha256")
            logical_port = record.get("logical_port")
            logical_bit = record.get("logical_bit")
            logical_width = record.get("logical_port_width")
            p = record.get("p")
            n = record.get("n")
            p_port = p.get("port") if isinstance(p, Mapping) else None
            n_port = n.get("port") if isinstance(n, Mapping) else None
            p_package_pin = (
                p.get("package_pin") if isinstance(p, Mapping) else None
            )
            n_package_pin = (
                n.get("package_pin") if isinstance(n, Mapping) else None
            )
            p_site = p.get("site") if isinstance(p, Mapping) else None
            n_site = n.get("site") if isinstance(n, Mapping) else None
            adapter = adapter_by_port.get(p_port) if isinstance(p_port, str) else None
            expected_role = "source" if direction == "tx" else "sink"
            if (
                not isinstance(identity, str)
                or not identity
                or identity in seen
                or direction not in {"tx", "rx"}
                or identity != f"{direction}:{p_port}"
                or not isinstance(logical_port, str)
                or re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", logical_port) is None
                or isinstance(logical_bit, bool)
                or not isinstance(logical_bit, int)
                or isinstance(logical_width, bool)
                or not isinstance(logical_width, int)
                or logical_bit < 0
                or logical_bit >= logical_width
                or not isinstance(n_port, str)
                or not isinstance(p_package_pin, str)
                or not isinstance(n_package_pin, str)
                or not isinstance(p_site, str)
                or not isinstance(n_site, str)
                or adapter is None
                or adapter.get("role") != expected_role
                or adapter.get("logical_port")
                != f"{logical_port}[{logical_bit}]"
                or adapter.get("logical_port_width") != logical_width
                or adapter.get("iostandard") != record.get("iostandard")
                or adapter.get("p", {}).get("package_pin")
                != p_package_pin
                or adapter.get("n", {}).get("package_pin")
                != n_package_pin
                or adapter.get("n", {}).get("port") != n_port
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count <= 0
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise ValidationError(
                    "virtual I/O physical-port membership is invalid"
                )
            seen.add(identity)
            seen_ports.add(p_port)
            scheduled_hops += count
        if seen_ports != set(adapter_by_port):
            raise ValidationError(
                "virtual I/O physical-port coverage differs from its adapter"
            )
        manifest = getattr(binding, "manifest", None)
        relation = {
            "logical_sha256": _artifact_digest(binding),
            "membership": (
                "per-port canonical JSON schedule-entry strings, newline framed"
            ),
            **(
                {"store_sha256": manifest["store_sha256"]}
                if isinstance(manifest, Mapping)
                and isinstance(manifest.get("store_sha256"), str)
                else {}
            ),
        }
        return {
            "schema": IO_CONTRACT_STORAGE_SCHEMA,
            "status": "pass",
            "provider": "phase6-indexed-xdc-differential-adapter-contract-v2",
            "platform": binding["platform"],
            "design": binding.get("design"),
            "fpga": fpga,
            "binding_sha256": _artifact_digest(binding),
            "binding_relation": relation,
            "xdc": {"path": str(xdc_path), "sha256": _file_sha256(xdc_path)},
            "adapter": {
                "path": str(adapter_path),
                "sha256": _file_sha256(adapter_path),
                "module": f"emuflow_virtual_io_{_name(fpga)}",
            },
            "implementation_contract": (
                "one fixed RapidWright LVDS IOB at the P package pin; the verified "
                "N package pin is the dedicated complementary mate"
            ),
            "entries": entries,
            "metrics": {
                "differential_pairs": len(entries),
                "physical_ports": 2 * len(entries),
                "scheduled_hops": scheduled_hops,
            },
        }

    records: Dict[str, Dict[str, Any]] = {}
    for item in binding["entries"]:
        for role, direction in (("source", "tx"), ("sink", "rx")):
            end = item[role]
            if end["fpga"] != fpga:
                continue
            match = re.fullmatch(
                r"([a-zA-Z_][a-zA-Z0-9_]*)\[(\d+)\]", end["logical_port"]
            )
            if match is None:
                raise ValidationError("virtual I/O contract has an invalid logical port")
            record = {
                "id": f"{direction}:{end['p']['port']}",
                "direction": direction,
                "link": item["link"],
                "wire": item["wire"],
                "wire_id": item["wire_id"],
                "bank": end["bank"],
                "pair_id": end["pair_id"],
                "iostandard": item["iostandard"],
                "logical_port": match.group(1),
                "logical_bit": int(match.group(2)),
                "logical_port_width": end["logical_port_width"],
                "p": dict(end["p"]),
                "n": dict(end["n"]),
                "schedule_entries": [],
            }
            key = record["id"]
            existing = records.setdefault(key, record)
            comparable = {k: v for k, v in record.items() if k != "schedule_entries"}
            if {k: v for k, v in existing.items() if k != "schedule_entries"} != comparable:
                raise ValidationError("one physical differential pair has conflicting bindings")
            existing["schedule_entries"].append(item["schedule_entry"])
    for record in records.values():
        record["schedule_entries"].sort()
    entries = sorted(records.values(), key=lambda item: item["id"])
    return {
        "schema": IO_CONTRACT_SCHEMA,
        "status": "pass",
        "provider": "phase6-xdc-differential-adapter-contract-v1",
        "platform": binding["platform"],
        "design": binding.get("design"),
        "fpga": fpga,
        "binding_sha256": _digest(binding),
        "xdc": {"path": str(xdc_path), "sha256": _file_sha256(xdc_path)},
        "adapter": {
            "path": str(adapter_path),
            "sha256": _file_sha256(adapter_path),
            "module": f"emuflow_virtual_io_{_name(fpga)}",
        },
        "implementation_contract": (
            "one fixed RapidWright LVDS IOB at the P package pin; the verified "
            "N package pin is the dedicated complementary mate"
        ),
        "entries": entries,
        "metrics": {
            "differential_pairs": len(entries),
            "physical_ports": 2 * len(entries),
            "scheduled_hops": sum(len(item["schedule_entries"]) for item in entries),
        },
    }


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
    from .default_platform import DEFAULT_PLATFORM_PATH
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
