"""Endpoint-exact physical timing for DUT logic between TDM boundaries."""

from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple, Union

from .boundary_timing import BOUNDARY_IDENTITY_SCHEMA
from .errors import ValidationError
from .io import read_json, write_json
from .ir import EmuIR
from .partition import PARTITION_ASSIGNMENT_SCHEMA
from .placement import _vivado_mapped_name
from .platform import Platform
from .sta import (
    STA_PATH_DATABASE_SCHEMA,
    sta_object_index,
    sta_path_endpoints,
)
from .tdm import reconstruct_tdm_schedule_timing_paths


LOGIC_SEGMENT_IDENTITY_SCHEMA = "emuflow.logic-segment-identity/v1"
LOGIC_SEGMENT_TIMING_SCHEMA = "emuflow.logic-segment-timing/v1"
LOGIC_SEGMENT_QUERY_HEADER = "endpoint\tkind\tstart_pin\tend_pin"
LOGIC_SEGMENT_TIMING_HEADER = (
    "endpoint\tkind\tdelay_ns\tstart_pin\tend_pin"
)
VIVADO_LOGIC_SEGMENT_QUERY_HEADER = (
    "endpoint_hex\tkind\tstart_kind\tstart_object_hex\t"
    "end_kind\tend_object_hex\tcone_anchor_kind\tcone_anchor_object_hex"
)
VIVADO_LOGIC_SEGMENT_TIMING_HEADER = (
    "endpoint_hex\tkind\tdelay_ns\tstart_object_hex\tend_object_hex\t"
    "measurement\tactual_start_object_hex\tactual_end_object_hex"
)
_LEGACY_VIVADO_LOGIC_SEGMENT_TIMING_HEADER = (
    "endpoint_hex\tkind\tdelay_ns\tstart_object_hex\tend_object_hex"
)
_FF_TYPES = {"FDCE", "FDPE", "FDRE", "FDSE"}
_STATE_RESOURCES = {"ff", "bram", "bram18k", "uram288"}


def _incoming_transported_cut_nets(
    exact_contract: Mapping[str, Any], fpga: str
) -> Set[str]:
    """Return only transported cuts that enter ``fpga``.

    A logical net may itself be transported to some *other* FPGA while still
    being an ordinary local fan-in on its source FPGA.  Treating every
    transported net as a fan-in stop point drops the architectural launches of
    such locally originated nets.  The exact contract already records the
    destination set, so use that causal direction rather than mere membership
    in the global transported-net set.
    """

    return {
        item["net"]
        for item in exact_contract["cut_nodes"]
        if fpga in item["sink_fpgas"]
    }


def _architectural_launch_endpoints(
    ir: EmuIR,
    instance_assignment: Mapping[str, str],
    sink_net: str,
    fpga: str,
    incoming_cut_nets: Set[str],
    *,
    nets: Optional[Mapping[str, Mapping[str, Any]]] = None,
    instances: Optional[Mapping[str, Mapping[str, Any]]] = None,
    incoming: Optional[Mapping[str, List[str]]] = None,
) -> List[Mapping[str, Any]]:
    """Return every local architectural source in a cut-net fan-in cone.

    Incoming transported cuts are deliberate stop points: their independently
    routed RX-to-TX contracts cover those branches.  All remaining primary
    inputs and state/memory outputs are retained so a reconvergent
    launch-to-TX segment cannot be certified by measuring only the single
    representative path selected by the original STA database.
    """

    # A physical timing query may contain tens of thousands of static-exact
    # segments.  The net/instance/fan-in indexes describe the whole original
    # design and must therefore be built once by the caller, not once per
    # segment.  Keep the optional fallback for direct callers and small-unit
    # tests, while the production path supplies the shared indexes below.
    if nets is None:
        nets = {item["id"]: item for item in ir.value["nets"]}
    if instances is None:
        instances = {item["id"]: item for item in ir.value["instances"]}
    if incoming is None:
        built_incoming: Dict[str, List[str]] = defaultdict(list)
        for net in ir.value["nets"]:
            for endpoint in net["sinks"]:
                instance = endpoint["instance"]
                if instance is not None:
                    built_incoming[instance].append(net["id"])
        incoming = built_incoming
    if sink_net not in nets:
        raise ValidationError(
            f"static exact launch sink net {sink_net!r} is absent"
        )
    launches: Dict[Tuple[str, str, int], Mapping[str, Any]] = {}
    pending = [sink_net]
    visited = set()
    while pending:
        net_id = pending.pop()
        if net_id in visited:
            continue
        visited.add(net_id)
        if net_id != sink_net and net_id in incoming_cut_nets:
            continue
        net = nets.get(net_id)
        if net is None:
            raise ValidationError(
                f"static exact launch cone net {net_id!r} is absent"
            )
        for driver in net["drivers"]:
            instance_id = driver["instance"]
            if instance_id is None:
                key = ("", driver["port"], driver["bit"])
                launches[key] = driver
                continue
            instance = instances.get(instance_id)
            if instance is None:
                raise ValidationError(
                    f"static exact launch driver {instance_id!r} is absent"
                )
            if instance_assignment.get(instance_id) != fpga:
                if net_id in incoming_cut_nets:
                    continue
                raise ValidationError(
                    "static exact launch cone crosses an undeclared "
                    f"partition boundary at {net_id!r}"
                )
            resources = instance.get("resources", {})
            if any(resources.get(field, 0) for field in _STATE_RESOURCES):
                key = (instance_id, driver["port"], driver["bit"])
                launches[key] = driver
                continue
            pending.extend(incoming.get(instance_id, []))
    return [launches[key] for key in sorted(launches)]


def _instance_pin_inventory(ir: EmuIR) -> Dict[str, set[tuple[str, int]]]:
    result: Dict[str, set[tuple[str, int]]] = {
        instance["id"]: set() for instance in ir.value["instances"]
    }
    for net in ir.value["nets"]:
        for collection in ("drivers", "sinks"):
            for endpoint in net[collection]:
                instance = endpoint["instance"]
                if instance is not None:
                    result[instance].add((endpoint["port"], endpoint["bit"]))
    for instance in ir.value["instances"]:
        for endpoint in instance.get("constant_connections", []):
            result[instance["id"]].add((endpoint["port"], endpoint["bit"]))
    return result


def _scalar_pin(port: str, bit: int, pins: set[tuple[str, int]]) -> str:
    width = max(
        (candidate_bit + 1 for candidate_port, candidate_bit in pins
         if candidate_port == port),
        default=0,
    )
    return port if width <= 1 else f"{port}__{bit}"


