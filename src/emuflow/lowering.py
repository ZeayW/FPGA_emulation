from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .boundary_timing import (
    build_boundary_identity_database,
    validate_boundary_identity_database,
)
from .errors import ValidationError
from .io import read_json, write_json
from .ir import EMUIR_SCHEMA, EmuIR
from .emuir_storage import open_emuir_sqlite_read_only
from .indexed_physical_ir import IndexedPhysicalIR
from .netlist import FPGA_NETLIST_SCHEMA, TRANSPORT_ENDPOINTS_SCHEMA
from .placement_ir_storage import write_placement_emuir_from_split
from .split_netlist_storage import SplitNetlistView
from .transport_storage import read_transport


PLACEMENT_IR_REPORT_SCHEMA = "emuflow.placement-ir-report/v1"


def _top_net(
    index: Mapping[Tuple[str, int, str], List[Dict[str, Any]]],
    port: str,
    bit: int,
    collection: str,
) -> Dict[str, Any]:
    matches = index.get((port, bit, collection), [])
    if len(matches) != 1:
        raise ValidationError(
            f"transport EmuIR {port}[{bit}] expected one {collection[:-1]} "
            f"net, found {len(matches)}"
        )
    return matches[0]


def build_placement_ir(
    netlist: Mapping[str, Any],
    transport: Mapping[str, Any],
    transport_ir: EmuIR,
) -> EmuIR:
    if netlist.get("schema") != FPGA_NETLIST_SCHEMA:
        raise ValidationError("invalid per-FPGA netlist schema")
    if transport.get("schema") != TRANSPORT_ENDPOINTS_SCHEMA:
        raise ValidationError("invalid transport endpoint schema")
    if netlist.get("fpga") != transport.get("fpga"):
        raise ValidationError("netlist and transport target different FPGAs")

    top_net_index: Dict[
        Tuple[str, int, str], List[Dict[str, Any]]
    ] = {}
    for net in transport_ir.value["nets"]:
        for collection in ("drivers", "sinks"):
            for endpoint in net[collection]:
                if endpoint["instance"] is not None:
                    continue
                key = (
                    endpoint["port"],
                    endpoint["bit"],
                    collection,
                )
                top_net_index.setdefault(key, []).append(net)

    namespace = "__emuflow_transport__/"
    instance_map = {
        instance["id"]: f"{namespace}{instance['id']}"
        for instance in transport_ir.value["instances"]
    }

    def remap_endpoint(endpoint: Mapping[str, Any]) -> Dict[str, Any]:
        value = dict(endpoint)
        if value["instance"] is not None:
            value["instance"] = instance_map[value["instance"]]
        return value

    local_nets: Dict[str, Dict[str, Any]] = {}
    rx_endpoint_to_signal = {
        endpoint["id"]: endpoint["signal"]
        for endpoint in transport["endpoints"]
        if endpoint["kind"] == "rx"
    }
    shadow_signal_to_logical_net = {
        endpoint["signal"]: endpoint["net"]
        for endpoint in transport["endpoints"]
        if endpoint["kind"] == "rx"
    }
    shadow_index = {
        item["signal"]: item["index"] for item in transport["shadow_signals"]
    }
    for segment in netlist["nets"]:
        # The selected split is immutable for this lowering call.  Copy only
        # the net shell and endpoint arrays that are actually rewritten;
        # recursively copying every instance/net payload doubled peak memory
        # for large partitions without adding isolation.
        value = dict(segment)
        value["aliases"] = list(segment.get("aliases", []))
        value["drivers"] = [
            dict(endpoint) for endpoint in segment["drivers"]
        ]
        value["sinks"] = [
            dict(endpoint) for endpoint in segment["sinks"]
        ]
        value["id"] = segment["original_net"]
        value["name"] = segment["name"]
        value.setdefault("aliases", [])
        value.setdefault("bus_index", 0)
        value.setdefault("fanout", len(value["sinks"]))
        generated_drivers = [
            endpoint
            for endpoint in value["drivers"]
            if endpoint["instance"] is not None
            and endpoint["instance"].startswith("__emuflow_rx_")
        ]
        value["drivers"] = [
            endpoint
            for endpoint in value["drivers"]
            if endpoint not in generated_drivers
        ]
        if generated_drivers:
            endpoint_id = generated_drivers[0]["instance"]
            signal = rx_endpoint_to_signal.get(endpoint_id)
            if signal is None:
                raise ValidationError(
                    f"local net references unknown RX endpoint {endpoint_id!r}"
                )
            top_net = _top_net(
                top_net_index,
                "shadow_values",
                shadow_index[signal],
                "sinks",
            )
            value["drivers"].extend(
                remap_endpoint(endpoint)
                for endpoint in top_net["drivers"]
                if endpoint["instance"] is not None
            )
        local_nets[value["id"]] = value

    consumed_transport_nets = set()
    source_index = {
        item["signal"]: item["index"] for item in transport["source_signals"]
    }
    for signal, index in source_index.items():
        # The shared open-flow runtime currently uses CPython 3.8 even though
        # developer environments may be newer.  Keep this hot-path operation
        # compatible with that validated runtime instead of relying on
        # str.removeprefix (introduced in Python 3.9).
        original_net = signal[4:] if signal.startswith("net:") else signal
        if original_net not in local_nets:
            raise ValidationError(
                f"transport source net {original_net!r} is not local"
            )
        top_net = _top_net(
            top_net_index,
            "source_values",
            index,
            "drivers",
        )
        consumed_transport_nets.add(top_net["id"])
        local_nets[original_net]["sinks"].extend(
            remap_endpoint(endpoint)
            for endpoint in top_net["sinks"]
            if endpoint["instance"] is not None
        )

    preserved_shadow_nets = set()
    for signal, index in shadow_index.items():
        top_net = _top_net(
            top_net_index,
            "shadow_values",
            index,
            "sinks",
        )
        internal_sinks = [
            remap_endpoint(endpoint)
            for endpoint in top_net["sinks"]
            if endpoint["instance"] is not None
        ]
        logical_net = shadow_signal_to_logical_net.get(signal)
        if logical_net in local_nets:
            local_nets[logical_net]["sinks"].extend(internal_sinks)
            local_nets[logical_net]["fanout"] = len(
                local_nets[logical_net]["sinks"]
            )
            consumed_transport_nets.add(top_net["id"])
        else:
            # A routing-only FPGA still needs the captured shadow register to
            # drive the next-hop TX mux.  Remove only the top-level observation
            # port below; preserving this internal net is essential to a real
            # multi-hop physical implementation.
            preserved_shadow_nets.add(top_net["id"])

    removed_ports = {"source_values", "shadow_values"}
    dut_ports_by_id = {port["id"]: port for port in netlist["ports"]}
    shared_input_ports: Dict[str, Dict[str, Any]] = {}
    for port in transport_ir.value["ports"]:
        if port["id"] in removed_ports:
            continue
        existing = dut_ports_by_id.get(port["id"])
        if existing is None:
            continue
        shared_fields = ("direction", "width", "clock", "reset")
        if any(existing.get(field) != port.get(field) for field in shared_fields):
            raise ValidationError(
                "transport top port is incompatible with DUT port "
                f"{port['id']!r}"
            )
        if port["direction"] != "input":
            raise ValidationError(
                "transport top port collides with non-input DUT port "
                f"{port['id']!r}"
            )
        shared_input_ports[port["id"]] = port

    # A shared clock/reset is one electrical top-level input, not two nets
    # driven by duplicate copies of the same port.  Stitch every transport
    # consumer onto the existing DUT net and consume the transport-only net.
    for port_id, port in shared_input_ports.items():
        for bit in range(port["width"]):
            transport_net = _top_net(
                top_net_index, port_id, bit, "drivers"
            )
            local_matches = [
                net
                for net in local_nets.values()
                if any(
                    endpoint["instance"] is None
                    and endpoint["port"] == port_id
                    and endpoint["bit"] == bit
                    for endpoint in net["drivers"]
                )
            ]
            if len(local_matches) != 1:
                raise ValidationError(
                    f"shared top input {port_id}[{bit}] expected one DUT "
                    f"net, found {len(local_matches)}"
                )
            local_net = local_matches[0]
            local_net["sinks"].extend(
                remap_endpoint(endpoint)
                for endpoint in transport_net["sinks"]
                if endpoint["instance"] is not None
            )
            local_net["fanout"] = len(local_net["sinks"])
            consumed_transport_nets.add(transport_net["id"])

    # The generated transport RTL keeps each packed interface at width one
    # when a partition has no TX or no RX signals.  Yosys consequently emits
    # a dangling top-level source_values/shadow_values net for that dummy bit.
    # Both interface ports are removed below, so consume every remaining net
    # that references them as well.  Real interface bits have already been
    # stitched into local DUT nets by the loops above.
    for net in transport_ir.value["nets"]:
        if any(
            endpoint["instance"] is None
            and endpoint["port"] in removed_ports
            for collection in ("drivers", "sinks")
            for endpoint in net[collection]
        ):
            if net["id"] not in preserved_shadow_nets:
                consumed_transport_nets.add(net["id"])

    transport_nets = []
    for net in transport_ir.value["nets"]:
        if net["id"] in consumed_transport_nets:
            continue
        value = deepcopy(net)
        value["id"] = f"{namespace}{net['id']}"
        value["name"] = f"{namespace}{net['name']}"
        value["drivers"] = [
            remap_endpoint(endpoint)
            for endpoint in net["drivers"]
            if endpoint["instance"] is not None
            or endpoint["port"] not in removed_ports
        ]
        value["sinks"] = [
            remap_endpoint(endpoint)
            for endpoint in net["sinks"]
            if endpoint["instance"] is not None
            or endpoint["port"] not in removed_ports
        ]
        value["fanout"] = len(value["sinks"])
        transport_nets.append(value)

    ports = list(netlist["ports"])
    ports_by_id = {port["id"]: port for port in ports}
    for port in transport_ir.value["ports"]:
        if port["id"] in removed_ports:
            continue
        existing = ports_by_id.get(port["id"])
        if existing is not None:
            # Compatibility and net stitching were validated above.
            continue
        ports.append(deepcopy(port))
        ports_by_id[port["id"]] = ports[-1]

    instances = list(netlist["instances"])
    instances.extend(
        {
            **deepcopy(instance),
            "id": instance_map[instance["id"]],
            "name": instance_map[instance["id"]],
        }
        for instance in transport_ir.value["instances"]
    )
    clocks = [
        {
            "id": port["id"],
            "name": port["name"],
            "source_port": port["id"],
            "period_ns": None,
        }
        for port in ports
        if port.get("clock")
    ]
    return EmuIR(
        {
            "schema": EMUIR_SCHEMA,
            "design": {
                **deepcopy(netlist["design"]),
                "name": f"{netlist['design']['name']}__{netlist['fpga']}",
                "top": f"{netlist['design']['top']}__{netlist['fpga']}",
                "source_format": "emuflow-phase7-merged",
            },
            "ports": sorted(ports, key=lambda item: item["id"]),
            "instances": sorted(instances, key=lambda item: item["id"]),
            "nets": sorted(
                [*local_nets.values(), *transport_nets],
                key=lambda item: item["id"],
            ),
            "clocks": clocks,
            "warnings": list(transport_ir.value.get("warnings", [])),
        }
    )


