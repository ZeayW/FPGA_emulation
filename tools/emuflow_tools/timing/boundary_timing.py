"""Provider-neutral identities for physical multi-FPGA timing boundaries."""

from __future__ import annotations

from collections.abc import Sequence
from itertools import zip_longest
import math
from typing import Any, Dict, Iterator, Mapping

from emuflow.common.errors import ValidationError
from emuflow_tools.transport.netlist import transport_endpoint_port


BOUNDARY_IDENTITY_SCHEMA = "emuflow.boundary-identity/v1"
BOUNDARY_TIMING_SCHEMA = "emuflow.boundary-timing/v1"
_MEASUREMENTS_BY_SCOPE = {
    "partition-boundary": {
        "tx": "logical-source-to-tx-port",
        "rx": "rx-port-to-shadow-capture",
    },
    "board-integrated-interface": {
        "tx": "routed-launch-through-tx-boundary-to-interface-endpoint",
        "rx": "interface-startpoint-through-rx-boundary-to-shadow-capture",
    },
}


def _ordered_endpoint_records(
    value: Any, label: str
) -> Iterator[Mapping[str, Any]]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        raise ValidationError(f"{label} endpoints are invalid")
    # Sorting an already-materialized legacy list is an explicit compatibility
    # cost. Typed owners expose a checked lexical cursor and are not copied.
    iterable: Iterator[Mapping[str, Any]]
    if isinstance(value, list):
        if any(
            not isinstance(item, Mapping)
            or not isinstance(item.get("id"), str)
            or not item["id"]
            for item in value
        ):
            raise ValidationError(f"{label} endpoint is invalid")
        iterable = iter(sorted(value, key=lambda item: item["id"]))
    else:
        iterable = iter(value)
    previous = None
    for item in iterable:
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("id"), str)
            or not item["id"]
        ):
            raise ValidationError(f"{label} endpoint is invalid")
        if previous is not None and item["id"] <= previous:
            raise ValidationError(f"{label} endpoint is duplicated or unordered")
        previous = item["id"]
        yield item


def validate_boundary_identity_database(
    database: Mapping[str, Any],
    transport: Mapping[str, Any],
) -> Dict[str, Any]:
    if database.get("schema") != BOUNDARY_IDENTITY_SCHEMA:
        raise ValidationError("boundary identity schema is invalid")
    if database.get("status") != "pass":
        raise ValidationError("boundary identity database did not pass")
    if not isinstance(database.get("provider"), str) or not database["provider"]:
        raise ValidationError("boundary identity provider is invalid")
    for field in ("design", "platform", "fpga"):
        if database.get(field) != transport.get(field):
            raise ValidationError(f"boundary identity {field} disagrees")
    records = database.get("endpoints")
    sources = transport.get("endpoints")
    if (
        not isinstance(records, Sequence)
        or isinstance(records, (str, bytes, bytearray))
        or not isinstance(sources, Sequence)
        or isinstance(sources, (str, bytes, bytearray))
    ):
        raise ValidationError("boundary identity endpoints are invalid")

    missing = object()
    endpoint_count = 0
    tx_count = 0
    rx_count = 0
    for source, record in zip_longest(
        _ordered_endpoint_records(sources, "transport"),
        _ordered_endpoint_records(records, "boundary identity"),
        fillvalue=missing,
    ):
        if source is missing or record is missing or source["id"] != record["id"]:
            raise ValidationError("boundary identity endpoint coverage disagrees")
        endpoint_id = source["id"]
        for field in (
            "kind",
            "schedule_entry",
            "demand",
            "link",
            "peer",
            "lane",
            "logical_lane",
        ):
            if record.get(field) != source.get(field):
                raise ValidationError(
                    f"boundary identity {endpoint_id!r}.{field} disagrees"
                )
        if record.get("logical_net") != source.get("net") or record.get(
            "internal_signal"
        ) != source.get("signal"):
            raise ValidationError(
                f"boundary identity {endpoint_id!r} logical signal disagrees"
            )
        expected_source_class = (
            "dut-net"
            if source["kind"] == "tx" and source["signal"].startswith("net:")
            else "forwarded-shadow"
            if source["kind"] == "tx"
            else "captured-shadow"
        )
        if record.get("source_class") != expected_source_class:
            raise ValidationError(
                f"boundary identity {endpoint_id!r} source class disagrees"
            )
        merged = record.get("merged_ir")
        if not isinstance(merged, dict):
            raise ValidationError(
                f"boundary identity {endpoint_id!r} merged mapping is invalid"
            )
        if merged.get("external_port") != transport_endpoint_port(source) or (
            merged.get("external_port_bit") != source.get("lane")
        ):
            raise ValidationError(
                f"boundary identity {endpoint_id!r} external port disagrees"
            )
        if not isinstance(merged.get("external_net"), str) or not merged[
            "external_net"
        ]:
            raise ValidationError(
                f"boundary identity {endpoint_id!r} external net is invalid"
            )
        registers = merged.get("boundary_register_instances")
        if (
            not isinstance(registers, list)
            or len(set(registers)) != len(registers)
            or not all(isinstance(value, str) and value for value in registers)
        ):
            raise ValidationError(
                f"boundary identity {endpoint_id!r} register mapping is invalid"
            )
        needs_register = source["kind"] == "rx" or not source["signal"].startswith(
            "net:"
        )
        if needs_register and len(registers) != 1:
            raise ValidationError(
                f"boundary identity {endpoint_id!r} shadow register disagrees"
            )
        endpoint_count += 1
        if source["kind"] == "tx":
            tx_count += 1
        else:
            rx_count += 1
    coverage = database.get("coverage")
    expected_coverage = {
        "endpoints": endpoint_count,
        "tx": tx_count,
        "rx": rx_count,
        "external_port_nets": endpoint_count,
    }
    if not isinstance(coverage, dict) or coverage != expected_coverage:
        raise ValidationError("boundary identity coverage is invalid")
    return {
        "status": "pass",
        "fpga": database["fpga"],
        "endpoints": endpoint_count,
        "tx": coverage.get("tx"),
        "rx": coverage.get("rx"),
    }


