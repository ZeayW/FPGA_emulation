"""Endpoint-exact physical timing for DUT logic between TDM boundaries."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import uuid
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple, Union

from .sqlite_artifacts import immutable_sqlite_uri
from .boundary_timing import BOUNDARY_IDENTITY_SCHEMA
from .equivalence import _lut_definition
from .errors import ValidationError
from .timing_disposition import OPTIMIZED_AWAY, is_optimized_away, validate_optimization_record, require_optimization_proof
from .store_publication import bind_view_sources, bind_store_source
from .io import read_json, write_json
from .ir import EmuIR
from .emuir_compact import is_compact_emuir, selected_net_records
from .emuir_storage import is_emuir_sqlite, open_emuir_sqlite_read_only
from .indexed_physical_ir import IndexedPhysicalIR
from .indexed_timing import IndexedTimingView
from .partition import PARTITION_ASSIGNMENT_SCHEMA
from .phase3_assignment_index import read_phase3_exact_semantic_partition
from .phase3_assignment_index import open_phase3_assignment_index
from .phase3_exact_binary import (
    iter_phase3_exact_captures,
    iter_phase3_exact_segments,
)
from .phase3_storage import read_phase3_assignment_view
from .vivado_backend import _vivado_mapped_name
from .platform import Platform
from .routing import static_exact_contract_from_assignment, system_route_timing_paths
from .system_route_storage import pretty_system_routes_sha256, read_system_routes
from .sta import (
    STA_PATH_DATABASE_SCHEMA,
    STA_PATH_DATABASE_INDEX_SCHEMA,
    STA_PATH_DATABASE_STREAM_SCHEMA,
    iter_selected_sta_path_database_paths,
    sta_object_index,
    sta_path_endpoints,
)
from .tdm import reconstruct_tdm_schedule_timing_paths
from .tdm_schedule_storage import (
    TDMScheduleView,
    canonical_tdm_schedule_sha256,
    read_tdm_schedule,
)


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
_VIVADO_FF_CONTROLS = {"FDRE": {"CE", "R"}, "FDSE": {"CE", "S"},
                       "FDCE": {"CE", "CLR"}, "FDPE": {"CE", "PRE"}}
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


def _incoming_assignment_cut_nets(
    assignment: Mapping[str, Any], fpga: str
) -> Set[str]:
    cuts = assignment.get("cut_nets")
    if not isinstance(cuts, list):
        raise ValidationError("static exact assignment cut nets are invalid")
    result = set()
    for cut in cuts:
        if (
            not isinstance(cut, Mapping)
            or not isinstance(cut.get("net"), str)
            or not isinstance(cut.get("sink_fpgas"), list)
        ):
            raise ValidationError("static exact assignment cut net is invalid")
        if fpga in cut["sink_fpgas"]:
            result.add(cut["net"])
    return result


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


def _instance_pin_inventory(ir: EmuIR) -> Mapping[str, set[tuple[str, int]]]:
    if isinstance(ir, IndexedPhysicalIR):
        return ir.pins
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


@lru_cache(maxsize=1)
def _vivado_native_cell_types() -> frozenset[str]:
    from .xilinx_primitives import load_xilinx_primitive_library

    _library, contract = load_xilinx_primitive_library()
    return frozenset(contract["cells"])


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
    endpoint = _normalize_xilinx_logical_endpoint(endpoint, pins, instances)
    port, bit = endpoint["port"], endpoint["bit"]
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
        if port == "O" or (cell_type == "LUT6_2" and port in {"O5", "O6"}):
            physical_port, physical_bit = port, 0
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
        if (port not in {"C", "D", "Q"} and port not in _VIVADO_FF_CONTROLS.get(cell_type, ())) or bit != 0:
            raise ValidationError(
                f"unsupported Vivado FF pin {instance_id}.{port}[{bit}]"
            )
    else:
        if cell_type not in _vivado_native_cell_types():
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


def _xilinx_object(
    ir: EmuIR,
    endpoint: Mapping[str, Any],
    pins: Mapping[str, set[tuple[str, int]]],
    instances: Mapping[str, Mapping[str, Any]],
) -> tuple[str, str]:
    """Return a provider-independent Xilinx logical pin identity."""
    endpoint = _normalize_xilinx_logical_endpoint(endpoint, pins, instances)
    instance_id = endpoint["instance"]
    port = endpoint["port"]
    bit = endpoint["bit"]
    if instance_id is None:
        ports = {item["id"]: item for item in ir.value["ports"]}
        if port not in ports or bit >= ports[port]["width"]:
            raise ValidationError(f"Xilinx top endpoint {port}[{bit}] is absent")
        suffix = port if ports[port]["width"] == 1 else f"{port}[{bit}]"
        return "port", f"top:{suffix}"
    if instance_id not in instances or (port, bit) not in pins[instance_id]:
        raise ValidationError(
            f"Xilinx logical endpoint {instance_id}.{port}[{bit}] is absent"
        )
    instance = instances[instance_id]
    cell_type = instance["type"]
    if cell_type in {"$lut", "$_LUT_"}:
        _width, input_port, output_port, _truth = _lut_definition(instance)
        if port == input_port:
            port, bit = f"I{bit}", 0
        elif port == output_port:
            port, bit = "O", 0
        else:
            raise ValidationError(
                f"Xilinx LUT endpoint {instance_id}.{port}[{bit}] is invalid"
            )
        pins = {
            **pins,
            instance_id: {
                *((f"I{index}", 0) for index in range(_width)),
                ("O", 0),
            },
        }
    width = max(
        (candidate_bit + 1 for candidate_port, candidate_bit in pins[instance_id]
         if candidate_port == port),
        default=0,
    )
    suffix = port if width == 1 else f"{port}[{bit}]"
    return "pin", f"cell:{instance_id}/{suffix}"


def _normalize_xilinx_logical_endpoint(
    endpoint: Mapping[str, Any],
    pins: Mapping[str, set[tuple[str, int]]],
    instances: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    """Map physical-width RAMB18 aliases onto the logical Unisim port.

    The pre-placement timing model can name the four RAMB18E2 BEL write-enable
    pins.  The logical RAMB18E2 primitive exposes only WEA[1:0], with physical
    pins 2/3 duplicating logical bits 0/1.  Apply that bridge only when the
    physical-width endpoint is absent and its logical alias exists; an
    independently represented bit therefore remains distinct.
    """

    instance_id = endpoint.get("instance")
    if not isinstance(instance_id, str):
        return endpoint
    instance = instances.get(instance_id)
    if (
        instance is None
        or instance.get("type") != "RAMB18E2"
        or endpoint.get("port") != "WEA"
        or endpoint.get("bit") not in {2, 3}
    ):
        return endpoint
    instance_pins = pins.get(instance_id, set())
    physical = ("WEA", endpoint["bit"])
    logical = ("WEA", endpoint["bit"] % 2)
    if physical in instance_pins or logical not in instance_pins:
        return endpoint
    return {**endpoint, "bit": endpoint["bit"] % 2}


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


def _segment_id(member: str, cut_index: int, role: str) -> str:
    digest = hashlib.sha256(
        f"{member}\0{cut_index}\0{role}".encode("utf-8")
    ).hexdigest()[:24]
    return f"logic_{digest}_{cut_index}_{role}"


@dataclass(frozen=True)
class LogicSegmentQueryInputs:
    """Immutable global timing state shared by all per-FPGA workers."""

    original_ir: EmuIR | None
    assignment: Mapping[str, Any]
    path_database: Mapping[str, Any]
    routes: Mapping[str, Any]
    schedule: Mapping[str, Any]
    object_index: Mapping[str, Any] | None
    path_endpoints: Mapping[
        str, tuple[Mapping[str, Any], Mapping[str, Any]]
    ]
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
    exact_launches: Mapping[
        tuple[str, str], List[Mapping[str, Any]]
    ]
    exact_index_path: Optional[Path]


def _route_timing_paths(
    routes: Mapping[str, Any],
) -> List[Mapping[str, Any]]:
    """Return Phase 4 timing records, allowing a canonical zero-route run."""

    return system_route_timing_paths(
        routes,
        context="logic segment routes",
    )


def _exact_capture_identity(capture: Mapping[str, Any]) -> tuple[Any, ...]:
    """Return the pin-exact identity used to bind a capture segment.

    Architectural state macros such as RAMB18E2 expose multi-bit control and
    data ports.  The instance name alone is therefore not a capture identity:
    two bits of ``WEBWE`` are distinct physical timing endpoints even when
    they belong to the same memory instance.
    """

    endpoint = capture.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        raise ValidationError("static exact capture endpoint is invalid")
    if capture.get("kind") == "architectural-state":
        port = capture.get("port")
        bit = capture.get("bit")
        if (
            not isinstance(port, str)
            or not port
            or isinstance(bit, bool)
            or not isinstance(bit, int)
            or bit < 0
        ):
            raise ValidationError(
                "static exact architectural capture pin is invalid"
            )
        return endpoint, port, bit
    return endpoint, None, None


def _index_exact_logic_segments(
    exact_contract: Mapping[str, Any],
) -> tuple[Dict[Any, str], Dict[str, Mapping[str, Any]]]:
    captures = {
        item["id"]: item for item in exact_contract["capture_requirements"]
    }
    segment_by_key: Dict[Any, str] = {}
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
        elif item["kind"] == "rx_to_capture":
            capture = captures[item["capture_requirement"]]
            key = (
                "capture",
                item["source_cut_net"],
                None,
                item["fpga"],
                _exact_capture_identity(capture),
            )
        else:
            raise ValidationError(
                "static exact logic segment kind is invalid"
            )
        if key in segment_by_key:
            raise ValidationError(
                "logic segment exact contract mapping is ambiguous"
            )
        segment_by_key[key] = item["id"]
    return segment_by_key, captures


def _stream_indexed_exact_partition(
    path: Path, fpga: str, *, allow_legacy: bool
) -> tuple[Mapping[str, Any], Any, Any]:
    """Open the v3 typed semantic partition without list materialization."""
    connection = open_phase3_assignment_index(path)
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        metadata = {
            str(row[0]): json.loads(str(row[1]))
            for row in connection.execute("SELECT key,value FROM metadata")
        }
        if version == 3:
            descriptor = metadata.get("semantic_contract_binary")
            if not isinstance(descriptor, Mapping):
                raise ValidationError(
                    "assignment index lacks its Static Exact binary descriptor"
                )
            artifact = descriptor.get("artifact")
            if not isinstance(artifact, str) or Path(artifact).name != artifact:
                raise ValidationError("Static Exact binary artifact name is invalid")
            binary_path = path.with_name(artifact)
            header = {
                "schema": metadata.get("semantic_contract_schema"),
                "sha256": metadata.get("semantic_contract_sha256"),
                "fpga": fpga,
            }
            return (
                header,
                iter_phase3_exact_segments(binary_path, descriptor, fpga),
                iter_phase3_exact_captures(binary_path, descriptor, fpga),
            )
    finally:
        connection.close()
    if not allow_legacy:
        raise ValidationError(
            "legacy Static Exact partition requires explicit compatibility"
        )
    value = read_phase3_exact_semantic_partition(path, fpga)
    return value, iter(value["logic_segments"]), iter(value["capture_requirements"])


def _indexed_architectural_launch_endpoints(
    connection: Any,
    instance_assignment: Mapping[str, str],
    sink_net: str,
    fpga: str,
    incoming_cut_nets: Set[str],
    net_cache: Dict[str, Mapping[str, Any]],
) -> List[Mapping[str, Any]]:
    """Traverse one exact launch cone without materializing the design."""

    launches: Dict[Tuple[str, str, int], Mapping[str, Any]] = {}
    pending = [sink_net]
    visited: Set[str] = set()
    incoming_cache: Dict[str, List[str]] = {}
    resource_cache: Dict[str, Dict[str, int]] = {}
    while pending:
        net_id = pending.pop()
        if net_id in visited:
            continue
        visited.add(net_id)
        if net_id != sink_net and net_id in incoming_cut_nets:
            continue
        net = net_cache.get(net_id)
        if net is None:
            net = selected_net_records(connection, [net_id])[net_id]
            net_cache[net_id] = net
        for driver in net["drivers"]:
            instance_id = driver["instance"]
            if instance_id is None:
                launches[("", driver["port"], driver["bit"])] = driver
                continue
            if instance_assignment.get(instance_id) != fpga:
                if net_id in incoming_cut_nets:
                    continue
                raise ValidationError(
                    "static exact launch cone crosses an undeclared "
                    f"partition boundary at {net_id!r}"
                )
            resources = resource_cache.get(instance_id)
            if resources is None:
                resources = {
                    row[0]: int(row[1])
                    for row in connection.execute(
                        """SELECT r.resource,r.amount
                        FROM instance_resources r JOIN cell_records c
                        ON c.seq=r.instance_seq WHERE c.id=?""",
                        (instance_id,),
                    )
                }
                if not resources:
                    present = connection.execute(
                        "SELECT 1 FROM cell_records WHERE id=?", (instance_id,)
                    ).fetchone()
                    if present is None:
                        raise ValidationError(
                            f"static exact launch driver {instance_id!r} is absent"
                        )
                resource_cache[instance_id] = resources
            if any(resources.get(field, 0) for field in _STATE_RESOURCES):
                launches[(instance_id, driver["port"], driver["bit"])] = driver
                continue
            incoming = incoming_cache.get(instance_id)
            if incoming is None:
                incoming = [
                    row[0]
                    for row in connection.execute(
                        """SELECT n.id FROM endpoint_records e
                        JOIN cell_records c ON c.seq=e.instance_seq
                        JOIN net_records n ON n.seq=e.net_seq
                        WHERE c.id=? AND e.role=1 ORDER BY n.seq""",
                        (instance_id,),
                    )
                ]
                incoming_cache[instance_id] = incoming
            pending.extend(incoming)
    return [launches[key] for key in sorted(launches)]


def prepare_logic_segment_query_inputs(
    original_ir_path: Path,
    assignment_path: Path,
    path_database_path: Path,
    routes_path: Path,
    schedule_path: Path,
    platform: Platform,
    *,
    allow_legacy_schedule: bool = False,
) -> LogicSegmentQueryInputs:
    """Parse and index global query inputs exactly once per physical run."""
    assignment = read_phase3_assignment_view(
        assignment_path,
        include_instance_assignment=True,
        include_semantic_contract=False,
    )
    path_database = read_json(path_database_path)
    routes = read_system_routes(routes_path)
    schedule = read_tdm_schedule(schedule_path)
    if not isinstance(schedule, TDMScheduleView) and not allow_legacy_schedule:
        raise ValidationError(
            "legacy logic segment schedule requires explicit compatibility"
        )
    if assignment.get("schema") != PARTITION_ASSIGNMENT_SCHEMA:
        raise ValidationError("logic segment assignment schema is invalid")
    if path_database.get("schema") not in {
        STA_PATH_DATABASE_SCHEMA,
        STA_PATH_DATABASE_STREAM_SCHEMA,
        STA_PATH_DATABASE_INDEX_SCHEMA,
    }:
        raise ValidationError("logic segment STA database schema is invalid")

    exact_contract = static_exact_contract_from_assignment(assignment)
    exact_contract_sha256 = None
    exact_segment_by_key: Dict[Any, str] = {}
    exact_captures: Dict[str, Mapping[str, Any]] = {}
    exact_index_path: Optional[Path] = None
    if isinstance(exact_contract, dict) and exact_contract.get("mode") == (
        "static-exact-combinational"
    ):
        if exact_contract.get("indexed") is True:
            exact_contract_sha256 = assignment.get("semantic_contract_sha256")
            exact_index_path = assignment_path.with_name("assignment.sqlite")
            if not exact_index_path.is_file():
                raise ValidationError(
                    "indexed Static Exact timing requires assignment.sqlite"
                )
        else:
            from .combinational_cut import semantic_contract_sha256

            exact_contract_sha256 = semantic_contract_sha256(exact_contract)
        if (
            schedule.get("semantic_contract_schema")
            != exact_contract.get("schema")
            or schedule.get("semantic_contract_sha256")
            != exact_contract_sha256
        ):
            raise ValidationError("logic segment exact contract digest disagrees")
        if exact_index_path is None:
            exact_segment_by_key, exact_captures = _index_exact_logic_segments(
                exact_contract
            )

    route_timing_paths = _route_timing_paths(routes)
    selected_path_ids = {
        member
        for item in route_timing_paths
        for member in item.get("compressed_path_ids", [item["path"]])
    }
    selected_paths = (
        list(path_database.get("paths", []))
        if (
            path_database.get("schema") == STA_PATH_DATABASE_SCHEMA
            and not selected_path_ids
        )
        else list(
            iter_selected_sta_path_database_paths(
                path_database_path,
                path_database,
                selected_path_ids,
                verify_payload=False,
            )
        )
    )
    indexed = (
        is_emuir_sqlite(original_ir_path)
        and is_compact_emuir(original_ir_path)
    )
    exact_launches: Dict[
        tuple[str, str], List[Mapping[str, Any]]
    ] = {}
    if indexed:
        view = IndexedTimingView(original_ir_path)
        try:
            object_index = view.object_index
            path_endpoints = {
                path["id"]: sta_path_endpoints(path, object_index)
                for path in selected_paths
            }
            selected_ids = {
                net_id
                for path in selected_paths
                for net_id in path.get("path_nets", [])
            }
            selected_ids.update(item["net"] for item in routes["routes"])
            if isinstance(exact_contract, dict) and exact_index_path is None:
                for semantic in exact_contract["logic_segments"]:
                    for field in ("source_cut_net", "sink_cut_net"):
                        if isinstance(semantic.get(field), str):
                            selected_ids.add(semantic[field])
            original_nets = selected_net_records(
                view.connection, selected_ids
            )
            if isinstance(exact_contract, dict) and exact_index_path is None:
                instance_assignment = assignment["instance_assignment"]
                cache: Dict[str, Mapping[str, Any]] = dict(original_nets)
                for semantic in exact_contract["logic_segments"]:
                    if semantic["kind"] != "launch_to_tx":
                        continue
                    key = (semantic["sink_cut_net"], semantic["fpga"])
                    if key in exact_launches:
                        continue
                    exact_launches[key] = _indexed_architectural_launch_endpoints(
                        view.connection,
                        instance_assignment,
                        semantic["sink_cut_net"],
                        semantic["fpga"],
                        _incoming_transported_cut_nets(
                            exact_contract, semantic["fpga"]
                        ),
                        cache,
                    )
                original_nets.update(cache)
        finally:
            view.close()
        original_ir = None
        object_index = None
        original_instances: Dict[str, Mapping[str, Any]] = {}
        incoming_nets_by_instance: Dict[str, List[str]] = {}
    else:
        original_ir = EmuIR.load(original_ir_path)
        object_index = sta_object_index(original_ir)
        path_endpoints = {
            path["id"]: sta_path_endpoints(path, object_index)
            for path in selected_paths
        }
        original_nets = {
            item["id"]: item for item in original_ir.value["nets"]
        }
        original_instances = {
            item["id"]: item for item in original_ir.value["instances"]
        }
        incoming_nets_by_instance = defaultdict(list)
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
        object_index=object_index,
        path_endpoints=path_endpoints,
        database_paths={item["id"]: item for item in selected_paths},
        route_timing={item["path"]: item for item in route_timing_paths},
        route_by_net={item["net"]: item for item in routes["routes"]},
        timing_records=reconstruct_tdm_schedule_timing_paths(
            routes, platform, schedule
        ),
        original_nets=original_nets,
        original_instances=original_instances,
        incoming_nets_by_instance=incoming_nets_by_instance,
        exact_contract=exact_contract,
        exact_contract_sha256=exact_contract_sha256,
        exact_segment_by_key=exact_segment_by_key,
        exact_captures=exact_captures,
        exact_launches=exact_launches,
        exact_index_path=exact_index_path,
    )


def _write_logic_segment_query_impl(
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
    segments: Any,
    *,
    object_provider: str,
    prepared_inputs: Optional[LogicSegmentQueryInputs] = None,
    allow_legacy_schedule: bool = False,
    allow_legacy_exact_contract: bool = False,
) -> Dict[str, Any]:
    """Build physical path queries for all exact logical segments on one FPGA."""
    prepared = prepared_inputs or prepare_logic_segment_query_inputs(
        original_ir_path,
        assignment_path,
        path_database_path,
        routes_path,
        schedule_path,
        platform,
        allow_legacy_schedule=allow_legacy_schedule,
    )
    original_ir = prepared.original_ir
    indexed_merged = (
        is_emuir_sqlite(merged_ir_path) and is_compact_emuir(merged_ir_path)
    )
    merged_ir = (
        IndexedPhysicalIR(merged_ir_path)
        if indexed_merged
        else EmuIR.load(merged_ir_path)
    )
    try:
        assignment = prepared.assignment
        path_database = prepared.path_database
        routes = prepared.routes
        schedule = prepared.schedule
        from .boundary_identity_storage import read_boundary_identity_storage

        boundary_identity = read_boundary_identity_storage(
            boundary_identity_path, allow_legacy=True
        )
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
        exact_segment_by_key = dict(prepared.exact_segment_by_key)
        exact_captures = dict(prepared.exact_captures)
        exact_segments = (
            list(exact_contract["logic_segments"])
            if exact_contract is not None
            and prepared.exact_index_path is None
            else []
        )
        if (
            exact_contract_sha256 is not None
            and prepared.exact_index_path is None
            and not allow_legacy_exact_contract
        ):
            raise ValidationError(
                "legacy inline Static Exact contract requires explicit compatibility"
            )
        indexed_exact_spool = False
        if prepared.exact_index_path is not None:
            partition_semantics, segment_records, capture_records = (
                _stream_indexed_exact_partition(
                    prepared.exact_index_path,
                    fpga,
                    allow_legacy=allow_legacy_exact_contract,
                )
            )
            if (
                partition_semantics.get("schema")
                != schedule.get("semantic_contract_schema")
                or partition_semantics.get("sha256")
                != exact_contract_sha256
            ):
                raise ValidationError(
                    "indexed Static Exact semantic identity disagrees"
                )
            segments.index_exact_contract(capture_records, segment_records)
            indexed_exact_spool = True

        def exact_segment_records() -> Any:
            return (
                segments.exact_contract_segments()
                if indexed_exact_spool else iter(exact_segments)
            )

        def exact_capture_record(capture_id: str) -> Mapping[str, Any]:
            return (
                segments.exact_capture(capture_id)
                if indexed_exact_spool else exact_captures[capture_id]
            )

        def exact_segment_id(exact_key: tuple[Any, ...]) -> Optional[str]:
            return (
                segments.exact_segment_id(exact_key)
                if indexed_exact_spool else exact_segment_by_key.get(exact_key)
            )
        database_paths = prepared.database_paths
        route_timing = prepared.route_timing
        route_by_net = prepared.route_by_net
        records = prepared.timing_records
        instance_index = (
            merged_ir.instance_indices
            if indexed_merged
            else {
                instance["id"]: index
                for index, instance in enumerate(merged_ir.value["instances"])
            }
        )
        merged_instances = (
            merged_ir.instances
            if indexed_merged
            else {
                instance["id"]: instance
                for instance in merged_ir.value["instances"]
            }
        )
        merged_net_index = (
            merged_ir.net_indices
            if indexed_merged
            else {
                net["id"]: index
                for index, net in enumerate(merged_ir.value["nets"])
            }
        )
        original_nets = dict(prepared.original_nets)
        original_instances = prepared.original_instances
        incoming_nets_by_instance = prepared.incoming_nets_by_instance
        exact_launches = dict(prepared.exact_launches)
        if prepared.exact_index_path is not None and exact_contract_sha256 is not None:
            incoming_cut_nets = _incoming_assignment_cut_nets(assignment, fpga)
            instance_assignment = assignment["instance_assignment"]
            if is_emuir_sqlite(original_ir_path) and is_compact_emuir(
                original_ir_path
            ):
                source_view = IndexedTimingView(original_ir_path)
                try:
                    for semantic in exact_segment_records():
                        if semantic["kind"] != "launch_to_tx":
                            continue
                        key = (semantic["sink_cut_net"], fpga)
                        if key in exact_launches:
                            continue
                        exact_launches[key] = _indexed_architectural_launch_endpoints(
                            source_view.connection,
                            instance_assignment,
                            semantic["sink_cut_net"],
                            fpga,
                            incoming_cut_nets,
                            original_nets,
                        )
                finally:
                    source_view.close()
            else:
                if original_ir is None:
                    raise ValidationError("Static Exact source IR is unavailable")
                for semantic in exact_segment_records():
                    if semantic["kind"] != "launch_to_tx":
                        continue
                    key = (semantic["sink_cut_net"], fpga)
                    if key in exact_launches:
                        continue
                    exact_launches[key] = _architectural_launch_endpoints(
                        original_ir,
                        instance_assignment,
                        semantic["sink_cut_net"],
                        fpga,
                        incoming_cut_nets,
                        nets=original_nets,
                        instances=original_instances,
                        incoming=incoming_nets_by_instance,
                    )
        merged_pins = _instance_pin_inventory(merged_ir)
        if object_provider not in {"vivado", "xilinx"}:
            raise ValidationError("logic segment object provider is invalid")

        def endpoint_object(endpoint: Mapping[str, Any]) -> tuple[str, str]:
            if object_provider == "vivado":
                return _vivado_object(
                    merged_ir, endpoint, merged_pins, merged_instances
                )
            return _xilinx_object(
                merged_ir, endpoint, merged_pins, merged_instances
            )

        def tx_object(endpoint_id: str) -> tuple[str, str]:
            endpoint = endpoints.get(endpoint_id)
            if endpoint is None or endpoint.get("kind") != "tx":
                raise ValidationError(f"logic segment TX {endpoint_id!r} is absent")
            merged = endpoint["merged_ir"]
            external = {
                "instance": None,
                "port": merged["external_port"],
                "bit": merged["external_port_bit"],
            }
            if object_provider == "vivado":
                return _vivado_object(
                    merged_ir, external, merged_pins, merged_instances
                )
            return _xilinx_object(
                merged_ir, external, merged_pins, merged_instances
            )

        def rx_object(endpoint_id: str) -> tuple[str, str]:
            endpoint = endpoints.get(endpoint_id)
            if endpoint is None or endpoint.get("kind") != "rx":
                raise ValidationError(f"logic segment RX {endpoint_id!r} is absent")
            registers = endpoint["merged_ir"]["boundary_register_instances"]
            if not isinstance(registers, list) or len(registers) != 1:
                raise ValidationError(
                    f"logic segment RX {endpoint_id!r} is ambiguous"
                )
            register_q = {"instance": registers[0], "port": "Q", "bit": 0}
            if object_provider == "vivado":
                return _vivado_object(
                    merged_ir, register_q, merged_pins, merged_instances
                )
            return _xilinx_object(
                merged_ir, register_q, merged_pins, merged_instances
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
                    start_endpoint, end_endpoint = prepared.path_endpoints[member]
                    if object_provider == "xilinx":
                        start_endpoint = _normalize_xilinx_logical_endpoint(
                            start_endpoint, merged_pins, merged_instances
                        )
                        end_endpoint = _normalize_xilinx_logical_endpoint(
                            end_endpoint, merged_pins, merged_instances
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
                            matched_exact_segment_id = exact_segment_id(exact_key)
                            if matched_exact_segment_id is None:
                                raise ValidationError(
                                    "physical logic segment is absent from the "
                                    "static exact semantic contract"
                                )
                            segment["static_exact_segment_id"] = matched_exact_segment_id
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
                        capture_identity = _exact_capture_identity(
                            {
                                "kind": (
                                    "architectural-state"
                                    if end_instance is not None
                                    else "top-output"
                                ),
                                "endpoint": (
                                    end_instance
                                    if end_instance is not None
                                    else (
                                        f"top:{end_endpoint['port']}"
                                        f"[{end_endpoint['bit']}]"
                                    )
                                ),
                                **(
                                    {
                                        "port": end_endpoint["port"],
                                        "bit": end_endpoint["bit"],
                                    }
                                    if end_instance is not None
                                    else {}
                                ),
                            }
                        )
                        exact_key = (
                            "capture",
                            record["cut_nets"][-1],
                            None,
                            fpga,
                            capture_identity,
                        )
                        matched_exact_segment_id = exact_segment_id(exact_key)
                        if matched_exact_segment_id is None:
                            raise ValidationError(
                                "physical capture segment is absent from the "
                                "static exact semantic contract"
                            )
                        segment["static_exact_segment_id"] = matched_exact_segment_id
                    if object_provider == "vivado":
                        segment.update(
                            {
                                "start_object_kind": start_kind,
                                "end_object_kind": end_kind,
                            }
                        )
                    segments.append(segment)
        if exact_contract_sha256 is not None:
            segments.index_schedule_entries(schedule["entries"])
            incoming_cut_nets = _incoming_assignment_cut_nets(assignment, fpga)
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

            def tx_entries(
                net: str, source_fpga: str
            ) -> Any:
                found = False
                for entry in segments.schedule_from(net, source_fpga):
                    found = True
                    yield entry
                if not found:
                    raise ValidationError(
                        f"static exact cut {net!r} has no TX from {source_fpga!r}"
                    )

            def rx_entry(net: str, sink_fpga: str) -> Mapping[str, Any]:
                result = segments.schedule_to(net, sink_fpga)
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
                    capture = exact_capture_record(semantic["capture_requirement"])
                    end_kind, end_pin = endpoint_object(
                        capture_endpoint(capture)
                    )
                else:
                    end_kind, end_pin = tx_object(end_tx)
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
                segments.append_if_new_exact(segment)

            for semantic in exact_segment_records():
                if semantic["fpga"] != fpga:
                    continue
                kind = semantic["kind"]
                if kind == "launch_to_tx":
                    sink_cut_net = semantic["sink_cut_net"]
                    starts = launches_by_sink_net.get(sink_cut_net)
                    if starts is None:
                        starts = exact_launches.get((sink_cut_net, fpga))
                        if starts is None:
                            if original_ir is None:
                                raise ValidationError(
                                    "indexed exact launch evidence is absent"
                                )
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
                    ordinal = 0
                    for start in starts:
                        for item in tx_entries(semantic["sink_cut_net"], fpga):
                            add_contract_segment(
                                semantic, "launch", start,
                                entry_endpoint(item, "tx"), ordinal,
                            )
                            ordinal += 1
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

        segments.order_by_segment_id()
        segment_coverage = segments.coverage_counts()
        identity_header = {
            "schema": LOGIC_SEGMENT_IDENTITY_SCHEMA,
            "status": "pass",
            "design": assignment["design"],
            "platform": platform.name,
            "fpga": fpga,
            "provider": f"sta-endpoint-chain-to-{object_provider}-objects-v1",
            "coverage": {
                **segment_coverage,
                "unsupported_member_paths": len(unsupported),
            },
            "system_routes_sha256": pretty_system_routes_sha256(routes),
            "tdm_schedule_sha256": canonical_tdm_schedule_sha256(schedule),
            **(
                {"semantic_contract_sha256": exact_contract_sha256}
                if exact_contract_sha256 is not None
                else {}
            ),
        }
        from .logic_segment_identity_storage import (
            write_logic_segment_identity_storage,
        )

        query_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_query = query_path.with_name(
            f".{query_path.name}.{uuid.uuid4().hex}.publish.tmp"
        )
        query_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        query_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary_query, query_flags, 0o600)
        try:
            if temporary_query.is_symlink():
                raise ValidationError("logic segment query temporary is a symlink")
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                stream.write(
                    LOGIC_SEGMENT_QUERY_HEADER
                    if object_provider == "xilinx"
                    else VIVADO_LOGIC_SEGMENT_QUERY_HEADER
                )
                stream.write("\n")
                for item in segments:
                    if object_provider == "xilinx":
                        fields = (
                            item["id"], item["kind"], item["start_pin"],
                            item["end_pin"],
                        )
                    else:
                        fields = (
                            item["id"].encode("utf-8").hex(),
                            item["kind"], item["start_object_kind"],
                            item["start_pin"].encode("utf-8").hex(),
                            item["end_object_kind"],
                            item["end_pin"].encode("utf-8").hex(),
                            item.get("cone_anchor_object_kind", ""),
                            item.get("cone_anchor_pin", "").encode("utf-8").hex(),
                        )
                    stream.write("\t".join(fields))
                    stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            identity_view = write_logic_segment_identity_storage(
                identity_path,
                header=identity_header,
                segments=segments,
                unsupported_member_paths=(
                    {"path": member, "reason": reason}
                    for member, reason in sorted(unsupported.items())
                ),
                sidecar_publication=(temporary_query, query_path),
                source_bindings=bind_view_sources(
                    (merged_ir, "emuir-compact-v2"),
                    (boundary_identity, "boundary-identity-store-v1"),
                ),
            )
            identity_view.close()
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            temporary_query.unlink(missing_ok=True)
            raise
        return {
            "status": "pass",
            "fpga": fpga,
            "segments": len(segments),
            "exact_member_paths": exact_members,
            "unsupported_member_paths": len(unsupported),
            "query": str(query_path),
            "identity": str(identity_path),
        }
    finally:
        close_boundary = getattr(locals().get("boundary_identity"), "close", None)
        if callable(close_boundary):
            close_boundary()
        if indexed_merged:
            merged_ir.close()


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
    prepared_inputs: Optional[LogicSegmentQueryInputs] = None,
    allow_legacy_schedule: bool = False,
    allow_legacy_exact_contract: bool = False,
) -> Dict[str, Any]:
    from .logic_segment_identity_storage import LogicSegmentIdentitySpool

    with LogicSegmentIdentitySpool(identity_path.parent) as segments:
        return _write_logic_segment_query_impl(
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
            segments,
            object_provider=object_provider,
            prepared_inputs=prepared_inputs,
            allow_legacy_schedule=allow_legacy_schedule,
            allow_legacy_exact_contract=allow_legacy_exact_contract,
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
    allow_legacy_schedule: bool = False,
    allow_legacy_exact_contract: bool = False,
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
        allow_legacy_schedule=allow_legacy_schedule,
        allow_legacy_exact_contract=allow_legacy_exact_contract,
    )


def write_xilinx_logic_segment_query(
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
    allow_legacy_schedule: bool = False,
    allow_legacy_exact_contract: bool = False,
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
        object_provider="xilinx",
        prepared_inputs=prepared_inputs,
        allow_legacy_schedule=allow_legacy_schedule,
        allow_legacy_exact_contract=allow_legacy_exact_contract,
    )


def validate_logic_segment_identity(
    database: Mapping[str, Any],
    *,
    require_source_seals: bool = True,
) -> Dict[str, Any]:
    indexed_validator = getattr(database, "validate_identity", None)
    if callable(indexed_validator):
        return indexed_validator()
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
    for field in ("system_routes_sha256", "tdm_schedule_sha256"):
        digest = database.get(field)
        if digest is None and not require_source_seals:
            continue
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValidationError(f"logic segment {field} seal is invalid")
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


def _indexed_logic_measurement_import(
    input_path: Path,
    identity: Any,
    output_path: Path,
    *,
    provider: str,
    qualification: str,
    allow_missing: bool,
    allow_legacy_timing_format: bool,
    optimization_proofs=None,
) -> Dict[str, Any]:
    """Join a routed TSV to a typed identity without graph-sized dictionaries."""
    from .logic_segment_identity_storage import segment_from_row
    from .logic_segment_timing_storage import write_logic_segment_timing_storage

    source_bindings = bind_view_sources(
        (identity, "logic-segment-identity-store-v1")
    ) + (
        bind_store_source(input_path, "physical-timing-measurements-v1"),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".logic-measurements.", dir=output_path.parent
    ) as temporary:
        spool_path = Path(temporary) / "measurements.sqlite"
        connection = sqlite3.connect(spool_path, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute(
            "CREATE TABLE measurements("
            "ordinal INTEGER PRIMARY KEY,segment_id TEXT UNIQUE NOT NULL,"
            "kind TEXT NOT NULL,start_object TEXT NOT NULL,end_object TEXT NOT NULL,"
            "delay_ns REAL,measurement TEXT NOT NULL,"
            "actual_start_object TEXT,actual_end_object TEXT,optimization_source TEXT)"
        )
        connection.execute(
            "ATTACH DATABASE ? AS identity",
            (immutable_sqlite_uri(identity.store_path),),
        )
        inserted = 0
        try:
            with input_path.open("r", encoding="utf-8") as stream:
                header = next(stream, "").rstrip("\n")
                if header not in {
                    VIVADO_LOGIC_SEGMENT_TIMING_HEADER,
                    _LEGACY_VIVADO_LOGIC_SEGMENT_TIMING_HEADER,
                }:
                    raise ValidationError(
                        "Vivado logic segment timing header is invalid"
                    )
                extended_format = (
                    header == VIVADO_LOGIC_SEGMENT_TIMING_HEADER
                )
                if not extended_format and not allow_legacy_timing_format:
                    raise ValidationError(
                        "legacy Vivado logic timing format requires explicit compatibility"
                    )
                batch = []
                for line_number, line in enumerate(stream, start=2):
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    fields = line.split("\t")
                    try:
                        segment_id = bytes.fromhex(fields[0]).decode("utf-8")
                        start_object = bytes.fromhex(fields[3]).decode("utf-8")
                        end_object = bytes.fromhex(fields[4]).decode("utf-8")
                        measurement = (
                            fields[5]
                            if extended_format else "endpoint-exact"
                        )
                        actual_start = (
                            bytes.fromhex(fields[6]).decode("utf-8")
                            if extended_format else start_object
                        )
                        actual_end = (
                            bytes.fromhex(fields[7]).decode("utf-8")
                            if extended_format else end_object
                        )
                        expected_fields = 8 if extended_format else 5
                        eliminated = measurement.startswith(OPTIMIZED_AWAY + ":")
                        proof_source = measurement.split(":", 1)[1] if eliminated else None
                        if eliminated:
                            if fields[2] or actual_start or actual_end:
                                raise ValueError("eliminated path carries physical scalars")
                            measurement = OPTIMIZED_AWAY
                            actual_start = actual_end = None
                        delay = None if eliminated else float(fields[2])
                    except (IndexError, ValueError, UnicodeDecodeError) as error:
                        label = "Vivado"
                        raise ValidationError(
                            f"{label} logic segment timing line {line_number} is invalid"
                        ) from error
                    valid = (
                        len(fields) == expected_fields
                        and fields[1] in {"launch", "transition", "capture"}
                        and bool(segment_id and start_object and end_object)
                        and measurement in {
                            "endpoint-exact", "cut-net-cone-upper-bound", "optimized-endpoint-collection-upper-bound", OPTIMIZED_AWAY
                        }
                        and (eliminated or (bool(actual_start and actual_end)
                                            and math.isfinite(delay) and delay >= 0.0))
                    )
                    if not valid:
                        label = "Vivado"
                        raise ValidationError(
                            f"{label} logic segment timing line {line_number} is invalid"
                        )
                    if eliminated:
                        disposition = {"measurement": OPTIMIZED_AWAY, "delay_ns": None,
                                       "optimization_source": proof_source,
                                       "start_pin": start_object, "end_pin": end_object}
                        validate_optimization_record(disposition)
                        require_optimization_proof({"optimization_proofs": {identity["fpga"]: optimization_proofs}},
                                                   identity["fpga"], proof_source, disposition)
                    batch.append((
                        inserted + len(batch), segment_id, fields[1],
                        start_object, end_object, delay, measurement,
                        actual_start, actual_end, proof_source,
                    ))
                    if len(batch) >= 8192:
                        try:
                            connection.executemany(
                                "INSERT INTO measurements VALUES (?,?,?,?,?,?,?,?,?,?)",
                                batch,
                            )
                        except sqlite3.IntegrityError as error:
                            raise ValidationError(
                                "logic segment measurement identity duplicates"
                            ) from error
                        inserted += len(batch)
                        batch.clear()
                if batch:
                    try:
                        connection.executemany(
                            "INSERT INTO measurements VALUES (?,?,?,?,?,?,?,?,?,?)",
                            batch,
                        )
                    except sqlite3.IntegrityError as error:
                        raise ValidationError(
                            "logic segment measurement identity duplicates"
                        ) from error
                    inserted += len(batch)
            unknown = connection.execute(
                "SELECT m.segment_id FROM measurements m "
                "LEFT JOIN identity.segments i USING(segment_id) "
                "WHERE i.segment_id IS NULL ORDER BY m.ordinal LIMIT 1"
            ).fetchone()
            if unknown is not None:
                raise ValidationError(
                    "logic segment measurement references an unknown identity"
                )
            mismatch = connection.execute(
                "SELECT m.segment_id FROM measurements m JOIN identity.segments i "
                "USING(segment_id) WHERE m.kind!=i.kind OR "
                "m.start_object!=i.start_pin OR m.end_object!=i.end_pin "
                "ORDER BY m.ordinal LIMIT 1"
            ).fetchone()
            if mismatch is not None:
                raise ValidationError(
                    "logic segment measurement endpoint identity disagrees"
                )
            out_of_order = connection.execute(
                "SELECT 1 FROM (SELECT i.seq,LAG(i.seq) OVER "
                "(ORDER BY m.ordinal) AS previous_seq FROM measurements m "
                "JOIN identity.segments i USING(segment_id)) "
                "WHERE previous_seq IS NOT NULL AND seq<=previous_seq LIMIT 1"
            ).fetchone()
            if out_of_order is not None:
                raise ValidationError("logic segment measurement order is invalid")
            expected_count = identity.selector["counts"]["segments"]
            missing_count = expected_count - inserted
            if missing_count < 0:
                raise ValidationError("logic segment timing coverage is invalid")
            if missing_count and not allow_missing:
                missing = [
                    str(row[0]) for row in connection.execute(
                        "SELECT i.segment_id FROM identity.segments i "
                        "LEFT JOIN measurements m USING(segment_id) "
                        "WHERE m.segment_id IS NULL ORDER BY i.seq LIMIT 10"
                    )
                ]
                label = "Vivado"
                raise ValidationError(
                    f"{label} logic segment timing coverage is incomplete: {missing}"
                )
            connection.execute(
                "CREATE TABLE final_unsupported("
                "path TEXT PRIMARY KEY,reason TEXT NOT NULL) WITHOUT ROWID"
            )
            connection.execute(
                "INSERT INTO final_unsupported "
                "SELECT path,reason FROM identity.unsupported_members"
            )
            if missing_count:
                connection.execute(
                    "INSERT INTO final_unsupported(path,reason) "
                    "SELECT DISTINCT i.member_path,'no-routed-vivado-board-path' "
                    "FROM identity.segments i LEFT JOIN measurements m USING(segment_id) "
                    "WHERE m.segment_id IS NULL "
                    "ON CONFLICT(path) DO UPDATE SET reason=excluded.reason"
                )
            connection.commit()

            header_value = {
                "schema": LOGIC_SEGMENT_TIMING_SCHEMA,
                "status": "pass",
                "design": identity["design"],
                "platform": identity["platform"],
                "fpga": identity["fpga"],
                "provider": provider,
                "qualification": qualification,
                "coverage": identity["coverage"],
                "system_routes_sha256": identity["system_routes_sha256"],
                "tdm_schedule_sha256": identity["tdm_schedule_sha256"],
                **(
                    {"semantic_contract_sha256": identity["semantic_contract_sha256"]}
                    if "semantic_contract_sha256" in identity else {}
                ),
            }

            def measured_records():
                for row in connection.execute(
                    "SELECT i.*,m.delay_ns,m.measurement,"
                    "m.actual_start_object,m.actual_end_object,m.optimization_source "
                    "FROM identity.segments i JOIN measurements m USING(segment_id) "
                    "ORDER BY i.system_path,i.member_path,i.cut_index,i.segment_id"
                ):
                    record = segment_from_row(row)
                    record.update({
                        "delay_ns": None if row["delay_ns"] is None else float(row["delay_ns"]),
                        "measurement": str(row["measurement"]),
                    })
                    if row["optimization_source"] is not None:
                        record["optimization_source"] = str(row["optimization_source"])
                    if row["actual_start_object"] is not None:
                        record["actual_start_object"] = str(
                            row["actual_start_object"]
                        )
                        record["actual_end_object"] = str(
                            row["actual_end_object"]
                        )
                    yield record

            def unsupported_records():
                for row in connection.execute(
                    "SELECT path,reason FROM final_unsupported ORDER BY path"
                ):
                    yield {"path": str(row[0]), "reason": str(row[1])}

            def unmeasured_records():
                for row in connection.execute(
                    "SELECT i.segment_id,i.kind,i.system_path,i.member_path "
                    "FROM identity.segments i LEFT JOIN measurements m USING(segment_id) "
                    "WHERE m.segment_id IS NULL ORDER BY i.segment_id"
                ):
                    yield {
                        "id": str(row[0]), "kind": str(row[1]),
                        "system_path": str(row[2]), "member_path": str(row[3]),
                        "reason": "no-routed-vivado-board-path",
                    }

            view = write_logic_segment_timing_storage(
                output_path,
                header=header_value,
                segments=measured_records(),
                unsupported_member_paths=unsupported_records(),
                unmeasured_segments=(unmeasured_records() if allow_missing else ()),
                source_bindings=source_bindings,
            )
            try:
                validation = view.validate_timing()
            finally:
                view.close()
            return {
                **validation,
                **({"missing_segments": missing_count}),
                "output": str(output_path),
            }
        finally:
            connection.close()


def import_vivado_logic_segment_timing(
    input_path: Path,
    identity_path: Path,
    output_path: Path,
    *,
    provider: str = "vivado-routed-logic-segment-datapath-v1",
    qualification: str = "routed-vendor-device-endpoint-chain",
    allow_missing: bool = False,
    allow_legacy_identity: bool = False,
    allow_legacy_timing_format: bool = False,
    optimization_proofs=None,
) -> Dict[str, Any]:
    from .logic_segment_identity_storage import (
        LogicSegmentIdentityView,
        read_logic_segment_identity_storage,
    )

    identity = read_logic_segment_identity_storage(
        identity_path, allow_legacy=allow_legacy_identity
    )
    if isinstance(identity, LogicSegmentIdentityView):
        try:
            return _indexed_logic_measurement_import(
                input_path,
                identity,
                output_path,
                provider=provider,
                qualification=qualification,
                allow_missing=allow_missing,
                allow_legacy_timing_format=allow_legacy_timing_format,
                optimization_proofs=optimization_proofs,
            )
        finally:
            identity.close()
    source_bindings = (
        bind_store_source(input_path, "physical-timing-measurements-v1"),
    )
    validate_logic_segment_identity(identity)
    expected = {item["id"]: item for item in identity["segments"]}
    lines = input_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] not in {
        VIVADO_LOGIC_SEGMENT_TIMING_HEADER,
        _LEGACY_VIVADO_LOGIC_SEGMENT_TIMING_HEADER,
    }:
        raise ValidationError("Vivado logic segment timing header is invalid")
    extended_format = lines[0] == VIVADO_LOGIC_SEGMENT_TIMING_HEADER
    if not extended_format and not allow_legacy_timing_format:
        raise ValidationError(
            "legacy Vivado logic timing format requires explicit compatibility"
        )
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
                "optimized-endpoint-collection-upper-bound",
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
    unsupported = {
        item["path"]: item["reason"]
        for item in identity["unsupported_member_paths"]
    }
    for segment_id in missing:
        segment = expected[segment_id]
        unsupported[segment["member_path"]] = (
            "no-routed-vivado-board-path"
        )
    header = {
        "schema": LOGIC_SEGMENT_TIMING_SCHEMA,
        "status": "pass",
        "design": identity["design"],
        "platform": identity["platform"],
        "fpga": identity["fpga"],
        "provider": provider,
        "qualification": qualification,
        "coverage": identity["coverage"],
        "system_routes_sha256": identity["system_routes_sha256"],
        "tdm_schedule_sha256": identity["tdm_schedule_sha256"],
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
    from .logic_segment_timing_storage import write_logic_segment_timing_storage

    view = write_logic_segment_timing_storage(
        output_path,
        header=header,
        segments=(
            {**segment, **measurements[segment["id"]]}
            for segment in identity["segments"]
            if segment["id"] in measurements
        ),
        unsupported_member_paths=(
            {"path": path, "reason": reason}
            for path, reason in sorted(unsupported.items())
        ),
        unmeasured_segments=(
            {
                "id": segment_id,
                "kind": expected[segment_id]["kind"],
                "system_path": expected[segment_id]["system_path"],
                "member_path": expected[segment_id]["member_path"],
                "reason": "no-routed-vivado-board-path",
            }
            for segment_id in missing
        ),
        source_bindings=source_bindings,
    )
    try:
        validation = view.validate_timing()
    finally:
        view.close()
    return {
        **validation,
        "missing_segments": len(missing),
        "output": str(output_path),
    }


def validate_logic_segment_timing(
    database: Mapping[str, Any],
) -> Dict[str, Any]:
    indexed_validator = getattr(database, "validate_timing", None)
    if callable(indexed_validator):
        return indexed_validator()
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
    for key in ("system_routes_sha256", "tdm_schedule_sha256"):
        if key in database:
            identity[key] = database[key]
    if "semantic_contract_sha256" in database:
        identity["semantic_contract_sha256"] = database[
            "semantic_contract_sha256"
        ]
    identity["schema"] = LOGIC_SEGMENT_IDENTITY_SCHEMA
    identity["provider"] = "timing-validation"
    validate_logic_segment_identity(identity, require_source_seals=False)
    maximum = 0.0
    endpoint_exact = 0
    cone_bound = 0
    collection_bound = 0
    optimized_away = 0
    for segment in database["segments"]:
        if validate_optimization_record(segment):
            optimized_away += 1
            continue
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
            "optimized-endpoint-collection-upper-bound",
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
        elif measurement == "optimized-endpoint-collection-upper-bound":
            collection_bound += 1
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
        "endpoint_collection_bound_segments": collection_bound,
        "maximum_delay_ns": None if optimized_away and optimized_away == len(database["segments"]) else maximum,
        "optimized_away_segments": optimized_away,
    }
