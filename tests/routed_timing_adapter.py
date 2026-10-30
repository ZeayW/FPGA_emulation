"""Bind RapidWright routed site-pin delays to mapped logical endpoints."""

from __future__ import annotations

import math
import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional

from emuflow.common.errors import ValidationError
from emuflow.common.json_io import file_sha256
from emuflow_tools.document_io import read_json
from emuflow_tools.physical.xilinx_physical_handoff import validate_xilinx_physical_handoff_pair
from emuflow_tools.physical.rapidwright.xilinx_rwroute import validate_xilinx_route_db


XILINX_ROUTED_TIMING_SCHEMA = "emuflow.xilinx-routed-timing/v1"
XILINX_ROUTED_TIMING_STREAM_SCHEMA = "emuflow.xilinx-routed-timing/v2"
XILINX_ROUTED_TIMING_PAYLOAD_FORMAT = "jsonl-object/v1"


def _sha256(path: Path) -> str:
    return file_sha256(path)


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
    from tests.routed_endpoint_adapter import bind_routed_timing_sources
    from emuflow.storage.store_publication import require_store_sources_unchanged
    sources = bind_routed_timing_sources(path, value)
    try:
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
    finally:
        require_store_sources_unchanged(sources)


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
    from contextlib import ExitStack
    from emuflow_tools.physical.xilinx_netlist import IndexedXilinxMappedNetlist
    from emuflow_tools.physical.xilinx_physical_handoff import open_xilinx_physical_handoff
    from tests.routed_endpoint_adapter import build_native_routed_endpoints
    from emuflow.storage.store_publication import bind_store_source, require_store_sources_unchanged

    with ExitStack() as lifetime:
        mapped = lifetime.enter_context(IndexedXilinxMappedNetlist(mapped_path)) if mapped_value is None else mapped_value
        if getattr(mapped, "store_path", None) is None:
            raise ValidationError("routed endpoint production requires the current typed mapped index")
        if packed_value is None or placement_value is None:
            if Path(packed_path).resolve() != Path(placement_path).resolve():
                raise ValidationError("routed timing requires one canonical physical handoff")
            opened_packed, opened_placement = open_xilinx_physical_handoff(packed_path)
            packed = opened_packed if packed_value is None else packed_value
            placement = opened_placement if placement_value is None else placement_value
        else:
            packed, placement = packed_value, placement_value
        validate_xilinx_physical_handoff_pair(packed, placement)
        route_source = bind_store_source(route_path, "xilinx-route-manifest-v2")
        route = read_json(route_path) if route_value is None else route_value
        require_store_sources_unchanged((route_source,))
        if route.get("schema") != "emuflow.xilinx-route-db/v2":
            raise ValidationError("routed endpoint production requires the current route streams")
        if route_validation is None:
            route_validation = validate_xilinx_route_db(
                route_path,
                mapped_path=mapped_path,
                packed_path=packed_path,
                placement_path=placement_path,
                mapped_value=mapped,
                packed_value=packed,
                source_sha256=source_sha256,
                _route_source=route_source,
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
        return build_native_routed_endpoints(mapped, packed, placement, route_path, route, output_path,
                                             {**source_digests, "route_sha256": route_sha256},
                                             route_validation, timing_value_sink)


def validate_xilinx_routed_timing(
    path: Path,
    *,
    mapped_path: Optional[Path] = None,
    packed_path: Optional[Path] = None,
    placement_path: Optional[Path] = None,
    route_path: Optional[Path] = None,
    source_sha256: Optional[Mapping[str, str]] = None,
    _value: Optional[Mapping[str, Any]] = None,
    _timing_sha256: Optional[str] = None,
    _endpoint_consumer: Optional[Callable[[Mapping[str, Any]], None]] = None,
) -> Dict[str, Any]:
    value = read_json(path) if _value is None else _value
    if (
        value.get("schema")
        not in {XILINX_ROUTED_TIMING_SCHEMA, XILINX_ROUTED_TIMING_STREAM_SCHEMA}
        or value.get("status") != "pass"
    ):
        raise ValidationError("XilinxRoutedTimingDB header is invalid")
    sources = {
        "mapped_sha256": mapped_path,
        "packed_sha256": packed_path,
        "placement_sha256": placement_path,
        "route_sha256": route_path,
    }
    required_source_keys = {
        key for key, source_path in sources.items() if source_path is not None
    }
    if source_sha256 is not None and set(source_sha256) != required_source_keys:
        raise ValidationError(
            "XilinxRoutedTimingDB validated source digests are incomplete"
        )
    for key, source_path in sources.items():
        digest = value.get("source", {}).get(key)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValidationError(f"XilinxRoutedTimingDB source.{key} is invalid")
        expected_digest = (
            source_sha256.get(key)
            if source_sha256 is not None and source_path is not None
            else _sha256(source_path) if source_path is not None else None
        )
        if source_path is not None and digest != expected_digest:
            raise ValidationError(f"XilinxRoutedTimingDB source.{key} disagrees")
    seen = set()
    maximum = 0.0
    endpoint_count = 0
    for index, endpoint in enumerate(
        iter_xilinx_routed_timing_endpoints(path, value)
    ):
        endpoint_id = endpoint.get("id") if isinstance(endpoint, dict) else None
        delay = endpoint.get("route_delay_ns") if isinstance(endpoint, dict) else None
        if not isinstance(endpoint_id, str) or endpoint_id in seen:
            raise ValidationError(f"XilinxRoutedTimingDB endpoint {index} identity is invalid")
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(float(delay)) or float(delay) < 0.0:
            raise ValidationError(f"XilinxRoutedTimingDB endpoint {index} delay is invalid")
        seen.add(endpoint_id)
        maximum = max(maximum, float(delay))
        endpoint_count += 1
        if _endpoint_consumer is not None:
            _endpoint_consumer(endpoint)
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
    seen_virtual_io = set()
    seen_virtual_ports = set()
    virtual_io = value.get("virtual_io", [])
    if not isinstance(virtual_io, Sequence) or isinstance(virtual_io, (str, bytes)):
        raise ValidationError("XilinxRoutedTimingDB virtual I/O collection is invalid")
    for index, item in enumerate(virtual_io):
        identity = item.get("id") if isinstance(item, dict) else None
        direction = item.get("direction") if isinstance(item, dict) else None
        port = item.get("logical_port") if isinstance(item, dict) else None
        bit = item.get("logical_bit") if isinstance(item, dict) else None
        p_site = item.get("p_site") if isinstance(item, dict) else None
        n_site = item.get("n_site") if isinstance(item, dict) else None
        delay = item.get("route_delay_ns") if isinstance(item, dict) else None
        port_key = (port, bit, direction)
        if (
            not isinstance(identity, str)
            or not identity
            or identity in seen_virtual_io
            or direction not in {"tx", "rx"}
            or not isinstance(port, str)
            or not port
            or isinstance(bit, bool)
            or not isinstance(bit, int)
            or bit < 0
            or port_key in seen_virtual_ports
            or not isinstance(p_site, str)
            or not p_site
            or not isinstance(n_site, str)
            or not n_site
            or p_site == n_site
            or isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(float(delay))
            or float(delay) < 0.0
            or item.get("measurement") != "fixed-iob-routed-boundary"
        ):
            raise ValidationError(
                f"XilinxRoutedTimingDB virtual I/O record {index} is invalid"
            )
        seen_virtual_io.add(identity)
        seen_virtual_ports.add(port_key)
    summary = value.get("summary", {})
    if summary.get("logical_endpoints") != endpoint_count or not math.isclose(
        float(summary.get("maximum_route_delay_ns", -1.0)), maximum,
        rel_tol=1.0e-9, abs_tol=1.0e-12,
    ):
        raise ValidationError("XilinxRoutedTimingDB summary disagrees")
    return {
        "status": "pass",
        "schema": "emuflow.xilinx-routed-timing-validation/v1",
        **summary,
        "timing_sha256": _timing_sha256 or _sha256(path),
    }
