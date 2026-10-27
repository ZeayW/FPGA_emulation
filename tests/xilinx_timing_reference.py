"""Independent fixture producer for routed endpoint binding; not installed."""

from __future__ import annotations

import math
import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional

from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256, read_json, write_json
from emuflow.physical.xilinx_physical_handoff import validate_xilinx_physical_handoff_pair
from emuflow.physical.rapidwright.xilinx_rwroute import iter_xilinx_route_nets, validate_xilinx_route_db


XILINX_ROUTED_TIMING_SCHEMA = "emuflow.xilinx-routed-timing/v1"
XILINX_ROUTED_TIMING_STREAM_SCHEMA = "emuflow.xilinx-routed-timing/v2"
XILINX_ROUTED_TIMING_PAYLOAD_FORMAT = "jsonl-object/v1"


def _sha256(path: Path) -> str:
    return file_sha256(path)


def _placement_sites(value: Mapping[str, Any]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for cluster in value.get("clusters", []):
        if not isinstance(cluster, dict) or not isinstance(cluster.get("site"), str):
            raise ValidationError("Xilinx placement cluster is invalid")
        for assignment in cluster.get("assignments", []):
            instance = assignment.get("instance")
            site = assignment.get("site", cluster["site"])
            if (
                not isinstance(instance, str)
                or not isinstance(site, str)
                or instance in result
            ):
                raise ValidationError("Xilinx placement cell ownership is invalid")
            result[instance] = site
    return result


def _mapped_module(value: Mapping[str, Any], instances: set[str]) -> tuple[str, Mapping[str, Any]]:
    matches = []
    for name, module in value.get("modules", {}).items():
        cells = module.get("cells") if isinstance(module, Mapping) else None
        if isinstance(cells, Mapping) and set(cells) == instances:
            matches.append((name, module))
    if len(matches) != 1:
        raise ValidationError("mapped JSON does not uniquely match Xilinx placement")
    return matches[0]


def _logical_pin(port: str, index: int, width: int) -> str:
    return port if width == 1 else f"{port}[{index}]"


def _build_payload(
    mapped: Mapping[str, Any],
    placement: Mapping[str, Any],
    route: Mapping[str, Any],
    route_path: Path,
    *,
    record_sink: Optional[Callable[[Mapping[str, Any]], None]] = None,
) -> Dict[str, Any]:
    sites = _placement_sites(placement)
    top, module = _mapped_module(mapped, set(sites))
    cells = module["cells"]
    clock_gates = route.get("clock_gates", [])
    clock_gate_input_sites: Dict[str, str] = {}
    cascaded_clock_gate_inputs: set[str] = set()
    for gate in clock_gates:
        logical_cell = gate.get("logical_cell") if isinstance(gate, Mapping) else None
        physical_site = gate.get("physical_site") if isinstance(gate, Mapping) else None
        if (
            not isinstance(logical_cell, str)
            or logical_cell not in cells
            or not isinstance(physical_site, str)
            or not physical_site
        ):
            raise ValidationError("Xilinx clock-gate timing binding is invalid")
        sites[logical_cell] = physical_site
        input_site = gate.get("input_site", physical_site)
        if not isinstance(input_site, str) or not input_site:
            raise ValidationError("Xilinx clock-gate input-site binding is invalid")
        clock_gate_input_sites[logical_cell] = input_site
        if gate.get("cascaded_input") is True:
            cascaded_clock_gate_inputs.add(logical_cell)

    def endpoint_site(instance: str, pin: str) -> str:
        if instance in clock_gate_input_sites and pin in {"I0", "I1"}:
            return clock_gate_input_sites[instance]
        return sites[instance]
    endpoints: Dict[int, list[tuple[str, str, str]]] = defaultdict(list)
    for instance in sorted(cells):
        cell = cells[instance]
        directions = cell.get("port_directions", {})
        for port, bits in cell.get("connections", {}).items():
            if not isinstance(bits, list):
                raise ValidationError(f"mapped cell {instance!r} has an invalid connection")
            direction = directions.get(port)
            if direction not in {"input", "output", "inout"}:
                raise ValidationError(f"mapped cell {instance!r} has an invalid direction")
            role = "driver" if direction in {"output", "inout"} else "sink"
            for index, bit in enumerate(bits):
                if isinstance(bit, int):
                    endpoints[bit].append(
                        (instance, _logical_pin(port, index, len(bits)), role)
                    )
    virtual_io_by_net = {
        item["mapped_net"]: item for item in route.get("virtual_io", [])
    }

    records = [] if record_sink is None else None
    logical_endpoints = 0
    physical_sinks = 0
    exact_site_bindings = 0
    shared_site_bindings = 0
    intra_site_endpoints = 0
    maximum = 0.0
    seen_bits: set[int] = set()
    for net in iter_xilinx_route_nets(
        route_path, route, include_pips=False
    ):
        # build_xilinx_routed_timing validates the complete physical route
        # certificate first, including static roots, reachability and conflicts.
        # Device-tied constants have no mapped integer bit/timed data endpoint.
        # Never use an arbitrary kind/name as permission to drop a signal.
        if net.get("kind") in {"static_vcc", "static_gnd"}:
            expected_name = (
                "GLOBAL_LOGIC1" if net["kind"] == "static_vcc" else "GLOBAL_LOGIC0"
            )
            if (
                net.get("net") != expected_name
                or net.get("qualification") != "device-tied-static"
            ):
                raise ValidationError("Xilinx static route identity is invalid")
            continue
        if net.get("kind") == "clock_internal":
            if net.get("qualification") != "lut2-prefilter-to-bufgce-v1":
                raise ValidationError("Xilinx internal clock route is invalid")
            continue
        match = re.fullmatch(r"n([0-9]+)", str(net.get("net", "")))
        if match is None:
            raise ValidationError("Xilinx route net does not encode a mapped bit")
        bit = int(match.group(1))
        if bit in seen_bits:
            raise ValidationError("Xilinx route repeats a mapped bit")
        seen_bits.add(bit)
        logical = endpoints.get(bit, [])
        drivers = [value for value in logical if value[2] == "driver"]
        sinks = [
            value for value in logical
            if value[2] == "sink"
            and not (
                value[0] in cascaded_clock_gate_inputs and value[1] == "I0"
            )
        ]
        route_net = net
        route_sources = [pin for pin in route_net["pins"] if pin["is_output"]]
        virtual_io = virtual_io_by_net.get(net.get("net"))
        if virtual_io is None:
            if len(drivers) != 1 or not sinks:
                raise ValidationError(
                    f"routed bit {bit} lacks one mapped driver and sinks"
                )
            driver, driver_pin, _ = drivers[0]
            driver_site = endpoint_site(driver, driver_pin)
            if len(route_sources) != 1 or route_sources[0]["site"] != driver_site:
                raise ValidationError(f"routed bit {bit} driver site disagrees")
        elif virtual_io["direction"] == "tx":
            if len(drivers) != 1 or sinks:
                raise ValidationError(f"TX virtual I/O bit {bit} endpoint set is invalid")
            driver, driver_pin, _ = drivers[0]
            driver_site = endpoint_site(driver, driver_pin)
            if (
                len(route_sources) != 1
                or route_sources[0]["site"] != driver_site
                or not any(
                    not pin["is_output"] and pin["site"] == virtual_io["p_site"]
                    for pin in route_net["pins"]
                )
            ):
                raise ValidationError(f"TX virtual I/O bit {bit} route is invalid")
            # There is no mapped logical sink beyond the fixed output IOB.
            # Its route delay is carried in the explicit virtual-I/O timing
            # record and added to the TX boundary measurement downstream.
            continue
        elif virtual_io["direction"] == "rx":
            if drivers or not sinks:
                raise ValidationError(f"RX virtual I/O bit {bit} endpoint set is invalid")
            driver = f"virtual_io:{virtual_io['id']}"
            driver_pin = "O"
            driver_site = virtual_io["p_site"]
            if len(route_sources) != 1 or route_sources[0]["site"] != driver_site:
                raise ValidationError(f"RX virtual I/O bit {bit} route is invalid")
        else:
            raise ValidationError(f"virtual I/O bit {bit} direction is invalid")
        delay_by_site: Dict[str, list[float]] = defaultdict(list)
        for pin in route_net["pins"]:
            if not pin["is_output"]:
                delay_by_site[pin["site"]].append(float(pin["route_delay_ps"]))
                physical_sinks += 1
        logical_sites = {
            endpoint_site(instance, pin) for instance, pin, _role in sinks
        }
        if not set(delay_by_site).issubset(logical_sites):
            raise ValidationError(f"routed bit {bit} has an unbound physical sink site")
        for instance, sink_pin, _ in sorted(sinks):
            sink_site = endpoint_site(instance, sink_pin)
            delays = delay_by_site.get(sink_site)
            if delays:
                delay_ps = max(delays)
                binding = "exact-site-pin" if len(delays) == 1 else "shared-site-conservative-max"
                if len(delays) == 1:
                    exact_site_bindings += 1
                else:
                    shared_site_bindings += 1
            elif sink_site == driver_site:
                delay_ps = 0.0
                binding = "intra-site-interconnect-zero"
                intra_site_endpoints += 1
            else:
                raise ValidationError(
                    f"routed bit {bit} sink {instance!r}/{sink_pin} has no physical binding"
                )
            record = {
                "id": f"n{bit}:{instance}/{sink_pin}",
                "net": f"n{bit}",
                "mapped_bit": bit,
                "driver": {
                    "instance": driver, "pin": driver_pin, "site": driver_site,
                },
                "sink": {
                    "instance": instance, "pin": sink_pin, "site": sink_site,
                },
                "route_delay_ns": delay_ps / 1000.0,
                "binding": binding,
            }
            logical_endpoints += 1
            maximum = max(maximum, record["route_delay_ns"])
            if record_sink is None:
                assert records is not None
                records.append(record)
            else:
                record_sink(record)
    result = {
        "top": top,
        "part": route["part"],
        "qualification": {
            "provider": "rapidwright-lightweight",
            "analysis": "setup-route-only",
            "hold_analysis": "unavailable",
            "hard_block_clock_timing": "unqualified",
            "logic_coefficients_ps": dict(
                route["timing"]["logic_coefficients_ps"]
            ),
        },
        "clock_gates": [
            {
                "logical_cell": gate["logical_cell"],
                "physical_site": gate["physical_site"],
                "physical_type": gate["physical_type"],
                "input_site": gate.get("input_site", gate["physical_site"]),
                "implementation": gate.get("implementation", "direct-bufgce"),
                "cascaded_input": gate.get("cascaded_input", False),
                "qualification": gate["qualification"],
            }
            for gate in clock_gates
        ],
        "summary": {
            "logical_endpoints": logical_endpoints,
            "physical_route_sinks": physical_sinks,
            "exact_site_bindings": exact_site_bindings,
            "shared_site_bindings": shared_site_bindings,
            "intra_site_endpoints": intra_site_endpoints,
            "maximum_route_delay_ns": maximum,
        },
        "virtual_io": [
            {
                "id": item["id"],
                "direction": item["direction"],
                "logical_port": item["logical_port"],
                "logical_bit": item["logical_bit"],
                "p_site": item["p_site"],
                "n_site": item["n_site"],
                "route_delay_ns": float(item["route_delay_ps"]) / 1000.0,
                "measurement": "fixed-iob-routed-boundary",
            }
            for item in route.get("virtual_io", [])
        ],
    }
    if records is not None:
        result["endpoints"] = records
    return result


def _timing_payload_path(
    manifest_path: Path, value: Mapping[str, Any]
) -> tuple[Path, str, int]:
    payloads = value.get("payloads")
    descriptor = payloads.get("endpoints") if isinstance(payloads, Mapping) else None
    if not isinstance(descriptor, Mapping):
        raise ValidationError("XilinxRoutedTimingDB endpoint payload is invalid")
    relative = descriptor.get("path")
    digest = descriptor.get("sha256")
    records = descriptor.get("records")
    if (
        descriptor.get("format") != XILINX_ROUTED_TIMING_PAYLOAD_FORMAT
        or not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or Path(relative).name != relative
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or isinstance(records, bool)
        or not isinstance(records, int)
        or records < 0
    ):
        raise ValidationError("XilinxRoutedTimingDB endpoint payload is invalid")
    path = manifest_path.parent / relative
    if not path.is_file():
        raise ValidationError("XilinxRoutedTimingDB endpoint payload is missing")
    return path, digest, records


def iter_xilinx_routed_timing_endpoints(
    path: Path, value: Mapping[str, Any]
) -> Iterator[Mapping[str, Any]]:
    schema = value.get("schema")
    if schema == XILINX_ROUTED_TIMING_SCHEMA:
        endpoints = value.get("endpoints")
        if not isinstance(endpoints, list):
            raise ValidationError("XilinxRoutedTimingDB endpoints are invalid")
        yield from endpoints
        return
    if schema != XILINX_ROUTED_TIMING_STREAM_SCHEMA:
        raise ValidationError("XilinxRoutedTimingDB header is invalid")
    payload_path, expected_digest, expected_records = _timing_payload_path(
        path, value
    )
    digest = hashlib.sha256()
    count = 0
    with payload_path.open("rb") as stream:
        for raw in stream:
            digest.update(raw)
            if not raw.endswith(b"\n"):
                raise ValidationError(
                    "XilinxRoutedTimingDB endpoint payload is truncated"
                )
            try:
                endpoint = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValidationError(
                    f"XilinxRoutedTimingDB endpoint {count} is invalid"
                ) from error
            count += 1
            yield endpoint
    if count != expected_records:
        raise ValidationError(
            "XilinxRoutedTimingDB endpoint payload record count disagrees"
        )
    if digest.hexdigest() != expected_digest:
        raise ValidationError(
            "XilinxRoutedTimingDB endpoint payload digest disagrees"
        )


def build_xilinx_routed_timing(
    mapped_path: Path,
    packed_path: Path,
    placement_path: Path,
    route_path: Path,
    output_path: Path,
    *,
    route_validation: Optional[Mapping[str, Any]] = None,
    mapped_value: Optional[Mapping[str, Any]] = None,
    packed_value: Optional[Mapping[str, Any]] = None,
    placement_value: Optional[Mapping[str, Any]] = None,
    route_value: Optional[Mapping[str, Any]] = None,
    source_sha256: Optional[Mapping[str, str]] = None,
    timing_value_sink: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    # Load every large object at most once.  The standalone CLI used to parse
    # RouteDB once inside the validator and immediately parse it again here;
    # a real DLA partition makes that object close to one gigabyte.
    mapped = read_json(mapped_path) if mapped_value is None else mapped_value
    packed = read_json(packed_path) if packed_value is None else packed_value
    placement = (
        read_json(placement_path)
        if placement_value is None else placement_value
    )
    validate_xilinx_physical_handoff_pair(packed, placement)
    route = read_json(route_path) if route_value is None else route_value
    if route_validation is None:
        route_validation = validate_xilinx_route_db(
            route_path,
            mapped_path=mapped_path,
            packed_path=packed_path,
            placement_path=placement_path,
            mapped_value=mapped,
            packed_value=packed,
            source_sha256=source_sha256,
            _value=route,
        )
        route_sha256 = route_validation.get("route_sha256")
    else:
        # An externally supplied seal may outlive the in-memory producer, so
        # retain the mutation check for that public API.  The production
        # one-shot flow supplies both the seal and exact in-memory RouteDB and
        # therefore does not reread or rehash it.
        route_sha256 = (
            route_validation.get("route_sha256")
            if route_value is not None else _sha256(route_path)
        )
    if (
        route_validation.get("status") != "pass"
        or route_validation.get("schema")
        != "emuflow.xilinx-route-validation/v1"
        or route_validation.get("route_sha256") != route_sha256
    ):
        raise ValidationError("XilinxRouteDB validation seal is invalid")
    source_paths = {
        "mapped_sha256": mapped_path,
        "packed_sha256": packed_path,
        "placement_sha256": placement_path,
    }
    validated_sources = route_validation.get("source_sha256")
    if source_sha256 is not None:
        source_digests = dict(source_sha256)
    elif (
        isinstance(validated_sources, Mapping)
        and set(validated_sources) == set(source_paths)
    ):
        source_digests = dict(validated_sources)
    else:
        source_digests = {
            name: _sha256(path) for name, path in source_paths.items()
        }
    if set(source_digests) != set(source_paths):
        raise ValidationError("Xilinx routed timing source digests are incomplete")
    route_sources = route.get("source")
    if not isinstance(route_sources, dict) or any(
        route_sources.get(name) != digest
        for name, digest in source_digests.items()
    ):
        raise ValidationError("XilinxRouteDB validated source seal is stale")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    endpoint_path = output_path.with_name(output_path.name + ".endpoints.jsonl")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=endpoint_path.name + ".", suffix=".tmp", dir=output_path.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    endpoint_digest = hashlib.sha256()
    try:
        with temporary_path.open("wb") as stream:
            def emit_endpoint(record: Mapping[str, Any]) -> None:
                encoded = (
                    json.dumps(
                        record, sort_keys=True, separators=(",", ":"),
                    ).encode("utf-8")
                    + b"\n"
                )
                stream.write(encoded)
                endpoint_digest.update(encoded)

            payload = _build_payload(
                mapped,
                placement,
                route,
                route_path,
                record_sink=emit_endpoint,
            )
        os.replace(temporary_path, endpoint_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    value = {
        "schema": XILINX_ROUTED_TIMING_STREAM_SCHEMA,
        "status": "pass",
        "source": {
            **source_digests,
            "route_sha256": route_sha256,
        },
        "payloads": {
            "endpoints": {
                "format": XILINX_ROUTED_TIMING_PAYLOAD_FORMAT,
                "path": endpoint_path.name,
                "sha256": endpoint_digest.hexdigest(),
                "records": payload["summary"]["logical_endpoints"],
            }
        },
        **payload,
    }
    write_json(output_path, value, compact=True)
    if timing_value_sink is not None:
        timing_value_sink.clear()
        timing_value_sink.update(value)
    return {
        "status": "pass",
        "schema": "emuflow.xilinx-routed-timing-validation/v1",
        **value["summary"],
        "timing_sha256": _sha256(output_path),
    }


from emuflow.timing.xilinx_timing import validate_xilinx_routed_timing
