"""Endpoint-specific routed timing handoff for the open VPR backend."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping

from .boundary_timing import (
    BOUNDARY_IDENTITY_SCHEMA,
    build_boundary_timing_database,
    validate_boundary_timing_database,
)
from .errors import ValidationError
from .io import read_json, write_json
from .ir import EmuIR
from .emuir_compact import is_compact_emuir
from .emuir_storage import is_emuir_sqlite
from .indexed_physical_ir import IndexedPhysicalIR


VPR_BOUNDARY_QUERY_HEADER = "endpoint\tkind\tstart_pin\tend_pin"
VPR_BOUNDARY_TIMING_HEADER = (
    "endpoint\tkind\tdelay_ns\tstart_pin\tend_pin"
)
_FF_TYPES = {"FDCE", "FDPE", "FDRE", "FDSE"}


def _atom_pin(
    instance: Mapping[str, Any], index: int, port: str, bit: int
) -> str:
    cell_type = instance["type"]
    atom = f"i{index}"
    if cell_type.startswith("LUT") or cell_type in {"$lut", "$_LUT_"}:
        return f"{atom}.out[0]"
    if cell_type in _FF_TYPES or cell_type.startswith("$_DFF_"):
        if port not in {"D", "Q"} or bit != 0:
            raise ValidationError(
                f"unsupported VPR FF boundary pin {instance['id']}.{port}[{bit}]"
            )
        return f"{atom}.{port}[0]"
    if cell_type == "VTR_MULTIPLY":
        if port != "out":
            raise ValidationError(
                f"unsupported VPR multiplier boundary pin {port!r}"
            )
        return f"{atom}.out[{bit}]"
    if cell_type in {"VTR_SP_RAM", "VTR_DP_RAM"}:
        allowed = {"out"} if cell_type == "VTR_SP_RAM" else {"out1", "out2"}
        if port not in allowed:
            raise ValidationError(
                f"unsupported VPR RAM boundary pin {port!r}"
            )
        return f"{atom}__bit{bit}.{port}[0]"
    raise ValidationError(
        f"unsupported VPR boundary source cell type {cell_type!r}"
    )


def _logical_tx_start(
    nets: Mapping[str, Mapping[str, Any]],
    instances: Mapping[str, Mapping[str, Any]],
    net_id: str,
    net_index: Mapping[str, int],
    instance_index: Mapping[str, int],
) -> str:
    net = nets.get(net_id)
    if net is None or len(net["drivers"]) != 1:
        raise ValidationError(
            f"VPR TX boundary net {net_id!r} lacks one logical driver"
        )
    driver = net["drivers"][0]
    instance_id = driver.get("instance")
    if instance_id is None:
        return f"n{net_index[net_id]}.inpad[0]"
    instance = instances.get(instance_id)
    if instance is None:
        raise ValidationError(
            f"VPR TX boundary driver {instance_id!r} is absent"
        )
    return _atom_pin(
        instance,
        instance_index[instance_id],
        driver["port"],
        driver["bit"],
    )


def write_vpr_boundary_timing_query(
    ir_path: Path,
    identity_path: Path,
    output_path: Path,
    *,
    eblif_report: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    if is_emuir_sqlite(ir_path) and is_compact_emuir(ir_path):
        with IndexedPhysicalIR(ir_path) as ir:
            return _write_vpr_boundary_timing_query(
                ir, identity_path, output_path, eblif_report=eblif_report
            )
    return _write_vpr_boundary_timing_query(
        EmuIR.load(ir_path), identity_path, output_path,
        eblif_report=eblif_report,
    )


def _write_vpr_boundary_timing_query(
    ir,
    identity_path: Path,
    output_path: Path,
    *,
    eblif_report: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    identities = read_json(identity_path)
    if identities.get("schema") != BOUNDARY_IDENTITY_SCHEMA:
        raise ValidationError("VPR boundary timing identity schema is invalid")
    if isinstance(ir, IndexedPhysicalIR):
        net_index, instance_index = ir.net_indices, ir.instance_indices
        instances, nets = ir.instances, ir.nets
    else:
        net_index = {
            net["id"]: index for index, net in enumerate(ir.value["nets"])
        }
        instance_index = {
            instance["id"]: index
            for index, instance in enumerate(ir.value["instances"])
        }
        instances = {
            instance["id"]: instance for instance in ir.value["instances"]
        }
        nets = {net["id"]: net for net in ir.value["nets"]}
    top_ports = None
    if eblif_report is not None:
        records = eblif_report.get("top_ports")
        if not isinstance(records, list):
            raise ValidationError("VPR boundary eBLIF top-port map is invalid")
        top_ports = {}
        for record in records:
            if not isinstance(record, Mapping):
                raise ValidationError(
                    "VPR boundary eBLIF top-port record is invalid"
                )
            identity = (record.get("port"), record.get("bit"))
            if (
                not isinstance(identity[0], str)
                or isinstance(identity[1], bool)
                or not isinstance(identity[1], int)
                or identity in top_ports
                or record.get("direction") not in {"input", "output"}
                or not isinstance(record.get("net"), str)
                or not isinstance(record.get("packed_block"), str)
            ):
                raise ValidationError(
                    "VPR boundary eBLIF top-port map is inconsistent"
                )
            top_ports[identity] = record
    lines = [VPR_BOUNDARY_QUERY_HEADER]
    seen = set()
    for endpoint in identities.get("endpoints", []):
        endpoint_id = endpoint.get("id")
        kind = endpoint.get("kind")
        merged = endpoint.get("merged_ir")
        if (
            not isinstance(endpoint_id, str)
            or endpoint_id in seen
            or kind not in {"tx", "rx"}
            or not isinstance(merged, dict)
        ):
            raise ValidationError("VPR boundary timing identity is invalid")
        seen.add(endpoint_id)
        external_net = merged.get("external_net")
        if external_net not in net_index:
            raise ValidationError(
                f"VPR boundary endpoint {endpoint_id!r} external net is absent"
            )
        if top_ports is None:
            io_pin = (
                f"out:n{net_index[external_net]}.outpad[0]"
                if kind == "tx"
                else f"n{net_index[external_net]}.inpad[0]"
            )
        else:
            external_port = merged.get("external_port")
            external_bit = merged.get("external_port_bit")
            record = top_ports.get((external_port, external_bit))
            expected_direction = "output" if kind == "tx" else "input"
            if (
                record is None
                or record["direction"] != expected_direction
                or record.get("source_net", record["net"])
                != f"n{net_index[external_net]}"
            ):
                raise ValidationError(
                    f"VPR boundary endpoint {endpoint_id!r} disagrees with "
                    "the eBLIF top-port map"
                )
            suffix = "outpad[0]" if kind == "tx" else "inpad[0]"
            io_pin = f"{record['packed_block']}.{suffix}"
        registers = merged.get("boundary_register_instances")
        if not isinstance(registers, list):
            raise ValidationError(
                f"VPR boundary endpoint {endpoint_id!r} register map is invalid"
            )
        if kind == "tx":
            if registers:
                if len(registers) != 1 or registers[0] not in instance_index:
                    raise ValidationError(
                        f"VPR TX endpoint {endpoint_id!r} register is invalid"
                    )
                register = registers[0]
                start = _atom_pin(
                    instances[register], instance_index[register], "Q", 0
                )
            else:
                logical_net = merged.get("logical_net")
                if logical_net not in net_index:
                    raise ValidationError(
                        f"VPR TX endpoint {endpoint_id!r} logical net is absent"
                    )
                start = _logical_tx_start(
                    nets,
                    instances,
                    logical_net,
                    net_index,
                    instance_index,
                )
            end = io_pin
        else:
            if len(registers) != 1 or registers[0] not in instance_index:
                raise ValidationError(
                    f"VPR RX endpoint {endpoint_id!r} register is invalid"
                )
            register = registers[0]
            start = io_pin
            end = _atom_pin(
                instances[register], instance_index[register], "D", 0
            )
        if any(
            "\t" in value or "\n" in value
            for value in (endpoint_id, start, end)
        ):
            raise ValidationError("VPR boundary query contains unsafe text")
        lines.append("\t".join((endpoint_id, kind, start, end)))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "status": "pass",
        "fpga": identities["fpga"],
        "endpoints": len(lines) - 1,
        "unique_starts": len({line.split("\t")[2] for line in lines[1:]}),
        "unique_ends": len({line.split("\t")[3] for line in lines[1:]}),
        "output": str(output_path),
    }


def import_vpr_boundary_timing(
    input_path: Path,
    identity_path: Path,
    query_path: Path,
    output_path: Path,
) -> Dict[str, Any]:
    identities = read_json(identity_path)
    query_lines = query_path.read_text(encoding="utf-8").splitlines()
    if not query_lines or query_lines[0] != VPR_BOUNDARY_QUERY_HEADER:
        raise ValidationError("VPR boundary timing query header is invalid")
    queries = {}
    for line_number, line in enumerate(query_lines[1:], start=2):
        fields = line.split("\t")
        if len(fields) != 4 or fields[0] in queries:
            raise ValidationError(
                f"VPR boundary timing query line {line_number} is invalid"
            )
        queries[fields[0]] = tuple(fields[1:])
    lines = input_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != VPR_BOUNDARY_TIMING_HEADER:
        raise ValidationError("VPR boundary timing TSV header is invalid")
    expected_kind = {
        endpoint["id"]: endpoint["kind"]
        for endpoint in identities.get("endpoints", [])
    }
    if set(queries) != set(expected_kind):
        raise ValidationError(
            "VPR boundary timing query coverage disagrees with identities"
        )
    measurements = {}
    for line_number, line in enumerate(lines[1:], start=2):
        if not line:
            continue
        fields = line.split("\t")
        if (
            len(fields) != 5
            or fields[0] in measurements
            or expected_kind.get(fields[0]) != fields[1]
            or queries.get(fields[0])
            != (fields[1], fields[3], fields[4])
        ):
            raise ValidationError(
                f"VPR boundary timing line {line_number} identity is invalid"
            )
        try:
            delay = float(fields[2])
        except ValueError as error:
            raise ValidationError(
                f"VPR boundary timing line {line_number} delay is invalid"
            ) from error
        measurements[fields[0]] = {
            "delay_ns": delay,
            "start_object": fields[3],
            "end_object": fields[4],
        }
    database = build_boundary_timing_database(
        identities,
        measurements,
        provider="vpr-tatum-endpoint-longest-path-v1",
        qualification="routed-academic-architecture-endpoint-exact",
    )
    validation = validate_boundary_timing_database(database, identities)
    write_json(output_path, database)
    return {**validation, "output": str(output_path)}
