from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .boundary_timing import (
    boundary_identity_header,
    build_boundary_identity_database,
    iter_boundary_identity_records,
    validate_boundary_identity_database,
)
from .boundary_identity_storage import write_boundary_identity_storage
from .errors import ValidationError
from .store_publication import bind_view_sources
from .io import read_json, write_json
from .ir import EMUIR_SCHEMA, EmuIR
from .emuir_storage import _extras, open_emuir_sqlite_read_only
from .indexed_physical_ir import IndexedPhysicalIR
from .netlist import FPGA_NETLIST_SCHEMA, TRANSPORT_ENDPOINTS_SCHEMA
from .placement_ir_storage import write_placement_emuir_from_split
from .split_netlist_storage import SplitNetlistView
from .split_overlay import SplitProjectionConnection,attach_split_projection
from .transport_storage import read_transport


PLACEMENT_IR_REPORT_SCHEMA = "emuflow.placement-ir-report/v1"


class _LoweringOverlaySpool:
    """Own Phase 7 additions and cross-store joins outside the Python heap."""

    def __init__(
        self,
        directory: Path,
        *,
        split_path: Optional[Path] = None,
        part_index: Optional[int] = None,
        transport_ir_path: Optional[Path] = None,
        transport_store_path: Optional[Path] = None,
        fpga: Optional[str] = None,
    ):
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=".placement-overlay-", suffix=".sqlite", dir=directory
        )
        os.close(descriptor)
        self.path = Path(name)
        self.connection = sqlite3.connect(self.path, uri=True, factory=SplitProjectionConnection)
        self.connection.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            CREATE TABLE added_endpoints(
              seq INTEGER PRIMARY KEY,
              net_id TEXT NOT NULL,
              role INTEGER NOT NULL,
              priority INTEGER NOT NULL,
              instance_id TEXT,
              port TEXT NOT NULL,
              bit INTEGER NOT NULL,
              extras_json TEXT NOT NULL
            );
            CREATE TABLE port_additions(
              seq INTEGER PRIMARY KEY,
              driver_port TEXT NOT NULL,
              driver_bit INTEGER NOT NULL,
              instance_id TEXT,
              port TEXT NOT NULL,
              bit INTEGER NOT NULL,
              extras_json TEXT NOT NULL
            );
            CREATE TABLE consumed_nets(id TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE interface_nets(id TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE preserved_nets(id TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE fanout_nets(id TEXT PRIMARY KEY) WITHOUT ROWID;
            CREATE TABLE source_nets(id TEXT PRIMARY KEY) WITHOUT ROWID;
            """
        )
        self._part_index = part_index
        self._fpga = fpga
        self._has_split_store = split_path is not None
        self._has_transport_store = transport_store_path is not None
        if self._has_split_store:
            if part_index is None:
                raise ValueError("split part index is required")
            attach_split_projection(self.connection,split_path,"split_store")
        if transport_ir_path is not None:
            self.connection.execute(
                "ATTACH DATABASE ? AS transport_ir_store",
                (f"{Path(transport_ir_path).resolve().as_uri()}?mode=ro",),
            )
        if self._has_transport_store:
            if fpga is None:
                raise ValueError("transport FPGA identity is required")
            self.connection.execute(
                "ATTACH DATABASE ? AS transport_store",
                (f"{Path(transport_store_path).resolve().as_uri()}?mode=ro",),
            )
        self._added_seq = 0
        self._port_seq = 0
        self._added_batch: list[tuple[Any, ...]] = []
        self._port_batch: list[tuple[Any, ...]] = []
        self._identity_batches: Dict[str, list[str]] = {
            "consumed_nets": [],
            "interface_nets": [],
            "preserved_nets": [],
            "fanout_nets": [],
            "source_nets": [],
        }

    def top_net_id(self, port: str, bit: int, collection: str) -> str:
        role = {"drivers": 0, "sinks": 1}.get(collection)
        if role is None:
            raise ValueError(f"invalid endpoint collection {collection!r}")
        row = self.connection.execute(
            """SELECT count(*),min(n.id)
            FROM transport_ir_store.pin_names pin
            JOIN transport_ir_store.endpoint_records e INDEXED BY endpoint_owner
              ON e.port_seq=pin.seq
            JOIN transport_ir_store.net_records n ON n.seq=e.net_seq
            WHERE e.instance_seq IS NULL AND pin.name=?
              AND e.bit=? AND e.role=?""",
            (port, bit, role),
        ).fetchone()
        count = int(row[0])
        if count != 1:
            raise ValidationError(
                f"transport EmuIR {port}[{bit}] expected one "
                f"{collection[:-1]} net, found {count}"
            )
        return str(row[1])

    def is_local_net(self, net_id: str) -> bool:
        if not self._has_split_store or self._part_index is None:
            raise RuntimeError("split store is not attached")
        return self.connection.execute(
            """SELECT 1
            FROM split_store.source_nets n
            JOIN split_store.net_segments s
              ON s.source_net_seq=n.source_net_seq
            WHERE n.original_net=? AND s.part_index=?""",
            (net_id, self._part_index),
        ).fetchone() is not None

    def iter_rx_overlays(self):
        if not self._has_transport_store or self._fpga is None:
            raise RuntimeError("typed transport store is not attached")
        extra = self.connection.execute(
            """SELECT count(*)
            FROM transport_store.endpoints e
            LEFT JOIN transport_store.signals s
              ON s.fpga=e.fpga AND s.kind='shadow' AND s.signal=e.signal
            WHERE e.fpga=? AND e.kind='rx' AND s.signal IS NULL""",
            (self._fpga,),
        ).fetchone()[0]
        if extra:
            raise ValidationError(
                "transport RX endpoints contain an unindexed shadow signal"
            )
        for row in self.connection.execute(
            """WITH rx AS (
              SELECT signal,count(DISTINCT net) AS net_count,min(net) AS net
              FROM transport_store.endpoints
              WHERE fpga=? AND kind='rx' GROUP BY signal
            ), top AS (
              SELECT e.bit,count(*) AS net_count,min(n.id) AS net_id
              FROM transport_ir_store.endpoint_records e
                   INDEXED BY endpoint_owner
              JOIN transport_ir_store.pin_names pin ON pin.seq=e.port_seq
              JOIN transport_ir_store.net_records n ON n.seq=e.net_seq
              WHERE e.instance_seq IS NULL AND e.role=1
                AND pin.name='shadow_values'
              GROUP BY e.bit
            )
            SELECT s.signal,s.seq,rx.net,coalesce(rx.net_count,0),
                   coalesce(top.net_count,0),top.net_id,
                   CASE WHEN segment.source_net_seq IS NULL THEN 0 ELSE 1 END
            FROM transport_store.signals s
            LEFT JOIN rx ON rx.signal=s.signal
            LEFT JOIN top ON top.bit=s.seq
            LEFT JOIN split_store.source_nets net ON net.original_net=rx.net
            LEFT JOIN split_store.net_segments segment
              ON segment.source_net_seq=net.source_net_seq
             AND segment.part_index=?
            WHERE s.fpga=? AND s.kind='shadow'
            ORDER BY s.seq""",
            (self._fpga, self._part_index, self._fpga),
        ):
            signal, index = str(row[0]), int(row[1])
            rx_count, top_count = int(row[3]), int(row[4])
            if rx_count != 1:
                raise ValidationError(
                    f"transport shadow signal {signal!r} maps to "
                    f"{rx_count} logical nets"
                )
            if top_count != 1:
                raise ValidationError(
                    f"transport EmuIR shadow_values[{index}] expected one "
                    f"sink net, found {top_count}"
                )
            yield (
                signal,
                index,
                str(row[2]),
                bool(row[6]),
                str(row[5]),
            )

    def iter_source_overlays(self):
        if not self._has_transport_store or self._fpga is None:
            raise RuntimeError("typed transport store is not attached")
        for row in self.connection.execute(
            """WITH top AS (
              SELECT e.bit,count(*) AS net_count,min(n.id) AS net_id
              FROM transport_ir_store.endpoint_records e
                   INDEXED BY endpoint_owner
              JOIN transport_ir_store.pin_names pin ON pin.seq=e.port_seq
              JOIN transport_ir_store.net_records n ON n.seq=e.net_seq
              WHERE e.instance_seq IS NULL AND e.role=0
                AND pin.name='source_values'
              GROUP BY e.bit
            ), source AS (
              SELECT seq,signal,
                     CASE WHEN substr(signal,1,4)='net:'
                          THEN substr(signal,5) ELSE signal END AS net
              FROM transport_store.signals
              WHERE fpga=? AND kind='source'
            )
            SELECT source.signal,source.seq,source.net,
                   CASE WHEN segment.source_net_seq IS NULL THEN 0 ELSE 1 END,
                   coalesce(top.net_count,0),top.net_id
            FROM source
            LEFT JOIN top ON top.bit=source.seq
            LEFT JOIN split_store.source_nets net ON net.original_net=source.net
            LEFT JOIN split_store.net_segments segment
              ON segment.source_net_seq=net.source_net_seq
             AND segment.part_index=?
            ORDER BY source.seq""",
            (self._fpga, self._part_index),
        ):
            signal, index, net_id = str(row[0]), int(row[1]), str(row[2])
            if not row[3]:
                raise ValidationError(
                    f"transport source net {net_id!r} is not local"
                )
            count = int(row[4])
            if count != 1:
                raise ValidationError(
                    f"transport EmuIR source_values[{index}] expected one "
                    f"driver net, found {count}"
                )
            yield signal, index, net_id, str(row[5])

    def iter_local_rx_bindings(self):
        if (
            not self._has_transport_store
            or not self._has_split_store
            or self._fpga is None
            or self._part_index is None
        ):
            raise RuntimeError("typed transport and split stores are required")
        for row in self.connection.execute(
            """SELECT e.net,e.id
            FROM transport_store.endpoints e
            JOIN split_store.source_nets n ON n.original_net=e.net
            JOIN split_store.net_segments s
              ON s.source_net_seq=n.source_net_seq
             AND s.part_index=?
            WHERE e.fpga=? AND e.kind='rx'
            ORDER BY e.net,e.id""",
            (self._part_index, self._fpga),
        ):
            yield str(row[0]), str(row[1])

    @staticmethod
    def _fields(endpoint: Mapping[str, Any]) -> tuple[Optional[str], str, int, str]:
        return (
            endpoint.get("instance"), str(endpoint["port"]),
            int(endpoint["bit"]),
            _extras(endpoint, {"instance", "port", "bit"}),
        )

    def add_endpoints(
        self,
        net_id: str,
        role: int,
        priority: int,
        endpoints: Iterable[Mapping[str, Any]],
    ) -> None:
        for endpoint in endpoints:
            seq = self._added_seq
            self._added_seq += 1
            self._added_batch.append(
                (seq, net_id, role, priority, *self._fields(endpoint))
            )
            if len(self._added_batch) >= 4096:
                self._flush_added()

    def _flush_added(self) -> None:
        if self._added_batch:
            self.connection.executemany(
                "INSERT INTO added_endpoints VALUES (?,?,?,?,?,?,?,?)",
                self._added_batch,
            )
            self._added_batch.clear()

    def add_port_endpoints(
        self,
        driver_port: str,
        driver_bit: int,
        endpoints: Iterable[Mapping[str, Any]],
    ) -> None:
        for endpoint in endpoints:
            seq = self._port_seq
            self._port_seq += 1
            self._port_batch.append(
                (seq, driver_port, driver_bit, *self._fields(endpoint))
            )
            if len(self._port_batch) >= 4096:
                self._flush_port()

    def _flush_port(self) -> None:
        if self._port_batch:
            self.connection.executemany(
                "INSERT INTO port_additions VALUES (?,?,?,?,?,?,?)",
                self._port_batch,
            )
            self._port_batch.clear()

    def _identity(self, table: str, identity: str) -> None:
        batch = self._identity_batches[table]
        batch.append(identity)
        if len(batch) >= 4096:
            self._flush_identities(table)

    def _flush_identities(self, table: str) -> None:
        batch = self._identity_batches[table]
        if batch:
            self.connection.executemany(
                f"INSERT OR IGNORE INTO {table} VALUES (?)",
                ((identity,) for identity in batch),
            )
            batch.clear()

    def consume(self, net_id: str) -> None:
        self._identity("consumed_nets", net_id)

    def preserve(self, net_id: str) -> None:
        self._identity("preserved_nets", net_id)

    def consume_interface(self, net_id: str) -> None:
        self._identity("interface_nets", net_id)

    def recompute_fanout(self, net_id: str) -> None:
        self._identity("fanout_nets", net_id)

    def expect_source(self, net_id: str) -> None:
        self._identity("source_nets", net_id)

    def finish_consumed(self) -> None:
        self._flush_added()
        self._flush_port()
        for table in self._identity_batches:
            self._flush_identities(table)
        self.connection.execute(
            """INSERT OR IGNORE INTO consumed_nets
               SELECT id FROM interface_nets
                WHERE id NOT IN (SELECT id FROM preserved_nets)"""
        )
        self.connection.commit()

    def added_endpoints(self):
        for row in self.connection.execute(
            """SELECT net_id,role,priority,seq,instance_id,port,bit,extras_json
                 FROM added_endpoints ORDER BY seq"""
        ):
            yield (
                str(row[0]), int(row[1]), int(row[2]), int(row[3]),
                None if row[4] is None else str(row[4]), str(row[5]),
                int(row[6]), str(row[7]),
            )

    def port_additions(self):
        for row in self.connection.execute(
            """SELECT driver_port,driver_bit,seq,instance_id,port,bit,
                      extras_json
                 FROM port_additions ORDER BY seq"""
        ):
            yield (
                str(row[0]), int(row[1]), int(row[2]),
                None if row[3] is None else str(row[3]), str(row[4]),
                int(row[5]), str(row[6]),
            )

    def identities(self, table: str):
        for row in self.connection.execute(
            f"SELECT id FROM {table} ORDER BY id"
        ):
            yield str(row[0])

    def __enter__(self) -> "_LoweringOverlaySpool":
        return self

    def __exit__(self, *_args: object) -> None:
        self.connection.close()
        self.path.unlink(missing_ok=True)


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
    namespace = "__emuflow_transport__/"

    def remap_endpoint(endpoint: Mapping[str, Any]) -> Dict[str, Any]:
        value = dict(endpoint)
        if value["instance"] is not None:
            value["instance"] = f"{namespace}{value['instance']}"
        return value

    transport_store_path = getattr(transport, "store_path", None)
    legacy_rx_by_signal: Dict[str, Tuple[str, List[str]]] = {}
    if transport_store_path is None:
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

    with (
        SplitNetlistView(split_path, fpga) as split,
        _LoweringOverlaySpool(
            output_path.parent,
            split_path=split_path,
            part_index=split.part_index,
            transport_ir_path=transport_ir_path,
            transport_store_path=transport_store_path,
            fpga=fpga,
        ) as overlay,
    ):
        if split.metadata.get("schema") not in {"emuflow.split-netlists-sqlite/v2","emuflow.split-netlists-sqlite/v3"}:
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

        def top_net(port: str, bit: int, collection: str) -> Dict[str, Any]:
            return transport_ir.nets[
                overlay.top_net_id(port, bit, collection)
            ]

        rx_overlay_records = iter(overlay.iter_rx_overlays()) if (
            transport_store_path is not None
        ) else None
        rx_signal_records = iter(
            (signal, value[0])
            for signal, value in sorted(legacy_rx_by_signal.items())
        ) if rx_overlay_records is None else None
        for item in transport["shadow_signals"]:
            signal = item["signal"]
            index = item["index"]
            try:
                if rx_overlay_records is not None:
                    (
                        rx_signal,
                        rx_index,
                        logical_net,
                        local_net,
                        top_net_id,
                    ) = next(rx_overlay_records)
                else:
                    rx_signal, logical_net = next(rx_signal_records)
                    rx_index = index
                    local_net = (
                        logical_net is not None
                        and overlay.is_local_net(logical_net)
                    )
                    top_net_id = overlay.top_net_id(
                        "shadow_values", index, "sinks"
                    )
            except StopIteration as error:
                raise ValidationError(
                    "transport shadow signal coverage is incomplete"
                ) from error
            if rx_signal != signal or rx_index != index:
                raise ValidationError(
                    "transport shadow signal order disagrees with RX endpoints"
                )
            top_net_record = transport_ir.nets[top_net_id]
            if logical_net is not None and local_net:
                overlay.add_endpoints(
                    logical_net,
                    0,
                    0,
                    (
                        remap_endpoint(endpoint)
                        for endpoint in top_net_record["drivers"]
                        if endpoint["instance"] is not None
                    ),
                )
                overlay.add_endpoints(
                    logical_net,
                    1,
                    2,
                    (
                        remap_endpoint(endpoint)
                        for endpoint in top_net_record["sinks"]
                        if endpoint["instance"] is not None
                    ),
                )
                overlay.recompute_fanout(logical_net)
                overlay.consume(top_net_record["id"])
            else:
                overlay.preserve(top_net_record["id"])
        try:
            next(
                rx_overlay_records
                if rx_overlay_records is not None
                else rx_signal_records
            )
        except StopIteration:
            pass
        else:
            raise ValidationError(
                "transport RX endpoints contain an unindexed shadow signal"
            )

        source_overlay_records = iter(overlay.iter_source_overlays()) if (
            transport_store_path is not None
        ) else None
        for item in transport["source_signals"]:
            signal = item["signal"]
            original_net = signal[4:] if signal.startswith("net:") else signal
            index = item["index"]
            if source_overlay_records is not None:
                try:
                    source_signal, source_index, source_net, top_net_id = next(
                        source_overlay_records
                    )
                except StopIteration as error:
                    raise ValidationError(
                        "transport source signal coverage is incomplete"
                    ) from error
                if (
                    source_signal != signal
                    or source_index != index
                    or source_net != original_net
                ):
                    raise ValidationError(
                        "transport source signal order disagrees"
                    )
                top_net_record = transport_ir.nets[top_net_id]
            else:
                if not overlay.is_local_net(original_net):
                    raise ValidationError(
                        f"transport source net {original_net!r} is not local"
                    )
                top_net_record = top_net(
                    "source_values", index, "drivers"
                )
            overlay.consume(top_net_record["id"])
            overlay.expect_source(original_net)
            overlay.add_endpoints(
                original_net,
                1,
                1,
                (
                    remap_endpoint(endpoint)
                    for endpoint in top_net_record["sinks"]
                    if endpoint["instance"] is not None
                ),
            )
        if source_overlay_records is not None:
            try:
                next(source_overlay_records)
            except StopIteration:
                pass
            else:
                raise ValidationError(
                    "transport source signals contain an unindexed signal"
                )

        for port_id, port in shared_input_ports.items():
            for bit in range(port["width"]):
                top_net_record = top_net(port_id, bit, "drivers")
                overlay.consume(top_net_record["id"])
                overlay.add_port_endpoints(
                    port_id,
                    bit,
                    (
                        remap_endpoint(endpoint)
                        for endpoint in top_net_record["sinks"]
                        if endpoint["instance"] is not None
                    ),
                )

        for row in transport_ir.connection.execute(
            """SELECT DISTINCT n.id FROM endpoint_records e
            JOIN pin_names pin ON pin.seq=e.port_seq
            JOIN net_records n ON n.seq=e.net_seq
            WHERE e.instance_seq IS NULL
              AND pin.name IN ('source_values','shadow_values')
            ORDER BY n.seq"""
        ):
            overlay.consume_interface(str(row[0]))
        overlay.finish_consumed()

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
            consumed_transport_nets=overlay.identities("consumed_nets"),
            added_endpoints=overlay.added_endpoints(),
            port_added_endpoints=overlay.port_additions(),
            recompute_fanout=overlay.identities("fanout_nets"),
            expected_source_nets=overlay.identities("source_nets"),
            expected_rx_bindings=(
                overlay.iter_local_rx_bindings()
                if transport_store_path is not None
                else (
                    (net_id, endpoint_id)
                    for net_id, endpoint_ids in legacy_rx_by_signal.values()
                    if overlay.is_local_net(net_id)
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
        with write_boundary_identity_storage(
            boundary_path,
            boundary_identity_header(transport),
            iter_boundary_identity_records(transport, result, transport_ir),
            source_bindings=bind_view_sources(
                (transport, getattr(transport,"store_schema","transport-store-v2")),
                (transport_ir, "emuir-compact-v2"),
                (result, "emuir-compact-v2"),
            ),
        ) as boundary_database:
            boundary_validation = validate_boundary_identity_database(
                boundary_database, transport
            )
            boundary_schema = boundary_database["schema"]
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
            "schema": boundary_schema,
            "output": str(boundary_path),
            "validation": boundary_validation,
        },
    }
    if report_path is not None:
        write_json(report_path, report)
    return report