def boundary_timing_header(
    identities: Mapping[str, Any],
    *,
    provider: str,
    qualification: str,
    measurement_scope: str = "partition-boundary",
) -> Dict[str, Any]:
    """Return the bounded logical header shared by JSON and typed owners."""

    if identities.get("schema") != BOUNDARY_IDENTITY_SCHEMA:
        raise ValidationError("boundary timing requires valid identities")
    if measurement_scope not in _MEASUREMENTS_BY_SCOPE:
        raise ValidationError("boundary timing measurement scope is invalid")
    if any(
        not isinstance(value, str) or not value
        for value in (provider, qualification)
    ):
        raise ValidationError("boundary timing provider is invalid")
    for field in ("design", "platform", "fpga"):
        if not isinstance(identities.get(field), str) or not identities[field]:
            raise ValidationError(f"boundary timing {field} is invalid")
    return {
        "schema": BOUNDARY_TIMING_SCHEMA,
        "status": "pass",
        "design": identities["design"],
        "platform": identities["platform"],
        "fpga": identities["fpga"],
        "provider": provider,
        "qualification": qualification,
        "measurement_scope": measurement_scope,
        "coverage": {"endpoints": 0, "tx": 0, "rx": 0},
    }


def validate_boundary_timing_database(
    database: Mapping[str, Any], identities: Mapping[str, Any]
) -> Dict[str, Any]:
    if database.get("schema") != BOUNDARY_TIMING_SCHEMA:
        raise ValidationError("boundary timing schema is invalid")
    if database.get("status") != "pass":
        raise ValidationError("boundary timing database did not pass")
    for field in ("provider", "qualification"):
        if not isinstance(database.get(field), str) or not database[field]:
            raise ValidationError(f"boundary timing {field} is invalid")
    for field in ("design", "platform", "fpga"):
        if database.get(field) != identities.get(field):
            raise ValidationError(f"boundary timing {field} disagrees")
    measurement_scope = database.get("measurement_scope", "partition-boundary")
    if measurement_scope not in _MEASUREMENTS_BY_SCOPE:
        raise ValidationError("boundary timing measurement scope is invalid")
    records = database.get("endpoints")
    missing = object()
    endpoint_count = 0
    tx_count = 0
    rx_count = 0
    maximum_delay = 0.0
    for identity, item in zip_longest(
        _ordered_endpoint_records(
            identities.get("endpoints"), "boundary identity"
        ),
        _ordered_endpoint_records(records, "boundary timing"),
        fillvalue=missing,
    ):
        if identity is missing or item is missing or identity["id"] != item["id"]:
            raise ValidationError("boundary timing endpoint coverage disagrees")
        endpoint_id = item["id"]
        if item.get("kind") not in {"tx", "rx"}:
            raise ValidationError("boundary timing endpoint identity is invalid")
        delay = item.get("delay_ns")
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(float(delay))
            or float(delay) < 0.0
        ):
            raise ValidationError("boundary timing endpoint delay is invalid")
        if (
            item["kind"] != identity["kind"]
            or item.get("schedule_entry") != identity.get("schedule_entry")
            or item.get("measurement")
            != _MEASUREMENTS_BY_SCOPE[measurement_scope][item["kind"]]
            or not isinstance(item.get("start_object"), str)
            or not item["start_object"]
            or not isinstance(item.get("end_object"), str)
            or not item["end_object"]
        ):
            raise ValidationError(
                "boundary timing endpoint traceability is invalid"
            )
        endpoint_count += 1
        if item["kind"] == "tx":
            tx_count += 1
        else:
            rx_count += 1
        maximum_delay = max(maximum_delay, float(delay))
    coverage = database.get("coverage")
    expected_coverage = {
        "endpoints": endpoint_count,
        "tx": tx_count,
        "rx": rx_count,
    }
    if not isinstance(coverage, dict) or coverage != expected_coverage:
        raise ValidationError("boundary timing coverage is invalid")
    return {
        "status": "pass",
        "fpga": database["fpga"],
        "endpoints": endpoint_count,
        "maximum_delay_ns": maximum_delay,
    }
