"""Lossless-enough lowering of mapped EmuIR into VTR's eBLIF atoms.

The lowering preserves every EmuIR instance as one logic/FF/multiplier atom
or, for a word-wide VTR memory macro, the public VTR architecture's required
one-bit memory atoms.  A sidecar report binds the source IR hash, primitive
inventory, and emitted atom inventory so the physical flow cannot silently
switch back to the unsplit RTL design.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .equivalence import _lut_definition, _parameter_int
from .errors import ValidationError
from .io import write_json
from .ir import EmuIR
from .emuir_compact import compact_instances, compact_metadata, is_compact_emuir
from .emuir_storage import is_emuir_sqlite, open_emuir_sqlite_read_only


VTR_EBLIF_REPORT_SCHEMA = "emuflow.vtr-eblif-report/v1"
_FF_TYPES = {"FDCE", "FDPE", "FDRE", "FDSE"}
_MACRO_TYPES = {"VTR_MULTIPLY", "VTR_SP_RAM", "VTR_DP_RAM"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _bit(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value & 1
    text = str(value).strip().lower()
    if text in {"1", "1'b1", "true"}:
        return 1
    if text in {"0", "1'b0", "false", "x", "z"}:
        return 0
    if set(text) <= {"0", "1"}:
        return int(text, 2) & 1
    return int(text, 0) & 1


def _latch_init(instance: Mapping[str, Any]) -> int:
    raw = instance.get("parameters", {}).get("INIT")
    if raw is None or str(raw).strip().lower() in {"x", "z"}:
        return 2
    return _bit(raw)


def _is_lut(cell_type: str) -> bool:
    return cell_type.startswith("LUT") or cell_type in {"$lut", "$_LUT_"}


def _is_ff(cell_type: str) -> bool:
    return cell_type in _FF_TYPES or cell_type.startswith("$_DFF_")


class _Pins:
    def __init__(self, ir: EmuIR) -> None:
        self.net_names = {
            net["id"]: f"n{index}"
            for index, net in enumerate(ir.value["nets"])
        }
        self.pins: Dict[Tuple[str, str, int], str] = {}
        self.top: Dict[Tuple[str, int], str] = {}
        for net in ir.value["nets"]:
            name = self.net_names[net["id"]]
            for collection in ("drivers", "sinks"):
                for endpoint in net[collection]:
                    if endpoint["instance"] is None:
                        self.top[(endpoint["port"], endpoint["bit"])] = name
                    else:
                        self.pins[
                            (
                                endpoint["instance"],
                                endpoint["port"],
                                endpoint["bit"],
                            )
                        ] = name
        self.constants: Dict[Tuple[str, str, int], int] = {}
        for instance in ir.value["instances"]:
            for item in instance.get("constant_connections", []):
                self.constants[
                    (instance["id"], item["port"], item["bit"])
                ] = _bit(item["value"])
        self.dangling = 0

    def input(
        self,
        instance: Mapping[str, Any],
        port: str,
        bit: int = 0,
        default: int = 0,
    ) -> str:
        key = (instance["id"], port, bit)
        if key in self.pins:
            return self.pins[key]
        return "emuflow_const1" if self.constants.get(key, default) else (
            "emuflow_const0"
        )

    def output(
        self, instance: Mapping[str, Any], port: str, bit: int = 0
    ) -> str:
        key = (instance["id"], port, bit)
        if key in self.pins:
            return self.pins[key]
        name = f"emuflow_dangling_{self.dangling}"
        self.dangling += 1
        return name


def _lut_lines(
    instance: Mapping[str, Any], pins: _Pins, atom_name: str
) -> list[str]:
    width, input_port, output_port, truth = _lut_definition(instance)
    inputs = [
        pins.input(
            instance,
            f"I{index}" if input_port == "I" else input_port,
            0 if input_port == "I" else index,
        )
        for index in range(width)
    ]
    output = pins.output(instance, output_port)
    lines = [".names " + " ".join([*inputs, output])]
    for index in range(1 << width):
        if (truth >> index) & 1:
            pattern = "".join(
                "1" if (index >> offset) & 1 else "0"
                for offset in range(width)
            )
            lines.append(f"{pattern} 1")
    lines.append(f".cname {atom_name}")
    return lines


def _ff_lines(
    instance: Mapping[str, Any], pins: _Pins, atom_name: str
) -> Tuple[list[str], int]:
    cell_type = instance["type"]
    data = pins.input(instance, "D")
    output = pins.output(instance, "Q")
    clock = pins.input(instance, "C")
    init = _latch_init(instance)
    if cell_type.startswith("$_DFF_"):
        edge = "fe" if "_N_" in cell_type else "re"
        return [
            f".latch {data} {output} {edge} {clock} {init}",
            f".cname {atom_name}",
        ], 0

    enable = pins.input(instance, "CE", default=1)
    if cell_type in {"FDRE", "FDCE"}:
        control_port = "R" if cell_type == "FDRE" else "CLR"
        control_parameter = (
            "IS_R_INVERTED" if cell_type == "FDRE" else "IS_CLR_INVERTED"
        )
        control_value = 0
    else:
        control_port = "S" if cell_type == "FDSE" else "PRE"
        control_parameter = (
            "IS_S_INVERTED" if cell_type == "FDSE" else "IS_PRE_INVERTED"
        )
        control_value = 1
    control = pins.input(instance, control_port)
    data_inverted = _bit(
        instance.get("parameters", {}).get("IS_D_INVERTED", 0)
    )
    control_inverted = _bit(
        instance.get("parameters", {}).get(control_parameter, 0)
    )
    next_data = f"emuflow_ff_next_{atom_name}"
    lines = [f".names {data} {output} {enable} {control} {next_data}"]
    # Lower enable and synchronous set/reset to a LUT feeding VTR's plain DFF.
    for raw in range(16):
        d = ((raw >> 0) & 1) ^ data_inverted
        q = (raw >> 1) & 1
        ce = (raw >> 2) & 1
        ctl = ((raw >> 3) & 1) ^ control_inverted
        value = control_value if ctl else d if ce else q
        if value:
            lines.append(f"{raw & 1}{(raw >> 1) & 1}{(raw >> 2) & 1}{(raw >> 3) & 1} 1")
    lines.extend(
        (
            f".cname {atom_name}__control",
            f".latch {next_data} {output} re {clock} {init}",
            f".cname {atom_name}",
        )
    )
    return lines, 1


def _subckt(model: str, bindings: list[Tuple[str, str]], name: str) -> list[str]:
    return [
        ".subckt " + model + " " + " ".join(
            f"{port}={net}" for port, net in bindings
        ),
        f".cname {name}",
    ]


def _macro_lines(
    instance: Mapping[str, Any], pins: _Pins, atom_name: str
) -> Tuple[list[str], int]:
    cell_type = instance["type"]
    if cell_type == "VTR_MULTIPLY":
        widths = {
            "a": _parameter_int(instance, "A_WIDTH"),
            "b": _parameter_int(instance, "B_WIDTH"),
            "out": _parameter_int(instance, "Y_WIDTH"),
        }
        bindings = []
        for port in ("a", "b"):
            bindings.extend(
                (f"{port}[{bit}]", pins.input(instance, port, bit))
                for bit in range(widths[port])
            )
        bindings.extend(
            (f"out[{bit}]", pins.output(instance, "out", bit))
            for bit in range(widths["out"])
        )
        return _subckt("multiply", bindings, atom_name), 1

    width = _parameter_int(instance, "DATA_WIDTH")
    address_width = _parameter_int(instance, "ADDR_WIDTH")
    lines: list[str] = []
    if cell_type == "VTR_SP_RAM":
        for bit in range(width):
            bindings = [
                *[
                    (f"addr[{index}]", pins.input(instance, "addr", index))
                    for index in range(address_width)
                ],
                ("data", pins.input(instance, "data", bit)),
                ("we", pins.input(instance, "we")),
                ("out", pins.output(instance, "out", bit)),
                ("clk", pins.input(instance, "clk")),
            ]
            lines.extend(
                _subckt("single_port_ram", bindings, f"{atom_name}__bit{bit}")
            )
    else:
        for bit in range(width):
            bindings = [
                *[
                    (f"addr1[{index}]", pins.input(instance, "addr1", index))
                    for index in range(address_width)
                ],
                *[
                    (f"addr2[{index}]", pins.input(instance, "addr2", index))
                    for index in range(address_width)
                ],
                ("data1", pins.input(instance, "data1", bit)),
                ("data2", pins.input(instance, "data2", bit)),
                ("we1", pins.input(instance, "we1")),
                ("we2", pins.input(instance, "we2")),
                ("out1", pins.output(instance, "out1", bit)),
                ("out2", pins.output(instance, "out2", bit)),
                ("clk", pins.input(instance, "clk")),
            ]
            lines.extend(
                _subckt("dual_port_ram", bindings, f"{atom_name}__bit{bit}")
            )
    return lines, width


def validate_vtr_eblif_report(report: Mapping[str, Any]) -> Dict[str, Any]:
    if report.get("schema") != VTR_EBLIF_REPORT_SCHEMA:
        raise ValidationError("VTR eBLIF report schema is invalid")
    if report.get("status") != "pass":
        raise ValidationError("VTR eBLIF lowering did not pass")
    instances = report.get("source_instances")
    inventory = report.get("source_inventory")
    if not isinstance(instances, int) or instances < 0:
        raise ValidationError("VTR eBLIF source instance count is invalid")
    if not isinstance(inventory, dict) or sum(inventory.values()) != instances:
        raise ValidationError("VTR eBLIF source inventory is inconsistent")
    expected_atoms = (
        instances
        + report.get("memory_atom_expansion", 0)
        + report.get("ff_control_luts", 0)
        + report.get("output_alias_luts", 0)
    )
    if report.get("emitted_atoms") != expected_atoms:
        raise ValidationError("VTR eBLIF atom accounting is inconsistent")
    for field in ("source_sha256", "output_sha256"):
        value = report.get(field)
        if not isinstance(value, str) or len(value) != 64:
            raise ValidationError(f"VTR eBLIF {field} is invalid")
    top_ports = report.get("top_ports", [])
    if not isinstance(top_ports, list):
        raise ValidationError("VTR eBLIF top-port map is invalid")
    identities = set()
    for record in top_ports:
        if not isinstance(record, Mapping):
            raise ValidationError("VTR eBLIF top-port record is invalid")
        identity = (record.get("port"), record.get("bit"))
        if (
            not isinstance(identity[0], str)
            or isinstance(identity[1], bool)
            or not isinstance(identity[1], int)
            or identity[1] < 0
            or identity in identities
            or record.get("direction") not in {"input", "output"}
            or not isinstance(record.get("net"), str)
            or (
                "source_net" in record
                and not isinstance(record.get("source_net"), str)
            )
            or not isinstance(record.get("packed_block"), str)
        ):
            raise ValidationError("VTR eBLIF top-port map is inconsistent")
        identities.add(identity)
    return {
        "status": "pass",
        "source_instances": instances,
        "emitted_atoms": expected_atoms,
        "memory_atom_expansion": report.get("memory_atom_expansion", 0),
        "output_alias_luts": report.get("output_alias_luts", 0),
        "top_ports": len(top_ports),
    }


def _emit_vtr_eblif_materialized(
    ir_path: Path,
    output_path: Path,
    report_path: Optional[Path] = None,
) -> Dict[str, Any]:
    ir_path = ir_path.resolve()
    ir = EmuIR.load(ir_path)
    pins = _Pins(ir)
    inventory = Counter(
        instance["type"] for instance in ir.value["instances"]
    )
    unsupported = sorted(
        cell_type
        for cell_type in inventory
        if not (_is_lut(cell_type) or _is_ff(cell_type) or cell_type in _MACRO_TYPES)
    )
    if unsupported:
        raise ValidationError(
            "VTR eBLIF lowering does not support mapped primitives: "
            + ", ".join(unsupported)
        )

    top_digest = hashlib.sha256(
        ir.value["design"]["top"].encode()
    ).hexdigest()[:12]
    model = f"emuflow_partition_{top_digest}"
    inputs = []
    outputs = []
    output_aliases = []
    top_ports = []
    output_port_bits = [
        (port["id"], bit, pins.top[(port["id"], bit)])
        for port in ir.value["ports"]
        if port["direction"] == "output"
        for bit in range(port["width"])
        if (port["id"], bit) in pins.top
    ]
    output_net_counts = Counter(
        net for _port_id, _bit, net in output_port_bits
    )
    output_alias_names = {
        (port_id, bit): f"emuflow_top_output_{index:06d}"
        for index, (port_id, bit, _net) in enumerate(output_port_bits)
        if output_net_counts[_net] > 1
    }
    for port in ir.value["ports"]:
        target = inputs if port["direction"] == "input" else outputs
        if port["direction"] not in {"input", "output"}:
            raise ValidationError(
                f"VTR eBLIF cannot lower {port['direction']} port {port['id']!r}"
            )
        for bit in range(port["width"]):
            net = pins.top.get((port["id"], bit))
            if net is not None:
                external_net = net
                if (
                    port["direction"] == "output"
                    and output_net_counts[net] > 1
                ):
                    # A multicast source may feed multiple independent
                    # top-level transport ports.  eBLIF output pads are named
                    # by their output net; reusing the logical source net
                    # would make VPR collapse those physical pads into one
                    # packed I/O block.  Give every output port bit its own
                    # pad net and preserve the logical connection with a
                    # one-input buffer.
                    external_net = output_alias_names[(port["id"], bit)]
                    output_aliases.append((net, external_net))
                target.append(external_net)
                top_ports.append(
                    {
                        "port": port["id"],
                        "bit": bit,
                        "direction": port["direction"],
                        "net": external_net,
                        "source_net": net,
                        # VPR names an output pad block "out:<net>" and an
                        # input pad block with the atom net itself.  Preserve
                        # that exact packed-block identity so a later physical
                        # stage can bind Phase-6 package anchors to the packed
                        # I/O cluster without guessing from netlist order.
                        "packed_block": (
                            f"out:{external_net}"
                            if port["direction"] == "output"
                            else external_net
                        ),
                    }
                )
    lines = [f".model {model}"]
    lines.append(".inputs" + (" " + " ".join(sorted(set(inputs))) if inputs else ""))
    lines.append(".outputs" + (" " + " ".join(sorted(set(outputs))) if outputs else ""))
    lines.extend((".names emuflow_const0", ".names emuflow_const1", "1"))
    for source_net, output_net in output_aliases:
        lines.extend((f".names {source_net} {output_net}", "1 1"))

    ff_control_luts = 0
    memory_atom_expansion = 0
    base_atoms = 0
    hard_block_atoms = Counter()
    for index, instance in enumerate(ir.value["instances"]):
        atom_name = f"i{index}"
        cell_type = instance["type"]
        if _is_lut(cell_type):
            lines.extend(_lut_lines(instance, pins, atom_name))
            base_atoms += 1
        elif _is_ff(cell_type):
            emitted, controls = _ff_lines(instance, pins, atom_name)
            lines.extend(emitted)
            base_atoms += 1
            ff_control_luts += controls
        else:
            emitted, atoms = _macro_lines(instance, pins, atom_name)
            lines.extend(emitted)
            base_atoms += atoms
            memory_atom_expansion += atoms - 1
            hard_block_atoms[cell_type] += atoms
    lines.extend((".end", ""))
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    report = {
        "schema": VTR_EBLIF_REPORT_SCHEMA,
        "status": "pass",
        "provider": "emuir-to-vtr-eblif-v1",
        "design": ir.value["design"]["name"],
        "top": ir.value["design"]["top"],
        "source": str(ir_path),
        "source_sha256": _sha256(ir_path),
        "source_instances": len(ir.value["instances"]),
        "source_nets": len(ir.value["nets"]),
        "source_inventory": dict(sorted(inventory.items())),
        "clock_nets": {
            clock["id"]: pins.top[(clock["source_port"], 0)]
            for clock in ir.value["clocks"]
            if (clock["source_port"], 0) in pins.top
        },
        "top_ports": sorted(
            top_ports, key=lambda item: (item["port"], item["bit"])
        ),
        "ff_control_luts": ff_control_luts,
        "output_alias_luts": len(output_aliases),
        "memory_atom_expansion": memory_atom_expansion,
        "emitted_atoms": base_atoms + ff_control_luts + len(output_aliases),
        "hard_block_atoms": dict(sorted(hard_block_atoms.items())),
        "dangling_outputs": pins.dangling,
        "output": str(output_path),
        "output_sha256": _sha256(output_path),
    }
    report["validation"] = validate_vtr_eblif_report(report)
    if report_path is not None:
        write_json(report_path, report)
    return report


class _CompactPins:
    """Instance-local pin view for streamed compact-EmuIR eBLIF emission."""

    def __init__(self, connection) -> None:
        self.top = {
            (row[1], int(row[2])): f"n{int(row[0])}"
            for row in connection.execute(
                """SELECT e.net_seq,p.name,e.bit FROM endpoint_records AS e
                     JOIN pin_names AS p ON p.seq=e.port_seq
                    WHERE e.instance_seq IS NULL
                    ORDER BY e.net_seq,e.role,e.ordinal"""
            )
        }
        self.pins: Dict[Tuple[str, str, int], str] = {}
        self.constants: Dict[Tuple[str, str, int], int] = {}
        self.dangling = 0

    def bind(self, instance: Mapping[str, Any], rows) -> None:
        identity = instance["id"]
        self.pins = {
            (identity, row[1], int(row[2])): f"n{int(row[0])}"
            for row in rows
        }
        self.constants = {
            (identity, item["port"], item["bit"]): _bit(item["value"])
            for item in instance.get("constant_connections", [])
        }

    input = _Pins.input
    output = _Pins.output


def _emit_vtr_eblif_compact(
    ir_path: Path,
    output_path: Path,
    report_path: Optional[Path],
) -> Dict[str, Any]:
    connection = open_emuir_sqlite_read_only(ir_path)
    try:
        metadata = compact_metadata(connection)
        counts_row = connection.execute(
            "SELECT value FROM metadata WHERE key='record_counts_json'"
        ).fetchone()
        if counts_row is None:
            raise ValidationError("compact EmuIR record counts are missing")
        import json

        counts = json.loads(counts_row[0])
        inventory = Counter(
            {
                row[0]: int(row[1])
                for row in connection.execute(
                    "SELECT type,COUNT(*) FROM cell_records GROUP BY type"
                )
            }
        )
        unsupported = sorted(
            cell_type
            for cell_type in inventory
            if not (
                _is_lut(cell_type)
                or _is_ff(cell_type)
                or cell_type in _MACRO_TYPES
            )
        )
        if unsupported:
            raise ValidationError(
                "VTR eBLIF lowering does not support mapped primitives: "
                + ", ".join(unsupported)
            )

        pins = _CompactPins(connection)
        ports = metadata["ports"]
        output_port_bits = [
            (port["id"], bit, pins.top[(port["id"], bit)])
            for port in ports
            if port["direction"] == "output"
            for bit in range(port["width"])
            if (port["id"], bit) in pins.top
        ]
        output_net_counts = Counter(net for _, _, net in output_port_bits)
        output_alias_names = {
            (port_id, bit): f"emuflow_top_output_{index:06d}"
            for index, (port_id, bit, net) in enumerate(output_port_bits)
            if output_net_counts[net] > 1
        }
        inputs: list[str] = []
        outputs: list[str] = []
        output_aliases: list[Tuple[str, str]] = []
        top_ports = []
        for port in ports:
            target = inputs if port["direction"] == "input" else outputs
            if port["direction"] not in {"input", "output"}:
                raise ValidationError(
                    f"VTR eBLIF cannot lower {port['direction']} port {port['id']!r}"
                )
            for bit in range(port["width"]):
                net = pins.top.get((port["id"], bit))
                if net is None:
                    continue
                external_net = net
                if port["direction"] == "output" and output_net_counts[net] > 1:
                    external_net = output_alias_names[(port["id"], bit)]
                    output_aliases.append((net, external_net))
                target.append(external_net)
                top_ports.append(
                    {
                        "port": port["id"],
                        "bit": bit,
                        "direction": port["direction"],
                        "net": external_net,
                        "source_net": net,
                        "packed_block": (
                            f"out:{external_net}"
                            if port["direction"] == "output"
                            else external_net
                        ),
                    }
                )

        output_path = output_path.resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        ff_control_luts = 0
        memory_atom_expansion = 0
        base_atoms = 0
        hard_block_atoms = Counter()
        endpoint_cursor = iter(
            connection.execute(
                """SELECT e.instance_seq,e.net_seq,p.name,e.bit,e.role
                     FROM endpoint_records AS e INDEXED BY endpoint_owner
                     JOIN pin_names AS p ON p.seq=e.port_seq
                    WHERE e.instance_seq IS NOT NULL
                    ORDER BY e.instance_seq,e.port_seq,e.bit"""
            )
        )
        endpoint_row = next(endpoint_cursor, None)
        top = metadata["design"]["top"]
        model = "emuflow_partition_" + hashlib.sha256(
            top.encode()
        ).hexdigest()[:12]
        with output_path.open("w", encoding="utf-8") as output:
            output.write(f".model {model}\n")
            output.write(
                ".inputs"
                + (" " + " ".join(sorted(set(inputs))) if inputs else "")
                + "\n"
            )
            output.write(
                ".outputs"
                + (" " + " ".join(sorted(set(outputs))) if outputs else "")
                + "\n"
            )
            output.write(".names emuflow_const0\n.names emuflow_const1\n1\n")
            for source_net, output_net in output_aliases:
                output.write(f".names {source_net} {output_net}\n1 1\n")
            # Integer owners are persistent SQLite identities, not dense
            # iteration ordinals. Phase-7 merge may start at one or retain
            # gaps; use the same identity for pin binding and VPR atom names.
            instance_sequences = connection.execute(
                "SELECT seq FROM cell_records ORDER BY seq"
            )
            for (index,), instance in zip(
                instance_sequences, compact_instances(connection)
            ):
                rows = []
                while endpoint_row is not None and int(endpoint_row[0]) == index:
                    if int(endpoint_row[4]) not in (0, 1):
                        raise ValidationError("compact EmuIR endpoint role is invalid")
                    rows.append(endpoint_row[1:])
                    endpoint_row = next(endpoint_cursor, None)
                if endpoint_row is not None and int(endpoint_row[0]) < index:
                    raise ValidationError("compact EmuIR endpoint owner is invalid")
                pins.bind(instance, rows)
                atom_name = f"i{index}"
                cell_type = instance["type"]
                if _is_lut(cell_type):
                    emitted = _lut_lines(instance, pins, atom_name)
                    base_atoms += 1
                elif _is_ff(cell_type):
                    emitted, controls = _ff_lines(instance, pins, atom_name)
                    base_atoms += 1
                    ff_control_luts += controls
                else:
                    emitted, atoms = _macro_lines(instance, pins, atom_name)
                    base_atoms += atoms
                    memory_atom_expansion += atoms - 1
                    hard_block_atoms[cell_type] += atoms
                for line in emitted:
                    output.write(line + "\n")
            if endpoint_row is not None:
                raise ValidationError("compact EmuIR endpoint owner is invalid")
            output.write(".end\n")

        report = {
            "schema": VTR_EBLIF_REPORT_SCHEMA,
            "status": "pass",
            "provider": "emuir-to-vtr-eblif-v1",
            "design": metadata["design"]["name"],
            "top": top,
            "source": str(ir_path),
            "source_sha256": _sha256(ir_path),
            "source_instances": int(counts["cell_records"]),
            "source_nets": int(counts["net_records"]),
            "source_inventory": dict(sorted(inventory.items())),
            "clock_nets": {
                clock["id"]: pins.top[(clock["source_port"], 0)]
                for clock in metadata["clocks"]
                if (clock["source_port"], 0) in pins.top
            },
            "top_ports": sorted(top_ports, key=lambda item: (item["port"], item["bit"])),
            "ff_control_luts": ff_control_luts,
            "output_alias_luts": len(output_aliases),
            "memory_atom_expansion": memory_atom_expansion,
            "emitted_atoms": base_atoms + ff_control_luts + len(output_aliases),
            "hard_block_atoms": dict(sorted(hard_block_atoms.items())),
            "dangling_outputs": pins.dangling,
            "output": str(output_path),
            "output_sha256": _sha256(output_path),
        }
        report["validation"] = validate_vtr_eblif_report(report)
        if report_path is not None:
            write_json(report_path, report)
        return report
    finally:
        connection.close()


def emit_vtr_eblif(
    ir_path: Path,
    output_path: Path,
    report_path: Optional[Path] = None,
) -> Dict[str, Any]:
    ir_path = ir_path.resolve()
    if is_emuir_sqlite(ir_path) and is_compact_emuir(ir_path):
        return _emit_vtr_eblif_compact(ir_path, output_path, report_path)
    return _emit_vtr_eblif_materialized(ir_path, output_path, report_path)
