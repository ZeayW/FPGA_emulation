"""Provider-neutral identities for physical multi-FPGA timing boundaries."""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

from .errors import ValidationError
from .ir import EmuIR
from .indexed_physical_ir import IndexedPhysicalIR
from .netlist import TRANSPORT_ENDPOINTS_SCHEMA, transport_endpoint_port


BOUNDARY_IDENTITY_SCHEMA = "emuflow.boundary-identity/v1"
BOUNDARY_TIMING_SCHEMA = "emuflow.boundary-timing/v1"


def _top_net_index(ir: EmuIR) -> Dict[tuple[str, int], str]:
    if isinstance(ir, IndexedPhysicalIR):
        result: Dict[tuple[str, int], str] = {}
        for row in ir.connection.execute(
            """SELECT p.name,e.bit,n.id FROM endpoint_records e
            JOIN pin_names p ON p.seq=e.port_seq
            JOIN net_records n ON n.seq=e.net_seq
            WHERE e.instance_seq IS NULL ORDER BY e.net_seq,e.role,e.ordinal"""
        ):
            key = (row[0], int(row[1]))
            previous = result.setdefault(key, row[2])
            if previous != row[2]:
                raise ValidationError(
                    f"physical boundary port {key!r} is on multiple nets"
                )
        return result
    result: Dict[tuple[str, int], str] = {}
    for net in ir.value["nets"]:
        for collection in ("drivers", "sinks"):
            for endpoint in net[collection]:
                if endpoint["instance"] is not None:
                    continue
                key = (endpoint["port"], endpoint["bit"])
                previous = result.setdefault(key, net["id"])
                if previous != net["id"]:
                    raise ValidationError(
                        f"physical boundary port {key!r} is on multiple nets"
                    )
    return result


