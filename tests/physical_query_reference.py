"""Small independent endpoint naming and fan-in oracle for factual fixtures.

This module has no query preparation, stage invocation, publication or flow.
"""
from __future__ import annotations
import re
from collections import defaultdict
from functools import lru_cache
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple
from emuflow.common.errors import ValidationError
from emuflow.ir.ir import EmuIR
from emuflow.ir.indexed_physical_ir import IndexedPhysicalIR
from emuflow.validation.equivalence import _lut_definition
def _vivado_mapped_name(value):
    return value.replace("\\", "\\\\")
from emuflow.routing.routing import system_route_timing_paths
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
    from emuflow.physical.xilinx_primitives import load_xilinx_primitive_library

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