def _top_pin(
    ir: EmuIR,
    endpoint: Mapping[str, Any],
    eblif_top_ports: Optional[
        Mapping[tuple[str, int], Mapping[str, Any]]
    ] = None,
) -> str:
    matches = []
    for index, net in enumerate(ir.value["nets"]):
        for collection in ("drivers", "sinks"):
            if any(
                item["instance"] is None
                and item["port"] == endpoint["port"]
                and item["bit"] == endpoint["bit"]
                for item in net[collection]
            ):
                matches.append((index, collection))
    if len(matches) != 1:
        raise ValidationError(
            f"physical top endpoint {endpoint!r} does not bind one net"
        )
    net_index, collection = matches[0]
    if eblif_top_ports is not None:
        record = eblif_top_ports.get((endpoint["port"], endpoint["bit"]))
        direction = "input" if collection == "drivers" else "output"
        if (
            record is None
            or record.get("direction") != direction
            or record.get("source_net", record.get("net")) != f"n{net_index}"
            or not isinstance(record.get("packed_block"), str)
        ):
            raise ValidationError(
                f"physical top endpoint {endpoint!r} disagrees with "
                "the eBLIF top-port map"
            )
        suffix = "inpad[0]" if direction == "input" else "outpad[0]"
        return f"{record['packed_block']}.{suffix}"
    return (
        f"n{net_index}.inpad[0]"
        if collection == "drivers"
        else f"out:n{net_index}.outpad[0]"
    )


def _vpr_atom_pin(
    ir: EmuIR,
    instance_index: Mapping[str, int],
    endpoint: Mapping[str, Any],
    instances: Optional[Mapping[str, Mapping[str, Any]]] = None,
    eblif_top_ports: Optional[
        Mapping[tuple[str, int], Mapping[str, Any]]
    ] = None,
) -> str:
    instance_id = endpoint["instance"]
    if instance_id is None:
        return _top_pin(ir, endpoint, eblif_top_ports)
    if instances is None:
        instances = {
            instance["id"]: instance for instance in ir.value["instances"]
        }
    if instance_id not in instances or instance_id not in instance_index:
        raise ValidationError(
            f"physical logic endpoint instance {instance_id!r} is absent"
        )
    instance = instances[instance_id]
    cell_type = instance["type"]
    port = endpoint["port"]
    bit = endpoint["bit"]
    atom = f"i{instance_index[instance_id]}"
    if cell_type.startswith("LUT"):
        if port == "O":
            return f"{atom}.out[0]"
        match = re.fullmatch(r"I(\d+)", port)
        if match is not None and bit == 0:
            return f"{atom}.in[{int(match.group(1))}]"
    if cell_type in {"$lut", "$_LUT_"}:
        if port in {"Y", "O"}:
            return f"{atom}.out[0]"
        if port in {"A", "I"}:
            return f"{atom}.in[{bit}]"
    if cell_type in _FF_TYPES or cell_type.startswith("$_DFF_"):
        if port in {"D", "Q"} and bit == 0:
            return f"{atom}.{port}[0]"
    if cell_type == "VTR_MULTIPLY" and port in {"a", "b", "out"}:
        return f"{atom}.{port}[{bit}]"
    if cell_type == "VTR_SP_RAM":
        if port not in {"addr", "data", "we", "out", "clk"}:
            raise ValidationError(
                f"unsupported VTR SP-RAM pin {instance_id}.{port}[{bit}]"
            )
        atom_bit = bit if port in {"data", "out"} else 0
        pin_bit = bit if port == "addr" else 0
        return f"{atom}__bit{atom_bit}.{port}[{pin_bit}]"
    if cell_type == "VTR_DP_RAM":
        if port not in {
            "addr1", "addr2", "data1", "data2", "we1", "we2",
            "out1", "out2", "clk",
        }:
            raise ValidationError(
                f"unsupported VTR DP-RAM pin {instance_id}.{port}[{bit}]"
            )
        atom_bit = bit if port in {"data1", "data2", "out1", "out2"} else 0
        pin_bit = bit if port in {"addr1", "addr2"} else 0
        return f"{atom}__bit{atom_bit}.{port}[{pin_bit}]"
    raise ValidationError(
        f"unsupported physical logic pin {instance_id}.{port}[{bit}] "
        f"on {cell_type}"
    )