def build_boundary_identity_database(
    transport: Mapping[str, Any],
    merged_ir: EmuIR,
    transport_ir: Optional[EmuIR] = None,
) -> Dict[str, Any]:
    """Bind every logical TDM endpoint to a stable merged-EmuIR port/net.

    This database is deliberately timing-engine neutral.  A physical backend
    consumes these identities after routing and emits delay measurements keyed
    by the same endpoint IDs.
    """
    if transport.get("schema") != TRANSPORT_ENDPOINTS_SCHEMA:
        raise ValidationError("invalid transport endpoint schema")
    fpga = transport.get("fpga")
    if not isinstance(fpga, str) or not merged_ir.value["design"]["name"].endswith(
        f"__{fpga}"
    ):
        raise ValidationError("transport and merged EmuIR identities disagree")

    ports = {port["id"]: port for port in merged_ir.value["ports"]}
    nets = (
        merged_ir.nets
        if isinstance(merged_ir, IndexedPhysicalIR)
        else {net["id"]: net for net in merged_ir.value["nets"]}
    )
    top_nets = _top_net_index(merged_ir)
    shadow_registers: Dict[str, list[str]] = {}
    if transport_ir is not None:
        if isinstance(transport_ir, IndexedPhysicalIR):
            transport_top_net_ids: Dict[tuple[str, int], str] = {}
            for row in transport_ir.connection.execute(
                """SELECT pin.name,e.bit,n.id FROM endpoint_records e
                JOIN pin_names pin ON pin.seq=e.port_seq
                JOIN net_records n ON n.seq=e.net_seq
                WHERE e.instance_seq IS NULL AND e.role=1
                ORDER BY n.seq,e.ordinal"""
            ):
                transport_top_net_ids[(str(row[0]), int(row[1]))] = str(row[2])
            def synthesized_shadow_net(index: int) -> Optional[Mapping[str, Any]]:
                net_id = transport_top_net_ids.get(("shadow_values", index))
                return None if net_id is None else transport_ir.nets[net_id]
        else:
            transport_top_nets: Dict[
                tuple[str, int], Mapping[str, Any]
            ] = {}
            for net in transport_ir.value["nets"]:
                for endpoint in net["sinks"]:
                    if endpoint["instance"] is None:
                        transport_top_nets[
                            (endpoint["port"], endpoint["bit"])
                        ] = net
            def synthesized_shadow_net(index: int) -> Optional[Mapping[str, Any]]:
                return transport_top_nets.get(("shadow_values", index))
        for item in transport.get("shadow_signals", []):
            net = synthesized_shadow_net(item["index"])
            if net is None:
                raise ValidationError(
                    f"transport shadow {item['signal']!r} has no synthesized net"
                )
            shadow_registers[item["signal"]] = sorted(
                {
                    f"__emuflow_transport__/{driver['instance']}"
                    for driver in net["drivers"]
                    if driver["instance"] is not None
                }
            )
    records = []
    endpoint_ids = set()
    for index, endpoint in enumerate(transport.get("endpoints", [])):
        endpoint_id = endpoint.get("id")
        kind = endpoint.get("kind")
        if not isinstance(endpoint_id, str) or not endpoint_id:
            raise ValidationError(
                f"transport endpoint {index} has an invalid ID"
            )
        if endpoint_id in endpoint_ids:
            raise ValidationError(
                f"duplicate transport endpoint ID {endpoint_id!r}"
            )
        endpoint_ids.add(endpoint_id)
        if kind not in {"tx", "rx"}:
            raise ValidationError(
                f"transport endpoint {endpoint_id!r} has invalid kind"
            )
        for field in ("schedule_entry", "demand", "link", "peer"):
            value = endpoint.get(field)
            if not isinstance(value, str) or not value:
                raise ValidationError(
                    f"transport endpoint {endpoint_id!r} has invalid {field}"
                )
        lane = endpoint.get("lane")
        if isinstance(lane, bool) or not isinstance(lane, int) or lane < 0:
            raise ValidationError(
                f"transport endpoint {endpoint_id!r} has invalid lane"
            )
        logical_lane = endpoint.get("logical_lane")
        if (
            isinstance(logical_lane, bool)
            or not isinstance(logical_lane, int)
            or logical_lane < 0
        ):
            raise ValidationError(
                f"transport endpoint {endpoint_id!r} has invalid logical_lane"
            )
        port = transport_endpoint_port(endpoint)
        expected_direction = "output" if kind == "tx" else "input"
        if port not in ports or ports[port].get("direction") != expected_direction:
            raise ValidationError(
                f"transport endpoint {endpoint_id!r} cannot bind {port}[{lane}]"
            )
        external_net = top_nets.get((port, lane))
        if external_net is None:
            raise ValidationError(
                f"transport endpoint {endpoint_id!r} has no merged-IR port net"
            )

        logical_net = endpoint.get("net")
        signal = endpoint.get("signal")
        if not isinstance(logical_net, str) or not logical_net:
            raise ValidationError(
                f"transport endpoint {endpoint_id!r} has no logical net"
            )
        if not isinstance(signal, str) or not signal:
            raise ValidationError(
                f"transport endpoint {endpoint_id!r} has no internal signal"
            )
        local_net = nets.get(logical_net)
        boundary_registers = list(shadow_registers.get(signal, []))
        if not boundary_registers and local_net is not None:
            boundary_registers = sorted(
                {
                    item["instance"]
                    for item in local_net["drivers"]
                    if isinstance(item.get("instance"), str)
                    and item["instance"].startswith(
                        "__emuflow_transport__/"
                    )
                }
            )
        if kind == "tx" and signal.startswith("net:") and local_net is None:
            raise ValidationError(
                f"TX endpoint {endpoint_id!r} DUT source net is absent"
            )
        if (kind == "rx" or not signal.startswith("net:")) and len(
            boundary_registers
        ) != 1:
            raise ValidationError(
                f"transport endpoint {endpoint_id!r} lacks one shadow register"
            )
        source_class = (
            "dut-net"
            if kind == "tx" and signal.startswith("net:")
            else "forwarded-shadow"
            if kind == "tx"
            else "captured-shadow"
        )
        records.append(
            {
                "id": endpoint_id,
                "kind": kind,
                "schedule_entry": endpoint.get("schedule_entry"),
                "demand": endpoint.get("demand"),
                "logical_net": logical_net,
                "link": endpoint.get("link"),
                "peer": endpoint.get("peer"),
                "lane": lane,
                "logical_lane": endpoint.get("logical_lane"),
                "internal_signal": signal,
                "source_class": source_class,
                "merged_ir": {
                    "external_port": port,
                    "external_port_bit": lane,
                    "external_net": external_net,
                    "logical_net": logical_net if local_net is not None else None,
                    "boundary_register_instances": boundary_registers,
                },
            }
        )

    return {
        "schema": BOUNDARY_IDENTITY_SCHEMA,
        "status": "pass",
        "design": transport["design"],
        "platform": transport["platform"],
        "fpga": transport["fpga"],
        "provider": "phase6-endpoint-to-merged-emuir-identity-v1",
        "coverage": {
            "endpoints": len(records),
            "tx": sum(item["kind"] == "tx" for item in records),
            "rx": sum(item["kind"] == "rx" for item in records),
            "external_port_nets": len(records),
        },
        "endpoints": sorted(records, key=lambda item: item["id"]),
    }


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
    expected = {
        endpoint["id"]: endpoint
        for endpoint in transport.get("endpoints", [])
    }
    records = database.get("endpoints")
    if not isinstance(records, list):
        raise ValidationError("boundary identity endpoints are invalid")
    actual = {}
    for item in records:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValidationError("boundary identity endpoint is invalid")
        if item["id"] in actual:
            raise ValidationError("boundary identity endpoint is duplicated")
        actual[item["id"]] = item
    if set(actual) != set(expected):
        raise ValidationError("boundary identity endpoint coverage disagrees")
    for endpoint_id, source in expected.items():
        record = actual[endpoint_id]
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
    coverage = database.get("coverage")
    expected_coverage = {
        "endpoints": len(expected),
        "tx": sum(item["kind"] == "tx" for item in expected.values()),
        "rx": sum(item["kind"] == "rx" for item in expected.values()),
        "external_port_nets": len(expected),
    }
    if not isinstance(coverage, dict) or coverage != expected_coverage:
        raise ValidationError("boundary identity coverage is invalid")
    return {
        "status": "pass",
        "fpga": database["fpga"],
        "endpoints": len(expected),
        "tx": coverage.get("tx"),
        "rx": coverage.get("rx"),
    }


