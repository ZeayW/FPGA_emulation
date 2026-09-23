"""Bind RapidWright routed site-pin delays to mapped logical endpoints."""

from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import ValidationError
from .io import read_json, write_json
from .xilinx_rwroute import validate_xilinx_route_db


XILINX_ROUTED_TIMING_SCHEMA = "emuflow.xilinx-routed-timing/v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        cells = module.get("cells") if isinstance(module, dict) else None
        if isinstance(cells, dict) and set(cells) == instances:
            matches.append((name, module))
    if len(matches) != 1:
        raise ValidationError("mapped JSON does not uniquely match Xilinx placement")
    return matches[0]


def _logical_pin(port: str, index: int, width: int) -> str:
    return port if width == 1 else f"{port}[{index}]"


def _build_payload(
    mapped: Mapping[str, Any], placement: Mapping[str, Any], route: Mapping[str, Any]
) -> Dict[str, Any]:
    sites = _placement_sites(placement)
    top, module = _mapped_module(mapped, set(sites))
    cells = module["cells"]
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

    route_by_bit: Dict[int, Mapping[str, Any]] = {}
    for net in route.get("nets", []):
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
        match = re.fullmatch(r"n([0-9]+)", str(net.get("net", "")))
        if match is None:
            raise ValidationError("Xilinx route net does not encode a mapped bit")
        bit = int(match.group(1))
        if bit in route_by_bit:
            raise ValidationError("Xilinx route repeats a mapped bit")
        route_by_bit[bit] = net

    records = []
    physical_sinks = 0
    exact_site_bindings = 0
    shared_site_bindings = 0
    intra_site_endpoints = 0
    for bit in sorted(route_by_bit):
        logical = endpoints.get(bit, [])
        drivers = [value for value in logical if value[2] == "driver"]
        sinks = [value for value in logical if value[2] == "sink"]
        if len(drivers) != 1 or not sinks:
            raise ValidationError(f"routed bit {bit} lacks one mapped driver and sinks")
        driver, driver_pin, _ = drivers[0]
        driver_site = sites[driver]
        route_net = route_by_bit[bit]
        route_sources = [pin for pin in route_net["pins"] if pin["is_output"]]
        if len(route_sources) != 1 or route_sources[0]["site"] != driver_site:
            raise ValidationError(f"routed bit {bit} driver site disagrees")
        delay_by_site: Dict[str, list[float]] = defaultdict(list)
        for pin in route_net["pins"]:
            if not pin["is_output"]:
                delay_by_site[pin["site"]].append(float(pin["route_delay_ps"]))
                physical_sinks += 1
        logical_sites = {sites[instance] for instance, _pin, _role in sinks}
        if not set(delay_by_site).issubset(logical_sites):
            raise ValidationError(f"routed bit {bit} has an unbound physical sink site")
        for instance, sink_pin, _ in sorted(sinks):
            sink_site = sites[instance]
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
            records.append(
                {
                    "id": f"n{bit}:{instance}/{sink_pin}",
                    "net": f"n{bit}",
                    "mapped_bit": bit,
                    "driver": {"instance": driver, "pin": driver_pin, "site": driver_site},
                    "sink": {"instance": instance, "pin": sink_pin, "site": sink_site},
                    "route_delay_ns": delay_ps / 1000.0,
                    "binding": binding,
                }
            )
    maximum = max((item["route_delay_ns"] for item in records), default=0.0)
    return {
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
        "endpoints": records,
        "summary": {
            "logical_endpoints": len(records),
            "physical_route_sinks": physical_sinks,
            "exact_site_bindings": exact_site_bindings,
            "shared_site_bindings": shared_site_bindings,
            "intra_site_endpoints": intra_site_endpoints,
            "maximum_route_delay_ns": maximum,
        },
    }