def run_placement_ir_lowering(
    netlist_path: Path,
    transport_path: Path,
    transport_ir_path: Path,
    output_path: Path,
    report_path: Optional[Path] = None,
    boundary_identity_path: Optional[Path] = None,
) -> Dict[str, Any]:
    return run_placement_ir_lowering_from_value(
        read_json(netlist_path),
        transport_path,
        transport_ir_path,
        output_path,
        report_path,
        boundary_identity_path,
    )


def run_placement_ir_lowering_from_value(
    netlist: Mapping[str, Any],
    transport_path: Path,
    transport_ir_path: Path,
    output_path: Path,
    report_path: Optional[Path] = None,
    boundary_identity_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Lower one already-selected partition without a JSON round trip."""

    transport = read_transport(transport_path)
    transport_ir = EmuIR.load(transport_ir_path)
    result = build_placement_ir(
        netlist, transport, transport_ir
    )
    result.save(output_path)
    boundary_path = (
        boundary_identity_path
        if boundary_identity_path is not None
        else output_path.with_name("boundary-identities.json")
    )
    boundary_database = build_boundary_identity_database(
        transport, result, transport_ir
    )
    boundary_validation = validate_boundary_identity_database(
        boundary_database, transport
    )
    write_json(boundary_path, boundary_database)
    stats = result.stats()
    report = {
        "schema": PLACEMENT_IR_REPORT_SCHEMA,
        "status": "pass",
        "design": stats["design"],
        "instances": stats["instances"],
        "nets": stats["nets"],
        "resource_totals": stats["resource_totals"],
        "transport_instances": sum(
            instance["id"].startswith("__emuflow_transport__/")
            for instance in result.value["instances"]
        ),
        "output": str(output_path),
        "boundary_identity": {
            "schema": boundary_database["schema"],
            "output": str(boundary_path),
            "validation": boundary_validation,
        },
    }
    if report_path is not None:
        write_json(report_path, report)
    return report


def run_placement_ir_lowering_from_split(
    split_path: Path,
    fpga: str,
    transport_path: Path,
    transport_ir_path: Path,
    output_path: Path,
    report_path: Optional[Path] = None,
    boundary_identity_path: Optional[Path] = None,
) -> Dict[str, Any]:
    with IndexedPhysicalIR(transport_ir_path) as transport_ir:
        return _run_placement_ir_lowering_from_split_indexed(
            split_path,
            fpga,
            transport_path,
            transport_ir_path,
            transport_ir,
            output_path,
            report_path,
            boundary_identity_path,
        )


def _run_placement_ir_lowering_from_split_indexed(
    split_path: Path,
    fpga: str,
    transport_path: Path,
    transport_ir_path: Path,
    transport_ir: IndexedPhysicalIR,
    output_path: Path,
    report_path: Optional[Path] = None,
    boundary_identity_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Lower split SQLite directly into compact placement SQLite.

    The legacy adapter above remains for external JSON callers.  Production
    Phase 7 uses this path so one FPGA's complete netlist is never recreated
    as nested Python objects between the two SQLite artifacts.
    """

    transport = read_transport(transport_path)
    if transport.get("schema") != TRANSPORT_ENDPOINTS_SCHEMA:
        raise ValidationError("invalid transport endpoint schema")
    top_net_index: Dict[
        Tuple[str, int, str], List[str]
    ] = {}
    for row in transport_ir.connection.execute(
        """SELECT pin.name,e.bit,e.role,n.id
        FROM endpoint_records e
        JOIN pin_names pin ON pin.seq=e.port_seq
        JOIN net_records n ON n.seq=e.net_seq
        WHERE e.instance_seq IS NULL
        ORDER BY pin.name,e.bit,e.role,n.seq"""
    ):
        collection = ("drivers", "sinks")[int(row[2])]
        top_net_index.setdefault(
            (str(row[0]), int(row[1]), collection), []
        ).append(str(row[3]))

    def top_net(port: str, bit: int, collection: str) -> Dict[str, Any]:
        matches = top_net_index.get((port, bit, collection), [])
        if len(matches) != 1:
            raise ValidationError(
                f"transport EmuIR {port}[{bit}] expected one "
                f"{collection[:-1]} net, found {len(matches)}"
            )
        return transport_ir.nets[matches[0]]

    namespace = "__emuflow_transport__/"

    def remap_endpoint(endpoint: Mapping[str, Any]) -> Dict[str, Any]:
        value = dict(endpoint)
        if value["instance"] is not None:
            value["instance"] = f"{namespace}{value['instance']}"
        return value

    indexed_rx_signals = getattr(transport, "iter_rx_signal_records", None)
    indexed_rx_bindings = getattr(
        transport, "iter_expected_rx_bindings", None
    )
    legacy_rx_by_signal: Dict[str, Tuple[str, List[str]]] = {}
    if indexed_rx_signals is None:
        for endpoint in transport["endpoints"]:
            if endpoint["kind"] != "rx":
                continue
            signal = endpoint["signal"]
            logical_net, endpoint_ids = legacy_rx_by_signal.setdefault(
                signal, (endpoint["net"], [])
            )
            if logical_net != endpoint["net"]:
                raise ValidationError(
                    "one transport shadow signal maps to multiple nets"
                )
            endpoint_ids.append(endpoint["id"])
    removed_ports = {"source_values", "shadow_values"}

    with SplitNetlistView(split_path, fpga) as split:
        if split.metadata.get("schema") != "emuflow.split-netlists-sqlite/v2":
            raise ValidationError("invalid split-netlist storage schema")
        if split.fpga != transport.get("fpga"):
            raise ValidationError("netlist and transport target different FPGAs")
        dut_ports_by_id = {port["id"]: port for port in split.ports}
        shared_input_ports: Dict[str, Dict[str, Any]] = {}
        for port in transport_ir.value["ports"]:
            if port["id"] in removed_ports:
                continue
            existing = dut_ports_by_id.get(port["id"])
            if existing is None:
                continue
            if any(
                existing.get(field) != port.get(field)
                for field in ("direction", "width", "clock", "reset")
            ) or port["direction"] != "input":
                raise ValidationError(
                    f"transport top port is incompatible with DUT port {port['id']!r}"
                )
            shared_input_ports[port["id"]] = port

        consumed_transport_nets = set()
        preserved_shadow_nets = set()
        shadow_internal_sinks: Dict[str, List[Dict[str, Any]]] = {}
        rx_internal_drivers: Dict[str, List[Dict[str, Any]]] = {}
        local_net_ids = split.net_ids()
        rx_signal_records = iter(
            indexed_rx_signals()
            if indexed_rx_signals is not None
            else (
                (signal, value[0])
                for signal, value in sorted(legacy_rx_by_signal.items())
            )
        )
        for item in transport["shadow_signals"]:
            signal = item["signal"]
            index = item["index"]
            try:
                rx_signal, logical_net = next(rx_signal_records)
            except StopIteration as error:
                raise ValidationError(
                    "transport shadow signal coverage is incomplete"
                ) from error
            if rx_signal != signal:
                raise ValidationError(
                    "transport shadow signal order disagrees with RX endpoints"
                )
            top_net_record = top_net("shadow_values", index, "sinks")
            internal = [
                remap_endpoint(endpoint)
                for endpoint in top_net_record["sinks"]
                if endpoint["instance"] is not None
            ]
            if logical_net is not None and logical_net in local_net_ids:
                shadow_internal_sinks[logical_net] = internal
                rx_internal_drivers[logical_net] = [
                    remap_endpoint(endpoint)
                    for endpoint in top_net_record["drivers"]
                    if endpoint["instance"] is not None
                ]
                consumed_transport_nets.add(top_net_record["id"])
            else:
                preserved_shadow_nets.add(top_net_record["id"])
        try:
            next(rx_signal_records)
        except StopIteration:
            pass
        else:
            raise ValidationError(
                "transport RX endpoints contain an unindexed shadow signal"
            )

        source_internal_sinks: Dict[str, List[Dict[str, Any]]] = {}
        for item in transport["source_signals"]:
            signal = item["signal"]
            original_net = signal[4:] if signal.startswith("net:") else signal
            index = item["index"]
            if original_net not in local_net_ids:
                raise ValidationError(
                    f"transport source net {original_net!r} is not local"
                )
            top_net_record = top_net("source_values", index, "drivers")
            consumed_transport_nets.add(top_net_record["id"])
            source_internal_sinks[original_net] = [
                remap_endpoint(endpoint)
                for endpoint in top_net_record["sinks"]
                if endpoint["instance"] is not None
            ]

        shared_sinks: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
        for port_id, port in shared_input_ports.items():
            for bit in range(port["width"]):
                top_net_record = top_net(port_id, bit, "drivers")
                consumed_transport_nets.add(top_net_record["id"])
                shared_sinks[(port_id, bit)] = [
                    remap_endpoint(endpoint)
                    for endpoint in top_net_record["sinks"]
                    if endpoint["instance"] is not None
                ]

        for row in transport_ir.connection.execute(
            """SELECT DISTINCT n.id FROM endpoint_records e
            JOIN pin_names pin ON pin.seq=e.port_seq
            JOIN net_records n ON n.seq=e.net_seq
            WHERE e.instance_seq IS NULL
              AND pin.name IN ('source_values','shadow_values')
            ORDER BY n.seq"""
        ):
            net_id = str(row[0])
            if net_id not in preserved_shadow_nets:
                consumed_transport_nets.add(net_id)

        added_endpoints = []
        endpoint_orders: Dict[Tuple[str, int], int] = {}

        def add_endpoints(
            net_id: str,
            role: int,
            endpoints: List[Dict[str, Any]],
        ) -> None:
            key = (net_id, role)
            order = endpoint_orders.get(key, 0)
            for endpoint in endpoints:
                added_endpoints.append((net_id, role, order, endpoint))
                order += 1
            endpoint_orders[key] = order

        for net_id, endpoints in rx_internal_drivers.items():
            add_endpoints(net_id, 0, endpoints)
        for net_id, endpoints in source_internal_sinks.items():
            add_endpoints(net_id, 1, endpoints)
        for net_id, endpoints in shadow_internal_sinks.items():
            add_endpoints(net_id, 1, endpoints)

        port_added_endpoints = []
        for (port_id, bit), endpoints in shared_sinks.items():
            for order, endpoint in enumerate(endpoints):
                port_added_endpoints.append(
                    (port_id, bit, order, endpoint)
                )

        ports = list(split.ports)
        ports_by_id = {port["id"]: port for port in ports}
        for port in transport_ir.value["ports"]:
            if port["id"] in removed_ports or port["id"] in ports_by_id:
                continue
            ports.append(deepcopy(port))
            ports_by_id[port["id"]] = ports[-1]
        ports.sort(key=lambda item: item["id"])
        clocks = [
            {
                "id": port["id"], "name": port["name"],
                "source_port": port["id"], "period_ns": None,
            }
            for port in ports if port.get("clock")
        ]
        design = split.metadata["design"]
        merged_design = {
            **design,
            "name": f"{design['name']}__{fpga}",
            "top": f"{design['top']}__{fpga}",
            "source_format": "emuflow-phase7-merged",
        }
        write_placement_emuir_from_split(
            split_path,
            fpga,
            output_path,
            design=merged_design,
            ports=ports,
            clocks=clocks,
            warnings=list(transport_ir.value.get("warnings", [])),
            transport_ir_path=transport_ir_path,
            consumed_transport_nets=consumed_transport_nets,
            added_endpoints=added_endpoints,
            port_added_endpoints=port_added_endpoints,
            recompute_fanout=shadow_internal_sinks,
            expected_source_nets=source_internal_sinks,
            expected_rx_bindings=(
                (
                    (net_id, endpoint_id)
                    for net_id, endpoint_id in indexed_rx_bindings()
                    if net_id in local_net_ids
                )
                if indexed_rx_bindings is not None
                else (
                    (net_id, endpoint_id)
                    for net_id, endpoint_ids in legacy_rx_by_signal.values()
                    if net_id in local_net_ids
                    for endpoint_id in endpoint_ids
                )
            ),
        )
        original_instances = split.counts()["instances"]

    boundary_path = (
        boundary_identity_path
        if boundary_identity_path is not None
        else output_path.with_name("boundary-identities.json")
    )
    with IndexedPhysicalIR(output_path) as result:
        boundary_database = build_boundary_identity_database(
            transport, result, transport_ir
        )
        boundary_validation = validate_boundary_identity_database(
            boundary_database, transport
        )
    write_json(boundary_path, boundary_database)
    connection = open_emuir_sqlite_read_only(output_path)
    try:
        instances = int(connection.execute(
            "SELECT count(*) FROM cell_records"
        ).fetchone()[0])
        nets = int(connection.execute(
            "SELECT count(*) FROM net_records"
        ).fetchone()[0])
        resource_totals = {
            row[0]: int(row[1])
            for row in connection.execute(
                "SELECT resource,sum(amount) FROM instance_resources "
                "GROUP BY resource ORDER BY resource"
            )
        }
        transport_instances_count = int(connection.execute(
            "SELECT count(*) FROM cell_records "
            "WHERE substr(id,1,length(?))=?", (namespace, namespace)
        ).fetchone()[0])
    finally:
        connection.close()
    report = {
        "schema": PLACEMENT_IR_REPORT_SCHEMA,
        "status": "pass",
        "design": f"{transport['design']}__{fpga}",
        "instances": instances,
        "nets": nets,
        "resource_totals": resource_totals,
        "transport_instances": transport_instances_count,
        "original_instances": original_instances,
        "output": str(output_path),
        "boundary_identity": {
            "schema": boundary_database["schema"],
            "output": str(boundary_path),
            "validation": boundary_validation,
        },
    }
    if report_path is not None:
        write_json(report_path, report)
    return report