def build_boundary_timing_database(
    identities: Mapping[str, Any],
    measurements: Mapping[str, Mapping[str, Any]],
    *,
    provider: str,
    qualification: str,
    measurement_scope: str = "partition-boundary",
) -> Dict[str, Any]:
    """Build a complete endpoint-keyed physical timing database."""
    if identities.get("schema") != BOUNDARY_IDENTITY_SCHEMA:
        raise ValidationError("boundary timing requires valid identities")
    identity_records = {
        item["id"]: item for item in identities.get("endpoints", [])
    }
    if set(measurements) != set(identity_records):
        missing = sorted(set(identity_records) - set(measurements))
        extra = sorted(set(measurements) - set(identity_records))
        raise ValidationError(
            "boundary timing measurement coverage disagrees: "
            f"missing={len(missing)}, extra={len(extra)}"
        )
    measurements_by_scope = {
        "partition-boundary": {
            "tx": "logical-source-to-tx-port",
            "rx": "rx-port-to-shadow-capture",
        },
        "board-integrated-interface": {
            "tx": "routed-launch-through-tx-boundary-to-interface-endpoint",
            "rx": "interface-startpoint-through-rx-boundary-to-shadow-capture",
        },
    }
    if measurement_scope not in measurements_by_scope:
        raise ValidationError("boundary timing measurement scope is invalid")
    records = []
    for endpoint_id in sorted(identity_records):
        identity = identity_records[endpoint_id]
        measurement = measurements[endpoint_id]
        delay = measurement.get("delay_ns")
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(float(delay))
            or float(delay) < 0.0
        ):
            raise ValidationError(
                f"boundary timing {endpoint_id!r} delay is invalid"
            )
        start = measurement.get("start_object")
        end = measurement.get("end_object")
        if not isinstance(start, str) or not start or not isinstance(end, str) or not end:
            raise ValidationError(
                f"boundary timing {endpoint_id!r} object trace is invalid"
            )
        records.append(
            {
                "id": endpoint_id,
                "kind": identity["kind"],
                "schedule_entry": identity["schedule_entry"],
                "delay_ns": float(delay),
                "start_object": start,
                "end_object": end,
                "measurement": measurements_by_scope[measurement_scope][
                    identity["kind"]
                ],
            }
        )
    return {
        "schema": BOUNDARY_TIMING_SCHEMA,
        "status": "pass",
        "design": identities["design"],
        "platform": identities["platform"],
        "fpga": identities["fpga"],
        "provider": provider,
        "qualification": qualification,
        "measurement_scope": measurement_scope,
        "coverage": {
            "endpoints": len(records),
            "tx": sum(item["kind"] == "tx" for item in records),
            "rx": sum(item["kind"] == "rx" for item in records),
        },
        "endpoints": records,
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
    measurements_by_scope = {
        "partition-boundary": {
            "tx": "logical-source-to-tx-port",
            "rx": "rx-port-to-shadow-capture",
        },
        "board-integrated-interface": {
            "tx": "routed-launch-through-tx-boundary-to-interface-endpoint",
            "rx": "interface-startpoint-through-rx-boundary-to-shadow-capture",
        },
    }
    if measurement_scope not in measurements_by_scope:
        raise ValidationError("boundary timing measurement scope is invalid")
    expected = {
        item["id"]: item for item in identities.get("endpoints", [])
    }
    records = database.get("endpoints")
    if not isinstance(records, list):
        raise ValidationError("boundary timing endpoints are invalid")
    actual: Dict[str, str] = {}
    for item in records:
        if not isinstance(item, dict):
            raise ValidationError("boundary timing endpoint is invalid")
        endpoint_id = item.get("id")
        if endpoint_id in actual or item.get("kind") not in {"tx", "rx"}:
            raise ValidationError("boundary timing endpoint identity is invalid")
        actual[endpoint_id] = item["kind"]
        delay = item.get("delay_ns")
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(float(delay))
            or float(delay) < 0.0
        ):
            raise ValidationError("boundary timing endpoint delay is invalid")
        identity = expected.get(endpoint_id)
        if (
            identity is None
            or item["kind"] != identity["kind"]
            or item.get("schedule_entry") != identity.get("schedule_entry")
            or item.get("measurement")
            != measurements_by_scope[measurement_scope][item["kind"]]
            or not isinstance(item.get("start_object"), str)
            or not item["start_object"]
            or not isinstance(item.get("end_object"), str)
            or not item["end_object"]
        ):
            raise ValidationError(
                "boundary timing endpoint traceability is invalid"
            )
    if actual != {key: value["kind"] for key, value in expected.items()}:
        raise ValidationError("boundary timing endpoint coverage disagrees")
    coverage = database.get("coverage")
    expected_coverage = {
        "endpoints": len(expected),
        "tx": sum(item["kind"] == "tx" for item in expected.values()),
        "rx": sum(item["kind"] == "rx" for item in expected.values()),
    }
    if not isinstance(coverage, dict) or coverage != expected_coverage:
        raise ValidationError("boundary timing coverage is invalid")
    return {
        "status": "pass",
        "fpga": database["fpga"],
        "endpoints": len(expected),
        "maximum_delay_ns": max(
            (float(item["delay_ns"]) for item in records), default=0.0
        ),
    }