def _vivado_object(
    ir: EmuIR,
    endpoint: Mapping[str, Any],
    pins: Optional[Mapping[str, set[tuple[str, int]]]] = None,
    instances: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> tuple[str, str]:
    """Return an exact routed Vivado object kind/name for an EmuIR pin."""
    instance_id = endpoint["instance"]
    port = endpoint["port"]
    bit = endpoint["bit"]
    if instance_id is None:
        ports = {item["id"]: item for item in ir.value["ports"]}
        if port not in ports or bit >= ports[port]["width"]:
            raise ValidationError(
                f"physical top endpoint {port}[{bit}] is absent"
            )
        name = port if ports[port]["width"] == 1 else f"{port}[{bit}]"
        return "port", name
    if instances is None:
        instances = {
            instance["id"]: instance for instance in ir.value["instances"]
        }
    if pins is None:
        pins = _instance_pin_inventory(ir)
    if instance_id not in instances or (port, bit) not in pins[instance_id]:
        raise ValidationError(
            f"physical logic endpoint {instance_id}.{port}[{bit}] is absent"
        )
    instance = instances[instance_id]
    cell_type = instance["type"]
    provider_object = endpoint.get("object")
    mapped_prefix = f"{_vivado_mapped_name(instance_id)}/"
    if (
        isinstance(provider_object, str)
        and provider_object.startswith(mapped_prefix)
        and "/memory_reg_bram_" in provider_object
    ):
        return "pin", provider_object
    physical_port = port
    physical_bit = bit
    if cell_type.startswith("LUT"):
        if port == "O":
            physical_port, physical_bit = "O", 0
        elif re.fullmatch(r"I\d+", port) is None or bit != 0:
            raise ValidationError(
                f"unsupported Vivado LUT pin {instance_id}.{port}[{bit}]"
            )
    elif cell_type in {"$lut", "$_LUT_"}:
        if port in {"Y", "O"}:
            physical_port, physical_bit = "O", 0
        elif port in {"A", "I"}:
            physical_port, physical_bit = f"I{bit}", 0
        else:
            raise ValidationError(
                f"unsupported Vivado LUT pin {instance_id}.{port}[{bit}]"
            )
    elif cell_type in _FF_TYPES or cell_type.startswith("$_DFF_"):
        if port not in {"C", "D", "Q"} or bit != 0:
            raise ValidationError(
                f"unsupported Vivado FF pin {instance_id}.{port}[{bit}]"
            )
    elif cell_type not in {"VTR_MULTIPLY", "VTR_SP_RAM", "VTR_DP_RAM"}:
        raise ValidationError(
            f"unsupported Vivado physical logic cell {cell_type}"
        )
    if cell_type.startswith("LUT") or cell_type in {"$lut", "$_LUT_"}:
        pin_name = physical_port
    else:
        width = max(
            (
                candidate_bit + 1
                for candidate_port, candidate_bit in pins[instance_id]
                if candidate_port == port
            ),
            default=0,
        )
        pin_name = (
            physical_port
            if physical_bit == 0 and width <= 1
            else f"{physical_port}[{physical_bit}]"
        )
    return "pin", f"{_vivado_mapped_name(instance_id)}/{pin_name}"


def _boundary_maps(
    identity: Mapping[str, Any],
) -> tuple[Dict[str, Mapping[str, Any]], Dict[str, Mapping[str, Any]]]:
    if identity.get("schema") != BOUNDARY_IDENTITY_SCHEMA:
        raise ValidationError("logic segment boundary identity is invalid")
    endpoints = {item["id"]: item for item in identity["endpoints"]}
    if len(endpoints) != len(identity["endpoints"]):
        raise ValidationError("logic segment boundary endpoint IDs duplicate")
    return endpoints, {
        item["schedule_entry"]: item for item in identity["endpoints"]
    }


def _boundary_tx_port(
    ir: EmuIR,
    instance_index: Mapping[str, int],
    endpoints: Mapping[str, Mapping[str, Any]],
    endpoint_id: str,
    net_index: Optional[Mapping[str, int]] = None,
    eblif_top_ports: Optional[
        Mapping[tuple[str, int], Mapping[str, Any]]
    ] = None,
) -> str:
    endpoint = endpoints.get(endpoint_id)
    if endpoint is None or endpoint.get("kind") != "tx":
        raise ValidationError(f"logic segment TX {endpoint_id!r} is absent")
    external_net = endpoint["merged_ir"]["external_net"]
    if net_index is None:
        net_index = {
            net["id"]: index for index, net in enumerate(ir.value["nets"])
        }
    if external_net not in net_index:
        raise ValidationError("logic segment TX external net is absent")
    if eblif_top_ports is not None:
        merged = endpoint["merged_ir"]
        record = eblif_top_ports.get(
            (merged.get("external_port"), merged.get("external_port_bit"))
        )
        if (
            record is None
            or record.get("direction") != "output"
            or record.get("source_net", record.get("net"))
            != f"n{net_index[external_net]}"
            or not isinstance(record.get("packed_block"), str)
        ):
            raise ValidationError(
                f"logic segment TX {endpoint_id!r} disagrees with "
                "the eBLIF top-port map"
            )
        return f"{record['packed_block']}.outpad[0]"
    return f"out:n{net_index[external_net]}.outpad[0]"


def _boundary_rx_q(
    ir: EmuIR,
    instance_index: Mapping[str, int],
    endpoints: Mapping[str, Mapping[str, Any]],
    endpoint_id: str,
    instances: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> str:
    endpoint = endpoints.get(endpoint_id)
    if endpoint is None or endpoint.get("kind") != "rx":
        raise ValidationError(f"logic segment RX {endpoint_id!r} is absent")
    registers = endpoint["merged_ir"]["boundary_register_instances"]
    if not isinstance(registers, list) or len(registers) != 1:
        raise ValidationError(f"logic segment RX {endpoint_id!r} is ambiguous")
    return _vpr_atom_pin(
        ir,
        instance_index,
        {"instance": registers[0], "port": "Q", "bit": 0},
        instances,
    )


def _segment_id(member: str, cut_index: int, role: str) -> str:
    digest = hashlib.sha256(
        f"{member}\0{cut_index}\0{role}".encode("utf-8")
    ).hexdigest()[:24]
    return f"logic_{digest}_{cut_index}_{role}"


@dataclass(frozen=True)
class LogicSegmentQueryInputs:
    """Immutable global timing state shared by all per-FPGA workers."""

    original_ir: EmuIR
    assignment: Mapping[str, Any]
    path_database: Mapping[str, Any]
    routes: Mapping[str, Any]
    schedule: Mapping[str, Any]
    object_index: Mapping[str, Any]
    database_paths: Mapping[str, Mapping[str, Any]]
    route_timing: Mapping[str, Mapping[str, Any]]
    route_by_net: Mapping[str, Mapping[str, Any]]
    timing_records: List[Mapping[str, Any]]
    original_nets: Mapping[str, Mapping[str, Any]]
    original_instances: Mapping[str, Mapping[str, Any]]
    incoming_nets_by_instance: Mapping[str, List[str]]
    exact_contract: Optional[Mapping[str, Any]]
    exact_contract_sha256: Optional[str]
    exact_segment_by_key: Mapping[Any, str]
    exact_captures: Mapping[str, Mapping[str, Any]]


def prepare_logic_segment_query_inputs(
    original_ir_path: Path,
    assignment_path: Path,
    path_database_path: Path,
    routes_path: Path,
    schedule_path: Path,
    platform: Platform,
) -> LogicSegmentQueryInputs:
    """Parse and index global query inputs exactly once per physical run."""
    original_ir = EmuIR.load(original_ir_path)
    assignment = read_json(assignment_path)
    path_database = read_json(path_database_path)
    routes = read_json(routes_path)
    schedule = read_json(schedule_path)
    if assignment.get("schema") != PARTITION_ASSIGNMENT_SCHEMA:
        raise ValidationError("logic segment assignment schema is invalid")
    if path_database.get("schema") != STA_PATH_DATABASE_SCHEMA:
        raise ValidationError("logic segment STA database schema is invalid")

    exact_contract = schedule.get("semantic_contract")
    exact_contract_sha256 = None
    exact_segment_by_key: Dict[Any, str] = {}
    exact_captures: Dict[str, Mapping[str, Any]] = {}
    if isinstance(exact_contract, dict) and exact_contract.get("mode") == (
        "static-exact-combinational"
    ):
        from .combinational_cut import semantic_contract_sha256

        exact_contract_sha256 = semantic_contract_sha256(exact_contract)
        if schedule.get("semantic_contract_sha256") != exact_contract_sha256:
            raise ValidationError("logic segment exact contract digest disagrees")
        exact_captures = {
            item["id"]: item
            for item in exact_contract["capture_requirements"]
        }
        for item in exact_contract["logic_segments"]:
            if item["kind"] == "launch_to_tx":
                key = ("launch", None, item["sink_cut_net"], item["fpga"], None)
            elif item["kind"] == "rx_to_tx":
                key = (
                    "transition",
                    item["source_cut_net"],
                    item["sink_cut_net"],
                    item["fpga"],
                    None,
                )
            else:
                capture = exact_captures[item["capture_requirement"]]
                key = (
                    "capture",
                    item["source_cut_net"],
                    None,
                    item["fpga"],
                    capture["endpoint"],
                )
            if key in exact_segment_by_key:
                raise ValidationError(
                    "logic segment exact contract mapping is ambiguous"
                )
            exact_segment_by_key[key] = item["id"]

    incoming_nets_by_instance: Dict[str, List[str]] = defaultdict(list)
    for net in original_ir.value["nets"]:
        for endpoint in net["sinks"]:
            instance = endpoint["instance"]
            if instance is not None:
                incoming_nets_by_instance[instance].append(net["id"])
    return LogicSegmentQueryInputs(
        original_ir=original_ir,
        assignment=assignment,
        path_database=path_database,
        routes=routes,
        schedule=schedule,
        object_index=sta_object_index(original_ir),
        database_paths={item["id"]: item for item in path_database["paths"]},
        route_timing={item["path"]: item for item in routes["timing"]["paths"]},
        route_by_net={item["net"]: item for item in routes["routes"]},
        timing_records=reconstruct_tdm_schedule_timing_paths(
            routes, platform, schedule
        ),
        original_nets={item["id"]: item for item in original_ir.value["nets"]},
        original_instances={
            item["id"]: item for item in original_ir.value["instances"]
        },
        incoming_nets_by_instance=incoming_nets_by_instance,
        exact_contract=exact_contract,
        exact_contract_sha256=exact_contract_sha256,
        exact_segment_by_key=exact_segment_by_key,
        exact_captures=exact_captures,
    )


def _write_logic_segment_query(
    original_ir_path: Path,
    assignment_path: Path,
    path_database_path: Path,
    routes_path: Path,
    schedule_path: Path,
    platform: Platform,
    merged_ir_path: Path,
    boundary_identity_path: Path,
    fpga: str,
    query_path: Path,
    identity_path: Path,
    *,
    object_provider: str,
    eblif_report: Optional[Mapping[str, Any]] = None,
    prepared_inputs: Optional[LogicSegmentQueryInputs] = None,
) -> Dict[str, Any]:
    """Build physical path queries for all exact logical segments on one FPGA."""
    prepared = prepared_inputs or prepare_logic_segment_query_inputs(
        original_ir_path,
        assignment_path,
        path_database_path,
        routes_path,
        schedule_path,
        platform,
    )
    original_ir = prepared.original_ir
    merged_ir = EmuIR.load(merged_ir_path)
    assignment = prepared.assignment
    path_database = prepared.path_database
    routes = prepared.routes
    schedule = prepared.schedule
    boundary_identity = read_json(boundary_identity_path)
    if merged_ir.value["design"]["name"] != f"{assignment['design']}__{fpga}":
        raise ValidationError("logic segment merged IR target is invalid")
    endpoints, _ = _boundary_maps(boundary_identity)
    endpoints_by_schedule_kind = {
        (item["schedule_entry"], item["kind"]): item["id"]
        for item in boundary_identity["endpoints"]
    }
    if len(endpoints_by_schedule_kind) != len(boundary_identity["endpoints"]):
        raise ValidationError(
            "logic segment boundary schedule/kind identities duplicate"
        )
    exact_contract = prepared.exact_contract
    exact_contract_sha256 = prepared.exact_contract_sha256
    exact_segment_by_key = prepared.exact_segment_by_key
    exact_captures = prepared.exact_captures
    object_index = prepared.object_index
    database_paths = prepared.database_paths
    route_timing = prepared.route_timing
    route_by_net = prepared.route_by_net
    records = prepared.timing_records
    instance_index = {
        instance["id"]: index
        for index, instance in enumerate(merged_ir.value["instances"])
    }
    merged_instances = {
        instance["id"]: instance
        for instance in merged_ir.value["instances"]
    }
    merged_net_index = {
        net["id"]: index for index, net in enumerate(merged_ir.value["nets"])
    }
    original_nets = prepared.original_nets
    original_instances = prepared.original_instances
    incoming_nets_by_instance = prepared.incoming_nets_by_instance
    merged_pins = _instance_pin_inventory(merged_ir)
    eblif_top_ports = None
    if object_provider == "vpr" and eblif_report is not None:
        top_port_records = eblif_report.get("top_ports")
        if not isinstance(top_port_records, list):
            raise ValidationError("logic segment eBLIF top-port map is invalid")
        eblif_top_ports = {}
        for top_port_record in top_port_records:
            if not isinstance(top_port_record, Mapping):
                raise ValidationError(
                    "logic segment eBLIF top-port record is invalid"
                )
            identity = (
                top_port_record.get("port"),
                top_port_record.get("bit"),
            )
            if (
                not isinstance(identity[0], str)
                or isinstance(identity[1], bool)
                or not isinstance(identity[1], int)
                or identity in eblif_top_ports
            ):
                raise ValidationError(
                    "logic segment eBLIF top-port map is inconsistent"
                )
            eblif_top_ports[identity] = top_port_record
    if object_provider not in {"vpr", "vivado"}:
        raise ValidationError("logic segment object provider is invalid")

    def endpoint_object(endpoint: Mapping[str, Any]) -> tuple[str, str]:
        if object_provider == "vpr":
            return (
                "pin",
                _vpr_atom_pin(
                    merged_ir,
                    instance_index,
                    endpoint,
                    merged_instances,
                    eblif_top_ports,
                ),
            )
        return _vivado_object(
            merged_ir, endpoint, merged_pins, merged_instances
        )

    def tx_object(endpoint_id: str) -> tuple[str, str]:
        if object_provider == "vpr":
            return (
                "pin",
                _boundary_tx_port(
                    merged_ir,
                    instance_index,
                    endpoints,
                    endpoint_id,
                    merged_net_index,
                    eblif_top_ports,
                ),
            )
        endpoint = endpoints.get(endpoint_id)
        if endpoint is None or endpoint.get("kind") != "tx":
            raise ValidationError(f"logic segment TX {endpoint_id!r} is absent")
        merged = endpoint["merged_ir"]
        return _vivado_object(
            merged_ir,
            {
                "instance": None,
                "port": merged["external_port"],
                "bit": merged["external_port_bit"],
            },
            merged_pins,
            merged_instances,
        )

    def rx_object(endpoint_id: str) -> tuple[str, str]:
        if object_provider == "vpr":
            return (
                "pin",
                _boundary_rx_q(
                    merged_ir,
                    instance_index,
                    endpoints,
                    endpoint_id,
                    merged_instances,
                ),
            )
        endpoint = endpoints.get(endpoint_id)
        if endpoint is None or endpoint.get("kind") != "rx":
            raise ValidationError(f"logic segment RX {endpoint_id!r} is absent")
        registers = endpoint["merged_ir"]["boundary_register_instances"]
        if not isinstance(registers, list) or len(registers) != 1:
            raise ValidationError(
                f"logic segment RX {endpoint_id!r} is ambiguous"
            )
        return _vivado_object(
            merged_ir,
            {"instance": registers[0], "port": "Q", "bit": 0},
            merged_pins,
            merged_instances,
        )

    def cone_anchor_object(net_id: str) -> tuple[str, str]:
        net = original_nets.get(net_id)
        drivers = [] if net is None else net.get("drivers", [])
        if len(drivers) != 1:
            raise ValidationError(
                f"logic segment cut net {net_id!r} has no unique driver"
            )
        return endpoint_object(drivers[0])
    instance_assignment = assignment["instance_assignment"]
    segments = []
    exact_members = 0
    unsupported: Dict[str, str] = {}
    for record in records:
        timing = route_timing[record["path"]]
        members = timing.get("compressed_path_ids", [record["path"]])
        transitions = record["cut_transitions"]
        if len(transitions) != len(record["cut_nets"]):
            raise ValidationError("logic segment transition coverage is invalid")
        discontinuity = any(
            transitions[index - 1]["to"] != transitions[index]["from"]
            for index in range(1, len(transitions))
        )
        hops_by_net = {}
        for net in record["cut_nets"]:
            demand = route_by_net[net]["id"]
            hops = [
                hop for hop in record["scheduled_hops"]
                if hop["demand"] == demand
            ]
            if not hops:
                raise ValidationError(
                    f"logic timing path {record['path']!r} has no hops for {net!r}"
                )
            hops_by_net[net] = hops
        for member in members:
            path = database_paths.get(member)
            if path is None:
                raise ValidationError(
                    f"logic timing member {member!r} is absent from STA database"
                )
            if discontinuity:
                unsupported[member] = "discontinuous-compressed-partition-chain"
                continue
            try:
                start_endpoint, end_endpoint = sta_path_endpoints(
                    path, object_index
                )
                start_instance = start_endpoint["instance"]
                end_instance = end_endpoint["instance"]
                if (
                    start_instance is not None
                    and instance_assignment.get(start_instance)
                    != transitions[0]["from"]
                ):
                    raise ValidationError("launch endpoint partition mismatch")
                if (
                    end_instance is not None
                    and instance_assignment.get(end_instance)
                    != transitions[-1]["to"]
                ):
                    raise ValidationError("capture endpoint partition mismatch")
            except ValidationError as error:
                unsupported[member] = str(error)
                continue
            exact_members += 1
            prior_rx: Optional[str] = None
            for cut_index, (net, transition) in enumerate(
                zip(record["cut_nets"], transitions)
            ):
                hops = hops_by_net[net]
                first_tx = hops[0]["tx_endpoint"]
                last_rx = hops[-1]["rx_endpoint"]
                segment_fpga = transition["from"]
                role = "launch" if cut_index == 0 else "transition"
                if segment_fpga == fpga:
                    start_kind, start_pin = (
                        endpoint_object(start_endpoint)
                        if cut_index == 0
                        else rx_object(prior_rx)
                    )
                    end_kind, end_pin = tx_object(first_tx)
                    segment = {
                        "id": _segment_id(member, cut_index, role),
                        "kind": role,
                        "system_path": record["path"],
                        "member_path": member,
                        "cut_index": cut_index,
                        "fpga": fpga,
                        "replace_tx_endpoint": first_tx,
                        "start_pin": start_pin,
                        "end_pin": end_pin,
                    }
                    if exact_contract_sha256 is not None:
                        exact_key = (
                            role,
                            (
                                None
                                if cut_index == 0
                                else record["cut_nets"][cut_index - 1]
                            ),
                            net,
                            fpga,
                            None,
                        )
                        exact_segment_id = exact_segment_by_key.get(exact_key)
                        if exact_segment_id is None:
                            raise ValidationError(
                                "physical logic segment is absent from the "
                                "static exact semantic contract"
                            )
                        segment["static_exact_segment_id"] = exact_segment_id
                    if object_provider == "vivado":
                        anchor_kind, anchor_pin = cone_anchor_object(net)
                        segment.update(
                            {
                                "start_object_kind": start_kind,
                                "end_object_kind": end_kind,
                                "cone_anchor_object_kind": anchor_kind,
                                "cone_anchor_pin": anchor_pin,
                            }
                        )
                    segments.append(segment)
                prior_rx = last_rx
            capture_fpga = transitions[-1]["to"]
            if capture_fpga == fpga:
                start_kind, start_pin = rx_object(prior_rx)
                end_kind, end_pin = endpoint_object(end_endpoint)
                segment = {
                    "id": _segment_id(
                        member, len(transitions), "capture"
                    ),
                    "kind": "capture",
                    "system_path": record["path"],
                    "member_path": member,
                    "cut_index": len(transitions),
                    "fpga": fpga,
                    "replace_tx_endpoint": None,
                    "start_pin": start_pin,
                    "end_pin": end_pin,
                }
                if exact_contract_sha256 is not None:
                    capture_endpoint_id = (
                        end_instance
                        if end_instance is not None
                        else (
                            f"top:{end_endpoint['port']}"
                            f"[{end_endpoint['bit']}]"
                        )
                    )
                    exact_key = (
                        "capture",
                        record["cut_nets"][-1],
                        None,
                        fpga,
                        capture_endpoint_id,
                    )
                    exact_segment_id = exact_segment_by_key.get(exact_key)
                    if exact_segment_id is None:
                        raise ValidationError(
                            "physical capture segment is absent from the "
                            "static exact semantic contract"
                        )
                    segment["static_exact_segment_id"] = exact_segment_id
                if object_provider == "vivado":
                    segment.update(
                        {
                            "start_object_kind": start_kind,
                            "end_object_kind": end_kind,
                        }
                    )
                segments.append(segment)
    if exact_contract_sha256 is not None:
        schedule_entries = list(schedule["entries"])
        incoming_cut_nets = _incoming_transported_cut_nets(
            exact_contract, fpga
        )
        entries_by_net_from: Dict[
            Tuple[str, str], List[Mapping[str, Any]]
        ] = defaultdict(list)
        entries_by_net_to: Dict[
            Tuple[str, str], List[Mapping[str, Any]]
        ] = defaultdict(list)
        for entry in schedule_entries:
            entries_by_net_from[(entry["net"], entry["from"])].append(entry)
            entries_by_net_to[(entry["net"], entry["to"])].append(entry)
        for entries_for_endpoint in entries_by_net_from.values():
            entries_for_endpoint.sort(key=lambda item: item["id"])
        launches_by_sink_net: Dict[str, List[Mapping[str, Any]]] = {}

        def entry_endpoint(
            entry: Mapping[str, Any], kind: str
        ) -> str:
            endpoint_id = endpoints_by_schedule_kind.get((entry["id"], kind))
            if endpoint_id is None:
                raise ValidationError(
                    f"static exact schedule entry {entry['id']!r} lacks "
                    f"one {kind.upper()} physical boundary"
                )
            return endpoint_id

        def tx_entries(net: str, source_fpga: str) -> List[Mapping[str, Any]]:
            result = entries_by_net_from.get((net, source_fpga), [])
            if not result:
                raise ValidationError(
                    f"static exact cut {net!r} has no TX from {source_fpga!r}"
                )
            return result

        def rx_entry(net: str, sink_fpga: str) -> Mapping[str, Any]:
            result = entries_by_net_to.get((net, sink_fpga), [])
            if len(result) != 1:
                raise ValidationError(
                    f"static exact cut {net!r} does not have one arrival "
                    f"at {sink_fpga!r}"
                )
            return result[0]

        def capture_endpoint(capture: Mapping[str, Any]) -> Mapping[str, Any]:
            endpoint = capture["endpoint"]
            if capture["kind"] == "top-output":
                match = re.fullmatch(r"top:(.+)\[(\d+)\]", endpoint)
                if match is None:
                    raise ValidationError(
                        "static exact top-output capture identity is invalid"
                    )
                return {
                    "instance": None,
                    "port": match.group(1),
                    "bit": int(match.group(2)),
                }
            instance = merged_instances.get(endpoint)
            if (
                capture["kind"] != "architectural-state"
                or instance is None
                or not any(
                    instance.get("resources", {}).get(field, 0)
                    for field in _STATE_RESOURCES
                )
            ):
                raise ValidationError(
                    f"static exact capture endpoint {endpoint!r} is unsupported"
                )
            port = capture.get("port")
            bit = capture.get("bit")
            if (
                not isinstance(port, str)
                or isinstance(bit, bool)
                or not isinstance(bit, int)
                or bit < 0
            ):
                raise ValidationError(
                    f"static exact capture endpoint {endpoint!r} does not "
                    "preserve its reached pin"
                )
            return {"instance": endpoint, "port": port, "bit": bit}

        existing = {
            (
                item.get("static_exact_segment_id"),
                item["start_pin"],
                item["end_pin"],
            )
            for item in segments
        }

        def add_contract_segment(
            semantic: Mapping[str, Any],
            role: str,
            start: Union[Mapping[str, Any], str],
            end_tx: Optional[str],
            ordinal: int,
        ) -> None:
            start_kind, start_pin = (
                rx_object(start) if isinstance(start, str)
                else endpoint_object(start)
            )
            if end_tx is None:
                capture = exact_captures[semantic["capture_requirement"]]
                end_kind, end_pin = endpoint_object(
                    capture_endpoint(capture)
                )
            else:
                end_kind, end_pin = tx_object(end_tx)
            dedup = (semantic["id"], start_pin, end_pin)
            if dedup in existing:
                return
            existing.add(dedup)
            identity = hashlib.sha256(
                "\0".join(
                    (semantic["id"], start_pin, end_pin, str(ordinal))
                ).encode("utf-8")
            ).hexdigest()[:24]
            segment = {
                "id": f"static_exact_{identity}_{role}",
                "kind": role,
                "system_path": f"static-exact-contract:{semantic['id']}",
                "member_path": (
                    f"static-exact-contract:{semantic['id']}:{identity}"
                ),
                "cut_index": ordinal,
                "fpga": fpga,
                "replace_tx_endpoint": end_tx,
                "start_pin": start_pin,
                "end_pin": end_pin,
                "static_exact_segment_id": semantic["id"],
            }
            if object_provider == "vivado":
                segment.update(
                    {
                        "start_object_kind": start_kind,
                        "end_object_kind": end_kind,
                    }
                )
                if end_tx is not None:
                    anchor_kind, anchor_pin = cone_anchor_object(
                        semantic["sink_cut_net"]
                    )
                    segment.update(
                        {
                            "cone_anchor_object_kind": anchor_kind,
                            "cone_anchor_pin": anchor_pin,
                        }
                    )
            segments.append(segment)

        for semantic in exact_contract["logic_segments"]:
            if semantic["fpga"] != fpga:
                continue
            kind = semantic["kind"]
            if kind == "launch_to_tx":
                sink_cut_net = semantic["sink_cut_net"]
                starts = launches_by_sink_net.get(sink_cut_net)
                if starts is None:
                    starts = _architectural_launch_endpoints(
                        original_ir,
                        instance_assignment,
                        sink_cut_net,
                        fpga,
                        incoming_cut_nets,
                        nets=original_nets,
                        instances=original_instances,
                        incoming=incoming_nets_by_instance,
                    )
                    launches_by_sink_net[sink_cut_net] = starts
                if not starts:
                    # A dependency-free constant cone has no dynamic launch
                    # endpoint.  Its sealed semantic-contract record qualifies
                    # configuration stability independently; do not invent a
                    # physical startpoint merely to create a timing query.
                    continue
                destinations = [
                    entry_endpoint(item, "tx")
                    for item in tx_entries(semantic["sink_cut_net"], fpga)
                ]
                for ordinal, (start, destination) in enumerate(
                    (start, destination)
                    for start in starts
                    for destination in destinations
                ):
                    add_contract_segment(
                        semantic, "launch", start, destination, ordinal
                    )
            elif kind == "rx_to_tx":
                source = entry_endpoint(
                    rx_entry(semantic["source_cut_net"], fpga), "rx"
                )
                for ordinal, destination in enumerate(
                    entry_endpoint(item, "tx")
                    for item in tx_entries(semantic["sink_cut_net"], fpga)
                ):
                    add_contract_segment(
                        semantic,
                        "transition",
                        source,
                        destination,
                        ordinal,
                    )
            else:
                source = entry_endpoint(
                    rx_entry(semantic["source_cut_net"], fpga), "rx"
                )
                add_contract_segment(
                    semantic, "capture", source, None, 0
                )

    segments.sort(key=lambda item: item["id"])
    identity = {
        "schema": LOGIC_SEGMENT_IDENTITY_SCHEMA,
        "status": "pass",
        "design": assignment["design"],
        "platform": platform.name,
        "fpga": fpga,
        "provider": f"sta-endpoint-chain-to-{object_provider}-objects-v1",
        "coverage": {
            "segments": len(segments),
            "system_paths": len({item["system_path"] for item in segments}),
            "member_paths": len({item["member_path"] for item in segments}),
            "unsupported_member_paths": len(unsupported),
        },
        "unsupported_member_paths": [
            {"path": member, "reason": reason}
            for member, reason in sorted(unsupported.items())
        ],
        "segments": segments,
        **(
            {"semantic_contract_sha256": exact_contract_sha256}
            if exact_contract_sha256 is not None
            else {}
        ),
    }
    validate_logic_segment_identity(identity)
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(identity_path, identity)
    query_path.parent.mkdir(parents=True, exist_ok=True)
    if object_provider == "vpr":
        rows = [
            LOGIC_SEGMENT_QUERY_HEADER,
            *(
                "\t".join(
                    (
                        item["id"],
                        item["kind"],
                        item["start_pin"],
                        item["end_pin"],
                    )
                )
                for item in segments
            ),
        ]
    else:
        rows = [
            VIVADO_LOGIC_SEGMENT_QUERY_HEADER,
            *(
                "\t".join(
                    (
                        item["id"].encode("utf-8").hex(),
                        item["kind"],
                        item["start_object_kind"],
                        item["start_pin"].encode("utf-8").hex(),
                        item["end_object_kind"],
                        item["end_pin"].encode("utf-8").hex(),
                        item.get("cone_anchor_object_kind", ""),
                        item.get("cone_anchor_pin", "").encode("utf-8").hex(),
                    )
                )
                for item in segments
            ),
        ]
    query_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return {
        "status": "pass",
        "fpga": fpga,
        "segments": len(segments),
        "exact_member_paths": exact_members,
        "unsupported_member_paths": len(unsupported),
        "query": str(query_path),
        "identity": str(identity_path),
    }


def write_vpr_logic_segment_query(
    original_ir_path: Path,
    assignment_path: Path,
    path_database_path: Path,
    routes_path: Path,
    schedule_path: Path,
    platform: Platform,
    merged_ir_path: Path,
    boundary_identity_path: Path,
    fpga: str,
    query_path: Path,
    identity_path: Path,
    *,
    eblif_report: Optional[Mapping[str, Any]] = None,
    prepared_inputs: Optional[LogicSegmentQueryInputs] = None,
) -> Dict[str, Any]:
    return _write_logic_segment_query(
        original_ir_path,
        assignment_path,
        path_database_path,
        routes_path,
        schedule_path,
        platform,
        merged_ir_path,
        boundary_identity_path,
        fpga,
        query_path,
        identity_path,
        object_provider="vpr",
        eblif_report=eblif_report,
        prepared_inputs=prepared_inputs,
    )


def write_vivado_logic_segment_query(
    original_ir_path: Path,
    assignment_path: Path,
    path_database_path: Path,
    routes_path: Path,
    schedule_path: Path,
    platform: Platform,
    merged_ir_path: Path,
    boundary_identity_path: Path,
    fpga: str,
    query_path: Path,
    identity_path: Path,
    *,
    prepared_inputs: Optional[LogicSegmentQueryInputs] = None,
) -> Dict[str, Any]:
    return _write_logic_segment_query(
        original_ir_path,
        assignment_path,
        path_database_path,
        routes_path,
        schedule_path,
        platform,
        merged_ir_path,
        boundary_identity_path,
        fpga,
        query_path,
        identity_path,
        object_provider="vivado",
        prepared_inputs=prepared_inputs,
    )


def validate_logic_segment_identity(
    database: Mapping[str, Any],
) -> Dict[str, Any]:
    if database.get("schema") != LOGIC_SEGMENT_IDENTITY_SCHEMA:
        raise ValidationError("logic segment identity schema is invalid")
    if database.get("status") != "pass":
        raise ValidationError("logic segment identity did not pass")
    fpga = database.get("fpga")
    if not isinstance(fpga, str) or not fpga:
        raise ValidationError("logic segment identity FPGA is invalid")
    exact_digest = database.get("semantic_contract_sha256")
    if exact_digest is not None and (
        not isinstance(exact_digest, str)
        or len(exact_digest) != 64
        or any(character not in "0123456789abcdef" for character in exact_digest)
    ):
        raise ValidationError("logic segment exact contract digest is invalid")
    ids = set()
    members = set()
    paths = set()
    for index, segment in enumerate(database.get("segments", [])):
        context = f"logic segment identity[{index}]"
        if not isinstance(segment, dict):
            raise ValidationError(f"{context} is invalid")
        segment_id = segment.get("id")
        if (
            not isinstance(segment_id, str)
            or not segment_id
            or segment_id in ids
            or segment.get("kind") not in {"launch", "transition", "capture"}
            or segment.get("fpga") != fpga
        ):
            raise ValidationError(f"{context} identity is invalid")
        ids.add(segment_id)
        exact_segment_id = segment.get("static_exact_segment_id")
        if exact_digest is not None and (
            not isinstance(exact_segment_id, str) or not exact_segment_id
        ):
            raise ValidationError(
                f"{context}.static_exact_segment_id is invalid"
            )
        if exact_digest is None and exact_segment_id is not None:
            raise ValidationError(
                f"{context} has an unbound static exact segment identity"
            )
        for field in ("system_path", "member_path", "start_pin", "end_pin"):
            if not isinstance(segment.get(field), str) or not segment[field]:
                raise ValidationError(f"{context}.{field} is invalid")
        object_kind_fields = {"start_object_kind", "end_object_kind"}
        present_object_kinds = object_kind_fields & set(segment)
        if present_object_kinds and (
            present_object_kinds != object_kind_fields
            or segment["start_object_kind"] not in {"pin", "port"}
            or segment["end_object_kind"] not in {"pin", "port"}
        ):
            raise ValidationError(f"{context} object kinds are invalid")
        anchor_fields = {
            "cone_anchor_object_kind",
            "cone_anchor_pin",
        }
        present_anchor_fields = anchor_fields & set(segment)
        if present_anchor_fields and (
            present_anchor_fields != anchor_fields
            or segment["kind"] == "capture"
            or segment["cone_anchor_object_kind"] not in {"pin", "port"}
            or not isinstance(segment["cone_anchor_pin"], str)
            or not segment["cone_anchor_pin"]
        ):
            raise ValidationError(f"{context} cone anchor is invalid")
        replacement = segment.get("replace_tx_endpoint")
        if (segment["kind"] == "capture") != (replacement is None):
            raise ValidationError(f"{context} replacement is invalid")
        members.add(segment["member_path"])
        paths.add(segment["system_path"])
    return {
        "status": "pass",
        "segments": len(ids),
        "member_paths": len(members),
        "system_paths": len(paths),
    }


def import_vpr_logic_segment_timing(
    input_path: Path,
    identity_path: Path,
    output_path: Path,
) -> Dict[str, Any]:
    identity = read_json(identity_path)
    validate_logic_segment_identity(identity)
    expected = {item["id"]: item for item in identity["segments"]}
    lines = input_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != LOGIC_SEGMENT_TIMING_HEADER:
        raise ValidationError("VPR logic segment timing header is invalid")
    measurements = {}
    for line_number, line in enumerate(lines[1:], start=2):
        if not line:
            continue
        fields = line.split("\t")
        segment = expected.get(fields[0]) if len(fields) == 5 else None
        if (
            segment is None
            or fields[0] in measurements
            or fields[1] != segment["kind"]
            or fields[3] != segment["start_pin"]
            or fields[4] != segment["end_pin"]
        ):
            raise ValidationError(
                f"VPR logic segment timing line {line_number} is invalid"
            )
        try:
            delay = float(fields[2])
        except ValueError as error:
            raise ValidationError(
                f"VPR logic segment timing line {line_number} delay is invalid"
            ) from error
        if not math.isfinite(delay) or delay < 0.0:
            raise ValidationError(
                f"VPR logic segment timing line {line_number} delay is invalid"
            )
        measurements[fields[0]] = delay
    if set(measurements) != set(expected):
        missing = sorted(set(expected) - set(measurements))
        raise ValidationError(
            f"VPR logic segment timing coverage is incomplete: {missing[:10]}"
        )
    records = [
        {
            **segment,
            "delay_ns": measurements[segment["id"]],
        }
        for segment in identity["segments"]
    ]
    database = {
        "schema": LOGIC_SEGMENT_TIMING_SCHEMA,
        "status": "pass",
        "design": identity["design"],
        "platform": identity["platform"],
        "fpga": identity["fpga"],
        "provider": "vpr-tatum-logic-segment-longest-path-v1",
        "qualification": "routed-academic-architecture-endpoint-chain",
        "coverage": identity["coverage"],
        "unsupported_member_paths": identity["unsupported_member_paths"],
        "segments": records,
        **(
            {
                "semantic_contract_sha256": identity[
                    "semantic_contract_sha256"
                ]
            }
            if "semantic_contract_sha256" in identity
            else {}
        ),
    }
    validation = validate_logic_segment_timing(database)
    write_json(output_path, database)
    return {**validation, "output": str(output_path)}


def import_vivado_logic_segment_timing(
    input_path: Path,
    identity_path: Path,
    output_path: Path,
    *,
    provider: str = "vivado-routed-logic-segment-datapath-v1",
    qualification: str = "routed-vendor-device-endpoint-chain",
    allow_missing: bool = False,
) -> Dict[str, Any]:
    identity = read_json(identity_path)
    validate_logic_segment_identity(identity)
    expected = {item["id"]: item for item in identity["segments"]}
    lines = input_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] not in {
        VIVADO_LOGIC_SEGMENT_TIMING_HEADER,
        _LEGACY_VIVADO_LOGIC_SEGMENT_TIMING_HEADER,
    }:
        raise ValidationError("Vivado logic segment timing header is invalid")
    extended_format = lines[0] == VIVADO_LOGIC_SEGMENT_TIMING_HEADER
    measurements = {}
    for line_number, line in enumerate(lines[1:], start=2):
        if not line:
            continue
        fields = line.split("\t")
        try:
            segment_id = bytes.fromhex(fields[0]).decode("utf-8")
            start_object = bytes.fromhex(fields[3]).decode("utf-8")
            end_object = bytes.fromhex(fields[4]).decode("utf-8")
            measurement = fields[5] if extended_format else "endpoint-exact"
            actual_start = (
                bytes.fromhex(fields[6]).decode("utf-8")
                if extended_format
                else start_object
            )
            actual_end = (
                bytes.fromhex(fields[7]).decode("utf-8")
                if extended_format
                else end_object
            )
        except (IndexError, ValueError, UnicodeDecodeError) as error:
            raise ValidationError(
                f"Vivado logic segment timing line {line_number} is invalid"
            ) from error
        expected_fields = 8 if extended_format else 5
        segment = (
            expected.get(segment_id)
            if len(fields) == expected_fields
            else None
        )
        if (
            segment is None
            or segment_id in measurements
            or fields[1] != segment["kind"]
            or start_object != segment["start_pin"]
            or end_object != segment["end_pin"]
            or measurement not in {
                "endpoint-exact",
                "cut-net-cone-upper-bound",
            }
            or not actual_start
            or not actual_end
        ):
            raise ValidationError(
                f"Vivado logic segment timing line {line_number} is invalid"
            )
        try:
            delay = float(fields[2])
        except ValueError as error:
            raise ValidationError(
                f"Vivado logic segment timing line {line_number} delay is invalid"
            ) from error
        if not math.isfinite(delay) or delay < 0.0:
            raise ValidationError(
                f"Vivado logic segment timing line {line_number} delay is invalid"
            )
        measurements[segment_id] = {
            "delay_ns": delay,
            "measurement": measurement,
            "actual_start_object": actual_start,
            "actual_end_object": actual_end,
        }
    missing = sorted(set(expected) - set(measurements))
    if missing and not allow_missing:
        raise ValidationError(
            "Vivado logic segment timing coverage is incomplete: "
            f"{missing[:10]}"
        )
    measured_segments = [
        segment
        for segment in identity["segments"]
        if segment["id"] in measurements
    ]
    unsupported = {
        item["path"]: item["reason"]
        for item in identity["unsupported_member_paths"]
    }
    for segment_id in missing:
        segment = expected[segment_id]
        unsupported[segment["member_path"]] = (
            "no-routed-vivado-board-path"
        )
    coverage = {
        "segments": len(measured_segments),
        "system_paths": len(
            {item["system_path"] for item in measured_segments}
        ),
        "member_paths": len(
            {item["member_path"] for item in measured_segments}
        ),
        "unsupported_member_paths": len(unsupported),
        "endpoint_exact_segments": sum(
            measurements[item["id"]]["measurement"] == "endpoint-exact"
            for item in measured_segments
        ),
        "cone_bound_segments": sum(
            measurements[item["id"]]["measurement"]
            == "cut-net-cone-upper-bound"
            for item in measured_segments
        ),
    }
    database = {
        "schema": LOGIC_SEGMENT_TIMING_SCHEMA,
        "status": "pass",
        "design": identity["design"],
        "platform": identity["platform"],
        "fpga": identity["fpga"],
        "provider": provider,
        "qualification": qualification,
        "coverage": coverage,
        "unsupported_member_paths": [
            {"path": path, "reason": reason}
            for path, reason in sorted(unsupported.items())
        ],
        "unmeasured_segments": [
            {
                "id": segment_id,
                "kind": expected[segment_id]["kind"],
                "system_path": expected[segment_id]["system_path"],
                "member_path": expected[segment_id]["member_path"],
                "reason": "no-routed-vivado-board-path",
            }
            for segment_id in missing
        ],
        "segments": [
            {**segment, **measurements[segment["id"]]}
            for segment in measured_segments
        ],
        **(
            {
                "semantic_contract_sha256": identity[
                    "semantic_contract_sha256"
                ]
            }
            if "semantic_contract_sha256" in identity
            else {}
        ),
    }
    validation = validate_logic_segment_timing(database)
    write_json(output_path, database)
    return {
        **validation,
        "missing_segments": len(missing),
        "output": str(output_path),
    }


def validate_logic_segment_timing(
    database: Mapping[str, Any],
) -> Dict[str, Any]:
    if database.get("schema") != LOGIC_SEGMENT_TIMING_SCHEMA:
        raise ValidationError("logic segment timing schema is invalid")
    if database.get("status") != "pass":
        raise ValidationError("logic segment timing did not pass")
    identity = {
        key: database[key]
        for key in (
            "status",
            "design",
            "platform",
            "fpga",
            "coverage",
            "unsupported_member_paths",
            "segments",
        )
    }
    if "semantic_contract_sha256" in database:
        identity["semantic_contract_sha256"] = database[
            "semantic_contract_sha256"
        ]
    identity["schema"] = LOGIC_SEGMENT_IDENTITY_SCHEMA
    identity["provider"] = "timing-validation"
    validate_logic_segment_identity(identity)
    maximum = 0.0
    endpoint_exact = 0
    cone_bound = 0
    for segment in database["segments"]:
        delay = segment.get("delay_ns")
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(float(delay))
            or float(delay) < 0.0
        ):
            raise ValidationError("logic segment timing delay is invalid")
        measurement = segment.get("measurement", "endpoint-exact")
        if measurement not in {
            "endpoint-exact",
            "cut-net-cone-upper-bound",
        }:
            raise ValidationError("logic segment timing measurement is invalid")
        if measurement == "cut-net-cone-upper-bound":
            if segment.get("kind") == "capture" or not isinstance(
                segment.get("cone_anchor_pin"), str
            ):
                raise ValidationError(
                    "logic segment cone-bound trace is invalid"
                )
            cone_bound += 1
        else:
            endpoint_exact += 1
        for field in ("actual_start_object", "actual_end_object"):
            if field in segment and (
                not isinstance(segment[field], str) or not segment[field]
            ):
                raise ValidationError(
                    "logic segment physical object trace is invalid"
                )
        maximum = max(maximum, float(delay))
    return {
        "status": "pass",
        "fpga": database["fpga"],
        "segments": len(database["segments"]),
        "endpoint_exact_segments": endpoint_exact,
        "cone_bound_segments": cone_bound,
        "maximum_delay_ns": maximum,
    }
