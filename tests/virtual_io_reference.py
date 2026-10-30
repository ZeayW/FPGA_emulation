"""Independent record replay and legacy contract oracle; never installed."""
from __future__ import annotations
import hashlib
import json
import re
import os
import uuid
from collections import defaultdict
from itertools import zip_longest
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Protocol
from emuflow.common.errors import ValidationError
from emuflow.platform.platform import Platform
from emuflow_tools.transport.netlist import transport_endpoint_port
from emuflow.scheduling.tdm_schedule_storage import canonical_tdm_schedule_sha256
from emuflow_tools.transport.virtual_board import BINDING_SCHEMA, IO_CONTRACT_SCHEMA, IO_CONTRACT_STORAGE_SCHEMA, _digest, validate_virtual_board

from tests.board_wiring_reference import _name, _file_sha256

class _TextWriter(Protocol):
    def write(self, value: str) -> int:
        ...


def _artifact_digest(value: Any) -> str:
    """Use a sealed relation digest without expanding an indexed artifact."""
    canonical_sha256 = getattr(value, "canonical_sha256", None)
    if isinstance(canonical_sha256, str):
        return canonical_sha256
    logical_sha256 = getattr(value, "logical_sha256", None)
    return logical_sha256 if isinstance(logical_sha256, str) else _digest(value)


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