def build_xilinx_routed_timing(
    mapped_path: Path,
    packed_path: Path,
    placement_path: Path,
    route_path: Path,
    output_path: Path,
) -> Dict[str, Any]:
    validate_xilinx_route_db(
        route_path,
        mapped_path=mapped_path,
        packed_path=packed_path,
        placement_path=placement_path,
    )
    payload = _build_payload(
        read_json(mapped_path), read_json(placement_path), read_json(route_path)
    )
    value = {
        "schema": XILINX_ROUTED_TIMING_SCHEMA,
        "status": "pass",
        "source": {
            "mapped_sha256": _sha256(mapped_path),
            "packed_sha256": _sha256(packed_path),
            "placement_sha256": _sha256(placement_path),
            "route_sha256": _sha256(route_path),
        },
        **payload,
    }
    write_json(output_path, value, compact=True)
    return validate_xilinx_routed_timing(
        output_path,
        mapped_path=mapped_path,
        packed_path=packed_path,
        placement_path=placement_path,
        route_path=route_path,
    )


def validate_xilinx_routed_timing(
    path: Path,
    *,
    mapped_path: Optional[Path] = None,
    packed_path: Optional[Path] = None,
    placement_path: Optional[Path] = None,
    route_path: Optional[Path] = None,
) -> Dict[str, Any]:
    value = read_json(path)
    if value.get("schema") != XILINX_ROUTED_TIMING_SCHEMA or value.get("status") != "pass":
        raise ValidationError("XilinxRoutedTimingDB header is invalid")
    sources = {
        "mapped_sha256": mapped_path,
        "packed_sha256": packed_path,
        "placement_sha256": placement_path,
        "route_sha256": route_path,
    }
    for key, source_path in sources.items():
        digest = value.get("source", {}).get(key)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValidationError(f"XilinxRoutedTimingDB source.{key} is invalid")
        if source_path is not None and digest != _sha256(source_path):
            raise ValidationError(f"XilinxRoutedTimingDB source.{key} disagrees")
    endpoints = value.get("endpoints")
    if not isinstance(endpoints, list):
        raise ValidationError("XilinxRoutedTimingDB endpoints are invalid")
    seen = set()
    maximum = 0.0
    for index, endpoint in enumerate(endpoints):
        endpoint_id = endpoint.get("id") if isinstance(endpoint, dict) else None
        delay = endpoint.get("route_delay_ns") if isinstance(endpoint, dict) else None
        if not isinstance(endpoint_id, str) or endpoint_id in seen:
            raise ValidationError(f"XilinxRoutedTimingDB endpoint {index} identity is invalid")
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(float(delay)) or float(delay) < 0.0:
            raise ValidationError(f"XilinxRoutedTimingDB endpoint {index} delay is invalid")
        seen.add(endpoint_id)
        maximum = max(maximum, float(delay))
    qualification = value.get("qualification")
    if not isinstance(qualification, dict):
        raise ValidationError("XilinxRoutedTimingDB qualification is invalid")
    coefficients = qualification.get("logic_coefficients_ps")
    expected_coefficients = {
        "ff_clock_to_q", "carry_co", "lut_a1", "lut_a2", "lut_a3",
        "lut_a4", "lut_a5", "lut_a6",
    }
    if not isinstance(coefficients, dict) or set(coefficients) != expected_coefficients:
        raise ValidationError("XilinxRoutedTimingDB logic coefficients are invalid")
    for name, delay in coefficients.items():
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(float(delay))
            or float(delay) < 0.0
        ):
            raise ValidationError(
                f"XilinxRoutedTimingDB logic coefficient {name!r} is invalid"
            )
    summary = value.get("summary", {})
    if summary.get("logical_endpoints") != len(endpoints) or not math.isclose(
        float(summary.get("maximum_route_delay_ns", -1.0)), maximum,
        rel_tol=1.0e-9, abs_tol=1.0e-12,
    ):
        raise ValidationError("XilinxRoutedTimingDB summary disagrees")
    return {
        "status": "pass",
        "schema": "emuflow.xilinx-routed-timing-validation/v1",
        **summary,
        "timing_sha256": _sha256(path),
    }
