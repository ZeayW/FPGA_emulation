"""Bind RapidWright routed delays to Phase-6 and system-timing identities."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple

from .boundary_timing import (
    build_boundary_timing_database,
    validate_boundary_timing_database,
)
from .errors import ValidationError
from .io import read_json, write_json
from .logic_segment_timing import (
    LOGIC_SEGMENT_TIMING_SCHEMA,
    validate_logic_segment_identity,
    validate_logic_segment_timing,
)
from .local_path_timing import (
    LOCAL_PATH_TIMING_SCHEMA,
    validate_local_path_identity,
    validate_local_path_timing,
)
from .xilinx_timing import validate_xilinx_routed_timing


XILINX_SEGMENT_QUALIFICATION = (
    "rapidwright-lightweight-routed-setup-no-hold-abstract-io-boundary"
)
_SEQUENTIAL = {
    "FDCE", "FDPE", "FDRE", "FDSE", "RAMB18E2", "RAMB36E2", "URAM288"
}
_CONSTANTS = {"GND", "VCC"}


def _pin(instance: str, port: str, index: int, width: int) -> str:
    suffix = port if width == 1 else f"{port}[{index}]"
    return f"cell:{instance}/{suffix}"


def _top(port: str, index: int, width: int) -> str:
    suffix = port if width == 1 else f"{port}[{index}]"
    return f"top:{suffix}"


class _TimingGraph:
    def __init__(self, mapped: Mapping[str, Any], timing: Mapping[str, Any]):
        modules = mapped.get("modules", {})
        top = timing.get("top")
        module = modules.get(top) if isinstance(modules, dict) else None
        if not isinstance(module, dict):
            raise ValidationError("RapidWright segment timing top is absent")
        self.cells = module.get("cells", {})
        self.ports = module.get("ports", {})
        if not isinstance(self.cells, dict) or not isinstance(self.ports, dict):
            raise ValidationError("RapidWright segment mapped netlist is invalid")
        self.edges: Dict[str, list[Tuple[str, float]]] = defaultdict(list)
        self.reverse: Dict[str, list[Tuple[str, float]]] = defaultdict(list)
        self.source_offsets: Dict[str, float] = {}
        coefficients = timing["qualification"]["logic_coefficients_ps"]
        lut_delay = max(float(value) for name, value in coefficients.items()
                        if name.startswith("lut_a")) / 1000.0
        ff_delay = float(coefficients["ff_clock_to_q"]) / 1000.0
        carry_delay = float(coefficients["carry_co"]) / 1000.0
        route_delays = {
            (item["sink"]["instance"], item["sink"]["pin"]):
            float(item["route_delay_ns"])
            for item in timing["endpoints"]
        }

        bit_endpoints: Dict[int, list[Tuple[str, str]]] = defaultdict(list)
        for port, value in sorted(self.ports.items()):
            bits = value.get("bits", [])
            direction = value.get("direction")
            for index, bit in enumerate(bits):
                if not isinstance(bit, int):
                    continue
                role = "driver" if direction in {"input", "inout"} else "sink"
                bit_endpoints[bit].append((_top(port, index, len(bits)), role))
                if role == "driver":
                    self.source_offsets.setdefault(_top(port, index, len(bits)), 0.0)
        for instance, cell in sorted(self.cells.items()):
            directions = cell.get("port_directions", {})
            connections = cell.get("connections", {})
            inputs, outputs = [], []
            for port, bits in sorted(connections.items()):
                direction = directions.get(port)
                for index, bit in enumerate(bits):
                    node = _pin(instance, port, index, len(bits))
                    if direction in {"input", "inout"}:
                        inputs.append(node)
                    if direction in {"output", "inout"}:
                        outputs.append(node)
                    if isinstance(bit, int):
                        role = "driver" if direction in {"output", "inout"} else "sink"
                        bit_endpoints[bit].append((node, role))
            cell_type = cell.get("type")
            if cell_type in _SEQUENTIAL:
                for output in outputs:
                    self.source_offsets[output] = (
                        ff_delay if cell_type.startswith("FD") else 0.0
                    )
            elif cell_type in _CONSTANTS:
                for output in outputs:
                    self.source_offsets[output] = 0.0
            else:
                if cell_type == "CARRY8":
                    delay = carry_delay
                elif isinstance(cell_type, str) and (
                    cell_type.startswith("LUT") or cell_type.startswith("MUXF")
                ):
                    delay = lut_delay
                elif cell_type == "DSP48E2":
                    # RapidWright lightweight timing does not characterize the
                    # hard macro. Preserve connectivity with an explicitly
                    # unqualified conservative research bound.
                    delay = 5.0
                else:
                    raise ValidationError(
                        f"RapidWright segment timing does not cover {cell_type!r}"
                    )
                for source in inputs:
                    for target in outputs:
                        self._edge(source, target, delay)

        for bit, endpoints in bit_endpoints.items():
            drivers = [node for node, role in endpoints if role == "driver"]
            sinks = [node for node, role in endpoints if role == "sink"]
            if len(drivers) != 1:
                if sinks:
                    raise ValidationError(
                        f"RapidWright timing bit {bit} lacks one driver"
                    )
                continue
            driver = drivers[0]
            for sink in sinks:
                delay = 0.0
                if sink.startswith("cell:"):
                    body = sink[len("cell:"):]
                    instance, pin = body.rsplit("/", 1)
                    delay = route_delays.get((instance, pin), 0.0)
                self._edge(driver, sink, delay)

    def _edge(self, source: str, target: str, delay: float) -> None:
        self.edges[source].append((target, delay))
        self.reverse[target].append((source, delay))

    def longest(self, starts: Sequence[str], target: str) -> Tuple[float, str]:
        arrival, origin = self._solve_longest(starts, target)
        return arrival[target], origin[target]

    def _solve_longest(
        self, starts: Sequence[str], target: str
    ) -> Tuple[Dict[str, float], Dict[str, str]]:
        """Solve one start set; retain the original target/error ordering."""
        start_values = {
            start: self.source_offsets.get(start, 0.0) for start in starts
        }
        reachable = set(start_values)
        pending = list(start_values)
        while pending:
            node = pending.pop()
            for successor, _delay in self.edges.get(node, []):
                if successor not in reachable:
                    reachable.add(successor)
                    pending.append(successor)
        if target not in reachable:
            raise ValidationError(
                f"RapidWright timing graph has no path to {target!r}"
            )
        indegree = {node: 0 for node in reachable}
        for node in reachable:
            for successor, _delay in self.edges.get(node, []):
                if successor in indegree:
                    indegree[successor] += 1
        queue = deque(sorted(node for node, degree in indegree.items() if degree == 0))
        order = []
        while queue:
            node = queue.popleft()
            order.append(node)
            for successor, _delay in self.edges.get(node, []):
                if successor not in indegree:
                    continue
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    queue.append(successor)
        if len(order) != len(reachable):
            raise ValidationError("RapidWright timing graph contains a combinational cycle")
        arrival = {node: float("-inf") for node in reachable}
        origin = {node: node for node in starts}
        arrival.update(start_values)
        for node in order:
            if not math.isfinite(arrival[node]):
                continue
            for successor, delay in self.edges.get(node, []):
                candidate = arrival[node] + delay
                if candidate > arrival.get(successor, float("-inf")):
                    arrival[successor] = candidate
                    origin[successor] = origin[node]
        return arrival, origin

    def architectural_sources(self) -> list[str]:
        return sorted(self.source_offsets)


def _graph(mapped_path: Path, timing_path: Path) -> _TimingGraph:
    validate_xilinx_routed_timing(timing_path, mapped_path=mapped_path)
    return _TimingGraph(read_json(mapped_path), read_json(timing_path))


def build_xilinx_boundary_timing(
    identity_path: Path,
    mapped_path: Path,
    timing_path: Path,
    output_path: Path,
) -> Dict[str, Any]:
    identity = read_json(identity_path)
    graph = _graph(mapped_path, timing_path)
    measurements = {}
    graph_nodes = set(graph.edges) | set(graph.reverse)
    tx_solution = None
    for endpoint in identity["endpoints"]:
        merged = endpoint["merged_ir"]
        port = merged["external_port"]
        bit = merged["external_port_bit"]
        # The width is encoded in the mapped top-port contract; accept either
        # scalar or indexed spelling by matching the exact prefix.
        candidates = [node for node in (f"top:{port}", f"top:{port}[{bit}]")
                      if node in graph_nodes]
        if len(candidates) != 1:
            raise ValidationError(
                f"RapidWright boundary {endpoint['id']!r} top port is ambiguous"
            )
        top_node = candidates[0]
        if endpoint["kind"] == "tx":
            # All TX queries have the same graph and architectural sources.
            # Keep only this invocation's solution; RX has different sources.
            if tx_solution is None:
                tx_solution = graph._solve_longest(
                    graph.architectural_sources(), top_node
                )
            arrival, origin = tx_solution
            if top_node not in arrival:
                raise ValidationError(
                    f"RapidWright timing graph has no path to {top_node!r}"
                )
            delay, start = arrival[top_node], origin[top_node]
            end = top_node
        else:
            registers = merged["boundary_register_instances"]
            if len(registers) != 1:
                raise ValidationError(
                    f"RapidWright RX boundary {endpoint['id']!r} is ambiguous"
                )
            target = f"cell:{registers[0]}/D"
            delay, start = graph.longest([top_node], target)
            end = target
        measurements[endpoint["id"]] = {
            "delay_ns": delay,
            "start_object": start,
            "end_object": end,
        }
    database = build_boundary_timing_database(
        identity,
        measurements,
        provider="rapidwright-lightweight-segment-graph-v1",
        qualification=XILINX_SEGMENT_QUALIFICATION,
    )
    validation = validate_boundary_timing_database(database, identity)
    write_json(output_path, database)
    return {**validation, "output": str(output_path)}


def build_xilinx_logic_segment_timing(
    identity_path: Path,
    mapped_path: Path,
    timing_path: Path,
    output_path: Path,
) -> Dict[str, Any]:
    identity = read_json(identity_path)
    validate_logic_segment_identity(identity)
    graph = _graph(mapped_path, timing_path)
    records = []
    for segment in identity["segments"]:
        delay, actual_start = graph.longest(
            [segment["start_pin"]], segment["end_pin"]
        )
        records.append({
            **segment,
            "delay_ns": delay,
            "measurement": "endpoint-exact",
            "actual_start_object": actual_start,
            "actual_end_object": segment["end_pin"],
        })
    database = {
        "schema": LOGIC_SEGMENT_TIMING_SCHEMA,
        "status": "pass",
        "design": identity["design"],
        "platform": identity["platform"],
        "fpga": identity["fpga"],
        "provider": "rapidwright-lightweight-logic-segment-graph-v1",
        "qualification": XILINX_SEGMENT_QUALIFICATION,
        "coverage": {
            **identity["coverage"],
            "endpoint_exact_segments": len(records),
            "cone_bound_segments": 0,
        },
        "unsupported_member_paths": identity["unsupported_member_paths"],
        "unmeasured_segments": [],
        "segments": records,
        **(
            {"semantic_contract_sha256": identity["semantic_contract_sha256"]}
            if "semantic_contract_sha256" in identity else {}
        ),
    }
    validation = validate_logic_segment_timing(database)
    write_json(output_path, database)
    return {**validation, "output": str(output_path)}


def build_xilinx_local_path_timing(
    identity_path: Path,
    mapped_path: Path,
    timing_path: Path,
    output_path: Path,
) -> Dict[str, Any]:
    """Measure all original same-FPGA paths on the routed Xilinx graph."""
    identity = read_json(identity_path)
    validate_local_path_identity(identity)
    graph = _graph(mapped_path, timing_path)
    records = []
    for path in identity["paths"]:
        delay, _actual_start = graph.longest(
            [path["start_pin"]], path["end_pin"]
        )
        records.append({**path, "delay_ns": delay})
    database = {
        "schema": LOCAL_PATH_TIMING_SCHEMA,
        "status": "pass",
        "design": identity["design"],
        "fpga": identity["fpga"],
        "provider": "rapidwright-lightweight-local-path-graph-v1",
        "qualification": (
            "source-bound-routed-endpoint-longest-path-upper-bound-"
            "with-launch-clock-to-q-and-capture-setup"
        ),
        "source": identity["source"],
        "identity_schema": identity["schema"],
        "coverage": identity["coverage"],
        "paths": records,
    }
    validation = validate_local_path_timing(database)
    write_json(output_path, database)
    return {**validation, "output": str(output_path)}
