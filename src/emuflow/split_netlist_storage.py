"""SQLite data plane for Phase 6 netlist splitting.

The source EmuIR and Phase 3 assignment are joined by SQLite's native engine.
Python submits set-oriented statements and reads only the final counters; it
never reconstructs the complete circuit as nested Python objects.
"""

from __future__ import annotations

import json
import heapq
import os
import sqlite3
import uuid
from pathlib import Path
from collections import defaultdict
from typing import Any, Mapping, Optional

from .errors import ValidationError
from .phase3_assignment_index import (
    ASSIGNMENT_INDEX_SCHEMA,
    open_phase3_assignment_index,
)
from .netlist import (
    LOGICAL_LANE_MAP_SCHEMA,
    SPLIT_MANIFEST_SCHEMA,
    TRANSPORT_ENDPOINTS_SCHEMA,
    _build_virtual_anchors,
    _endpoint_id,
    schedule_routes,
)
from .platform import Platform
from .tdm import TDM_SCHEDULE_SCHEMA, is_sampled_virtual_wire_schedule


SPLIT_NETLIST_STORAGE_SCHEMA = "emuflow.split-netlists-sqlite/v2"
SPLIT_NETLIST_APPLICATION_ID = 0x454D5553  # ASCII "EMUS"
SPLIT_NETLIST_USER_VERSION = 2


DDL = """
CREATE TABLE metadata(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE parts(
  part_index INTEGER PRIMARY KEY,
  fpga TEXT NOT NULL UNIQUE
);
CREATE TABLE payloads(
  seq INTEGER PRIMARY KEY,
  value TEXT NOT NULL UNIQUE
);
CREATE TABLE pin_names(
  seq INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE
);
CREATE TABLE source_instances(
  source_instance_seq INTEGER PRIMARY KEY,
  original_id TEXT NOT NULL UNIQUE,
  name TEXT,
  flags INTEGER NOT NULL,
  type TEXT NOT NULL,
  resources_seq INTEGER NOT NULL,
  parameters_seq INTEGER,
  attributes_seq INTEGER,
  constants_seq INTEGER NOT NULL,
  extras_seq INTEGER NOT NULL
);
CREATE TABLE instance_copies(
  part_index INTEGER NOT NULL,
  source_instance_seq INTEGER NOT NULL,
  copy_id TEXT,
  is_replica INTEGER NOT NULL CHECK(is_replica IN (0,1)),
  PRIMARY KEY(part_index,source_instance_seq,is_replica)
) WITHOUT ROWID;
CREATE INDEX instance_copies_identity
  ON instance_copies(part_index,copy_id);
CREATE TABLE source_nets(
  source_net_seq INTEGER PRIMARY KEY,
  original_net TEXT NOT NULL UNIQUE,
  name TEXT,
  flags INTEGER NOT NULL,
  cut_class TEXT NOT NULL
);
CREATE TABLE net_segments(
  part_index INTEGER NOT NULL,
  source_net_seq INTEGER NOT NULL,
  source_kind TEXT NOT NULL,
  PRIMARY KEY(part_index,source_net_seq)
) WITHOUT ROWID;
CREATE INDEX net_segments_source
  ON net_segments(source_net_seq,part_index);
CREATE TABLE endpoint_segments(
  part_index INTEGER NOT NULL,
  source_net_seq INTEGER NOT NULL,
  role INTEGER NOT NULL,
  ordinal INTEGER NOT NULL,
  source_instance_seq INTEGER,
  synthetic_instance_id TEXT,
  port_seq INTEGER NOT NULL,
  bit INTEGER NOT NULL,
  extras_seq INTEGER NOT NULL,
  synthetic INTEGER NOT NULL CHECK(synthetic IN (0,1)),
  PRIMARY KEY(part_index,source_net_seq,role,ordinal)
) WITHOUT ROWID;
CREATE INDEX endpoint_segments_instance
  ON endpoint_segments(part_index,source_instance_seq);
CREATE TABLE ports(
  part_index INTEGER NOT NULL,
  id TEXT NOT NULL,
  record_json TEXT NOT NULL,
  PRIMARY KEY(part_index,id)
) WITHOUT ROWID;
CREATE TABLE resources(
  part_index INTEGER NOT NULL,
  resource TEXT NOT NULL,
  amount INTEGER NOT NULL,
  PRIMARY KEY(part_index,resource)
) WITHOUT ROWID;
CREATE TABLE cut_sinks(
  net_id TEXT NOT NULL,
  source_part_index INTEGER NOT NULL,
  sink_part_index INTEGER NOT NULL,
  PRIMARY KEY(net_id,sink_part_index)
) WITHOUT ROWID;
CREATE TABLE exact_cut_sources(
  net_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  segment_id TEXT NOT NULL,
  PRIMARY KEY(net_id,ordinal)
) WITHOUT ROWID;
CREATE TABLE exact_cut_nodes(
  net_id TEXT PRIMARY KEY
) WITHOUT ROWID;
CREATE TABLE exact_logic_segments(
  source_cut_net TEXT NOT NULL,
  part_index INTEGER NOT NULL,
  segment_id TEXT NOT NULL,
  PRIMARY KEY(source_cut_net,part_index,segment_id)
) WITHOUT ROWID;
"""


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _sqlite_uri(path: Path) -> str:
    return f"file:{path.resolve()}?mode=ro&immutable=1"


def _source_metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    metadata = dict(connection.execute("SELECT key,value FROM src.metadata"))
    try:
        return {
            "logical_schema": metadata["logical_schema"],
            "design": json.loads(metadata["design_json"]),
            "warnings": json.loads(metadata["warnings_json"]),
            "record_counts": json.loads(
                metadata.get("record_counts_json", "{}")
            ),
            "clocks": [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT record_json FROM src.clocks ORDER BY seq"
                )
            ],
        }
    except (KeyError, json.JSONDecodeError) as error:
        raise ValidationError("EmuIR SQLite metadata is malformed") from error


def read_compact_emuir_control_metadata(path: Path) -> dict[str, Any]:
    """Read only the bounded Phase 6 control-plane subset of EmuIR."""

    connection = sqlite3.connect(_sqlite_uri(path), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise ValidationError(
                "streaming Phase 6 requires compact EmuIR SQLite v2"
            )
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        return {
            "logical_schema": metadata["logical_schema"],
            "design": json.loads(metadata["design_json"]),
            "warnings": json.loads(metadata["warnings_json"]),
            "record_counts": json.loads(
                metadata.get("record_counts_json", "{}")
            ),
            "clocks": [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT record_json FROM clocks ORDER BY seq"
                )
            ],
        }
    except (KeyError, json.JSONDecodeError) as error:
        raise ValidationError("EmuIR SQLite metadata is malformed") from error
    finally:
        connection.close()


def _schedule_incoming_rows(
    schedule: Mapping[str, Any], part_index: Mapping[str, int]
) -> list[tuple[str, int, str]]:
    routes = schedule.get("routes")
    entries = schedule.get("entries")
    if not isinstance(routes, list) or not isinstance(entries, list):
        raise ValidationError("Phase 6 schedule lacks route metadata")
    net_by_demand = {
        route["id"]: route["net"]
        for route in routes
        if isinstance(route, Mapping)
        and isinstance(route.get("id"), str)
        and isinstance(route.get("net"), str)
    }
    incoming: dict[tuple[str, int], str] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValidationError("Phase 6 schedule entry is malformed")
        demand = entry.get("demand")
        target = entry.get("to")
        entry_id = entry.get("id")
        if (
            demand not in net_by_demand
            or target not in part_index
            or not isinstance(entry_id, str)
        ):
            raise ValidationError("Phase 6 schedule entry is incomplete")
        key = (net_by_demand[demand], part_index[target])
        if key in incoming:
            raise ValidationError(
                "a routed demand has multiple incoming hops at one FPGA"
            )
        incoming[key] = entry_id
    return [
        (net_id, target, entry_id)
        for (net_id, target), entry_id in sorted(incoming.items())
    ]


def build_streaming_split_metadata(
    assignment_index_path: Path,
    schedule: Mapping[str, Any],
    platform: Platform,
    *,
    design: Mapping[str, Any],
    clocks: list[Mapping[str, Any]],
    pin_plan: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build only bounded Phase 6 control-plane artifacts.

    Circuit instances, nets, and pins are deliberately absent from this
    function.  They remain in SQLite and are split by
    :func:`write_split_netlists_sqlite`.
    """

    if schedule.get("schema") != TDM_SCHEDULE_SCHEMA:
        raise ValidationError("schedule has an unsupported schema")
    design_name = design.get("name")
    if schedule.get("design") != design_name:
        raise ValidationError("schedule design disagrees with EmuIR")
    if schedule.get("platform") != platform.name:
        raise ValidationError("schedule platform disagrees with BoardDB")

    assignment = open_phase3_assignment_index(assignment_index_path)
    try:
        assignment_metadata = {
            row["key"]: json.loads(row["value"])
            for row in assignment.execute("SELECT key,value FROM metadata")
        }
        parts = list(
            assignment.execute(
                "SELECT part_index,fpga FROM parts ORDER BY part_index"
            )
        )
        exact_nodes = {
            row["net_id"]
            for row in assignment.execute("SELECT net_id FROM exact_cut_nodes")
        }
        source_segments: dict[str, list[str]] = defaultdict(list)
        for row in assignment.execute(
            "SELECT net_id,segment_id FROM exact_cut_sources "
            "ORDER BY net_id,ordinal"
        ):
            source_segments[row["net_id"]].append(row["segment_id"])
        consumer_segments: dict[tuple[str, int], list[str]] = defaultdict(list)
        for row in assignment.execute(
            "SELECT source_cut_net,part_index,segment_id "
            "FROM exact_logic_segments "
            "ORDER BY source_cut_net,part_index,segment_id"
        ):
            consumer_segments[
                (row["source_cut_net"], row["part_index"])
            ].append(row["segment_id"])
    finally:
        assignment.close()
    if assignment_metadata.get("design") != design_name:
        raise ValidationError("assignment index design disagrees with EmuIR")
    if assignment_metadata.get("platform") != platform.name:
        raise ValidationError("assignment index platform disagrees with BoardDB")

    fpga_ids = [row["fpga"] for row in parts]
    part_index = {row["fpga"]: row["part_index"] for row in parts}
    if set(fpga_ids) != {fpga.id for fpga in platform.fpgas}:
        raise ValidationError("assignment partitions disagree with BoardDB")
    exact_digest = assignment_metadata.get("semantic_contract_sha256")
    exact_mode = exact_digest is not None
    if exact_mode:
        if (
            not is_sampled_virtual_wire_schedule(schedule)
            or schedule.get("semantic_contract_sha256") != exact_digest
        ):
            raise ValidationError(
                "exact split inputs do not share one semantic contract"
            )

    physical_lane_by_entry: dict[str, int] = {}
    if pin_plan is not None:
        if pin_plan.get("schema") != "emuflow.placement-aware-pin-plan/v1":
            raise ValidationError("pin plan has an unsupported schema")
        physical_lane_by_entry = {
            item["schedule_entry"]: item["physical_lane"]
            for item in pin_plan.get("entries", [])
        }
        expected_entries = {item["id"] for item in schedule["entries"]}
        if set(physical_lane_by_entry) != expected_entries:
            raise ValidationError(
                "pin plan must cover every schedule entry exactly"
            )

    route_by_demand = {
        route["id"]: route for route in schedule_routes(schedule)
    }
    endpoints_by_fpga: dict[str, list[dict[str, Any]]] = {
        fpga: [] for fpga in fpga_ids
    }
    lane_entries: list[dict[str, Any]] = []
    for entry in sorted(schedule["entries"], key=lambda item: item["id"]):
        route = route_by_demand.get(entry["demand"])
        if route is None:
            raise ValidationError(
                f"schedule entry {entry['id']!r} has no route metadata"
            )
        if entry["from"] not in part_index or entry["to"] not in part_index:
            raise ValidationError("schedule entry references an unknown FPGA")
        tx_id = _endpoint_id("tx", entry["id"])
        rx_id = _endpoint_id("rx", entry["id"])
        tx_signal = (
            f"net:{entry['net']}"
            if entry["from"] == route["source"]
            else f"shadow:{entry['demand']}:{entry['from']}"
        )
        rx_signal = f"shadow:{entry['demand']}:{entry['to']}"
        common = {
            "schedule_entry": entry["id"],
            "demand": entry["demand"],
            "net": entry["net"],
            "link": entry["link"],
            "slot": entry["slot"],
            "arrival_slot": entry["arrival_slot"],
            "lane": physical_lane_by_entry.get(entry["id"], entry["lane"]),
            "logical_lane": entry["lane"],
        }
        exact_tx: dict[str, Any] = {}
        exact_rx: dict[str, Any] = {}
        if exact_mode:
            if entry["net"] not in exact_nodes:
                raise ValidationError("exact schedule references an unknown cut node")
            source_entry = entry["from"] == route["source"]
            exact_tx = {
                "boundary_identity": f"static_exact:tx:{entry['id']}",
                "semantic_contract_sha256": exact_digest,
                "source_role": (
                    "cut_source" if source_entry else "route_tree_relay"
                ),
                "source_logic_segment_ids": (
                    list(source_segments.get(entry["net"], []))
                    if source_entry
                    else []
                ),
                "preserve": True,
            }
            exact_rx = {
                "boundary_identity": f"static_exact:rx:{entry['id']}",
                "semantic_contract_sha256": exact_digest,
                "consumer_logic_segment_ids": list(
                    consumer_segments.get(
                        (entry["net"], part_index[entry["to"]]), []
                    )
                ),
                "preserve": True,
            }
        endpoints_by_fpga[entry["from"]].append({
            "id": tx_id,
            "kind": "tx",
            "fpga": entry["from"],
            "peer": entry["to"],
            "signal": tx_signal,
            **common,
            **exact_tx,
        })
        endpoints_by_fpga[entry["to"]].append({
            "id": rx_id,
            "kind": "rx",
            "fpga": entry["to"],
            "peer": entry["from"],
            "signal": rx_signal,
            **common,
            **exact_rx,
        })
        lane_entries.append({
            "id": f"lane_{entry['id']}",
            "schedule_entry": entry["id"],
            "demand": entry["demand"],
            "net": entry["net"],
            "link": entry["link"],
            "from": entry["from"],
            "to": entry["to"],
            "slot": entry["slot"],
            "lane": physical_lane_by_entry.get(entry["id"], entry["lane"]),
            "logical_lane": entry["lane"],
            "tx_endpoint": tx_id,
            "rx_endpoint": rx_id,
        })

    clock_order = sorted(item["id"] for item in clocks) or ["clk"]
    transports: dict[str, dict[str, Any]] = {}
    anchors: dict[str, dict[str, Any]] = {}
    for fpga in fpga_ids:
        local_endpoints = sorted(
            endpoints_by_fpga[fpga], key=lambda item: item["id"]
        )
        source_signals = sorted({
            endpoint["signal"]
            for endpoint in local_endpoints
            if endpoint["kind"] == "tx"
            and endpoint["signal"].startswith("net:")
        })
        shadow_signals = sorted({
            f"shadow:{endpoint['demand']}:{fpga}"
            for endpoint in local_endpoints
            if endpoint["kind"] == "rx"
        })
        transports[fpga] = {
            "schema": TRANSPORT_ENDPOINTS_SCHEMA,
            "design": design_name,
            "platform": platform.name,
            "fpga": fpga,
            "frame_slots": schedule["metrics"]["frame_slots"],
            "virtual_clock_order": clock_order,
            "source_signals": [
                {"index": index, "signal": signal}
                for index, signal in enumerate(source_signals)
            ],
            "shadow_signals": [
                {"index": index, "signal": signal}
                for index, signal in enumerate(shadow_signals)
            ],
            "endpoints": local_endpoints,
            **({
                "cut_mode": "static-exact-combinational",
                "semantic_contract_sha256": exact_digest,
                "shadow_reset_value": 0,
                "startup_policy": "first-post-reset-frame-current-arrivals",
            } if exact_mode else {}),
        }
        anchors[fpga] = _build_virtual_anchors(
            fpga, platform, local_endpoints
        )

    lane_map = {
        "schema": LOGICAL_LANE_MAP_SCHEMA,
        "design": design_name,
        "platform": platform.name,
        "frame_slots": schedule["metrics"]["frame_slots"],
        "binding_status": "logical_only",
        "entries": sorted(lane_entries, key=lambda item: item["id"]),
        **({
            "cut_mode": "static-exact-combinational",
            "semantic_contract_sha256": exact_digest,
        } if exact_mode else {}),
    }
    manifest = {
        "schema": SPLIT_MANIFEST_SCHEMA,
        "design": design_name,
        "platform": platform.name,
        "provider": (
            (
                "chimew-electrical-pin-split-v1"
                if pin_plan.get("provider")
                == "chimew-paper-plus-emuflow-electrical-slot-v1"
                else "placement-aware-pin-split-v1"
            )
            if pin_plan is not None
            else "sqlite-streaming-cut-shadow-split-v1"
        ),
        "board_binding": {
            "status": "virtual",
            "requires_hardware_bsp_for_package_pins": True,
        },
        "netlist_storage": "split-netlists.sqlite",
        "fpgas": [{
            "fpga": fpga,
            "netlist": "split-netlists.sqlite",
            "netlist_selector": fpga,
            "transport": f"{fpga}/transport.json",
            "transport_rtl": f"{fpga}/transport_schedule.sv",
            "virtual_anchors": f"{fpga}/virtual_anchors.json",
            "virtual_xdc_template": f"{fpga}/virtual_anchors.xdc.template",
        } for fpga in fpga_ids],
        "lane_map": "lane_map.json",
        "runtime_controller_rtl": "virtual_runtime_controller.sv",
        **({
            "cut_mode": "static-exact-combinational",
            "qualification": "exact-boundary-materialization-pass",
            "semantic_contract_sha256": exact_digest,
        } if exact_mode else {}),
        **({
            "pin_plan": "pin_plan.json",
            "position_hints": "position_hints.json",
        } if pin_plan is not None else {}),
    }
    return {
        "manifest": manifest,
        "lane_map": lane_map,
        "transports": transports,
        "anchors": anchors,
    }


def write_split_netlists_sqlite(
    output_path: Path,
    ir_path: Path,
    assignment_index_path: Path,
    schedule: Mapping[str, Any],
    *,
    expected_design: str,
    expected_platform: str,
) -> dict[str, Any]:
    """Join and split a complete EmuIR without Python graph materialization."""

    assignment = open_phase3_assignment_index(assignment_index_path)
    try:
        assignment_metadata = {
            row["key"]: json.loads(row["value"])
            for row in assignment.execute("SELECT key,value FROM metadata")
        }
        parts = list(
            assignment.execute(
                "SELECT part_index,fpga FROM parts ORDER BY part_index"
            )
        )
    finally:
        assignment.close()
    if assignment_metadata.get("schema") != ASSIGNMENT_INDEX_SCHEMA:
        raise ValidationError("Phase 6 assignment index schema is invalid")
    if assignment_metadata.get("design") != expected_design:
        raise ValidationError("Phase 6 assignment index design disagrees")
    if assignment_metadata.get("platform") != expected_platform:
        raise ValidationError("Phase 6 assignment index platform disagrees")
    part_index = {row["fpga"]: row["part_index"] for row in parts}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(
        f".{output_path.name}.{uuid.uuid4().hex}.tmp"
    )
    # The source and assignment databases are attached as immutable file URIs.
    connection = sqlite3.connect(temporary, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute("PRAGMA cache_size=-131072")
        connection.execute(f"PRAGMA application_id={SPLIT_NETLIST_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={SPLIT_NETLIST_USER_VERSION}")
        connection.executescript(DDL)
        connection.execute("ATTACH DATABASE ? AS src", (_sqlite_uri(ir_path),))
        connection.execute(
            "ATTACH DATABASE ? AS assignment",
            (_sqlite_uri(assignment_index_path),),
        )
        if connection.execute("PRAGMA src.user_version").fetchone()[0] != 2:
            raise ValidationError(
                "streaming Phase 6 requires compact EmuIR SQLite v2"
            )
        source = _source_metadata(connection)
        if source["design"].get("name") != expected_design:
            raise ValidationError("Phase 6 EmuIR design disagrees")

        metadata = {
            "schema": SPLIT_NETLIST_STORAGE_SCHEMA,
            "design": source["design"],
            "logical_schema": source["logical_schema"],
            "warnings": source["warnings"],
            "clocks": source["clocks"],
            "platform": expected_platform,
            "semantic_contract_sha256": assignment_metadata.get(
                "semantic_contract_sha256"
            ),
        }
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES (?,?)",
            ((key, _json(value)) for key, value in sorted(metadata.items())),
        )
        connection.execute(
            "INSERT INTO parts SELECT part_index,fpga FROM assignment.parts"
        )
        # Preserve the compact EmuIR dictionaries exactly once.  Part-specific
        # rows below carry integer references instead of repeating JSON and
        # pin strings for every endpoint or instance copy.
        connection.execute("INSERT INTO payloads SELECT * FROM src.payloads")
        connection.execute("INSERT INTO pin_names SELECT * FROM src.pin_names")
        connection.execute(
            "INSERT OR IGNORE INTO pin_names(name) VALUES ('shadow_out')"
        )
        connection.execute(
            """
            INSERT INTO source_instances
            SELECT c.seq,c.id,c.name,c.flags,c.type,c.resources_seq,
                   c.parameters_seq,c.attributes_seq,c.constants_seq,
                   c.extras_seq
            FROM src.cell_records c
            WHERE EXISTS(
              SELECT 1 FROM assignment.copies cp
              WHERE cp.source_instance_seq=c.seq
            )
            """
        )
        connection.execute(
            """
            INSERT INTO instance_copies
            SELECT cp.part_index,cp.source_instance_seq,
                   CASE WHEN cp.is_replica=1 THEN cp.copy_id ELSE NULL END,
                   cp.is_replica
            FROM assignment.copies cp
            """
        )
        # Materialize endpoint ownership once.  Derive the much smaller set of
        # touched (partition, net) pairs from that ordered output rather than
        # running correlated source-graph queries once per endpoint.
        connection.execute(
            """
            INSERT INTO endpoint_segments
            SELECT cp.part_index,e.net_seq,e.role,e.ordinal,e.instance_seq,
                   NULL,e.port_seq,e.bit,e.extras_seq,0
            FROM src.endpoint_records e
            JOIN assignment.copies cp
              ON cp.source_instance_seq=e.instance_seq
            """
        )
        connection.execute(
            """
            CREATE TEMP TABLE replica_net_parts(
              part_index INTEGER NOT NULL,
              source_net_seq INTEGER NOT NULL,
              PRIMARY KEY(part_index,source_net_seq)
            ) WITHOUT ROWID
            """
        )
        has_replicas = connection.execute(
            "SELECT 1 FROM assignment.copies WHERE is_replica=1 LIMIT 1"
        ).fetchone()
        if has_replicas is not None:
            connection.execute(
                """
                INSERT INTO replica_net_parts
                SELECT DISTINCT cp.part_index,e.net_seq
                FROM assignment.copies cp
                JOIN src.endpoint_records e INDEXED BY endpoint_owner
                  ON e.instance_seq=cp.source_instance_seq
                WHERE cp.is_replica=1 AND e.role=0
                """
            )
        connection.execute(
            """
            INSERT INTO net_segments
            SELECT t.part_index,t.source_net_seq,
                   CASE
                     WHEN cs.net_id IS NOT NULL THEN 'transport_shadow'
                     WHEN rp.source_net_seq IS NOT NULL THEN 'logic_replica'
                     ELSE 'original'
                   END
            FROM (
              SELECT part_index,source_net_seq
              FROM endpoint_segments
              GROUP BY part_index,source_net_seq
            ) t
            JOIN src.net_records n ON n.seq=t.source_net_seq
            LEFT JOIN assignment.cut_sinks cs
              ON cs.net_id=n.id AND cs.sink_part_index=t.part_index
            LEFT JOIN replica_net_parts rp
              ON rp.part_index=t.part_index
             AND rp.source_net_seq=t.source_net_seq
            """
        )
        connection.execute(
            """
            INSERT INTO source_nets
            SELECT n.seq,n.id,n.name,n.flags,n.cut_class
            FROM (
              SELECT source_net_seq FROM net_segments
              GROUP BY source_net_seq
            ) t
            JOIN src.net_records n ON n.seq=t.source_net_seq
            """
        )
        connection.execute(
            """
            INSERT INTO endpoint_segments
            SELECT s.part_index,e.net_seq,e.role,e.ordinal,NULL,NULL,
                   e.port_seq,e.bit,e.extras_seq,0
            FROM net_segments s
            JOIN src.endpoint_records e
              ON e.net_seq=s.source_net_seq AND e.instance_seq IS NULL
            """
        )

        connection.execute(
            "CREATE TEMP TABLE incoming(net_id TEXT,part_index INTEGER,"
            "entry_id TEXT,PRIMARY KEY(net_id,part_index)) WITHOUT ROWID"
        )
        connection.executemany(
            "INSERT INTO incoming VALUES (?,?,?)",
            _schedule_incoming_rows(schedule, part_index),
        )
        missing = connection.execute(
            """
            SELECT count(*)
            FROM assignment.cut_sinks cs
            LEFT JOIN incoming i
              ON i.net_id=cs.net_id AND i.part_index=cs.sink_part_index
            WHERE i.entry_id IS NULL
            """
        ).fetchone()[0]
        if missing:
            raise ValidationError(
                "Phase 6 schedule has no incoming hop for a cut sink"
            )
        connection.execute(
            """
            INSERT INTO endpoint_segments
            SELECT n.part_index,n.source_net_seq,0,
                   COALESCE((
                     SELECT max(e.ordinal)+1 FROM endpoint_segments e
                     WHERE e.part_index=n.part_index
                       AND e.source_net_seq=n.source_net_seq AND e.role=0
                   ),0),
                   NULL,'__emuflow_rx_' || i.entry_id,
                   (SELECT seq FROM pin_names WHERE name='shadow_out'),0,
                   (SELECT seq FROM payloads WHERE value='{}'),1
            FROM net_segments n
            JOIN source_nets sn ON sn.source_net_seq=n.source_net_seq
            JOIN assignment.cut_sinks cs
              ON cs.net_id=sn.original_net
             AND cs.sink_part_index=n.part_index
            JOIN incoming i
              ON i.net_id=sn.original_net AND i.part_index=n.part_index
            """
        )
        connection.execute(
            """
            INSERT INTO ports
            SELECT DISTINCT s.part_index,
                   json_extract(p.record_json,'$.id'),p.record_json
            FROM net_segments s
            JOIN src.endpoint_records e
              ON e.net_seq=s.source_net_seq AND e.instance_seq IS NULL
            JOIN src.pin_names pn ON pn.seq=e.port_seq
            JOIN src.port_records p
              ON json_extract(p.record_json,'$.id')=pn.name
            """
        )
        connection.execute(
            """
            INSERT INTO resources
            SELECT c.part_index,j.key,sum(CAST(j.value AS INTEGER))
            FROM instance_copies c
            JOIN source_instances i
              ON i.source_instance_seq=c.source_instance_seq
            JOIN payloads p ON p.seq=i.resources_seq,
                 json_each(p.value) j
            GROUP BY c.part_index,j.key
            HAVING sum(CAST(j.value AS INTEGER)) != 0
            """
        )
        connection.execute(
            "INSERT INTO cut_sinks SELECT * FROM assignment.cut_sinks"
        )
        connection.execute(
            "INSERT INTO exact_cut_nodes SELECT * FROM assignment.exact_cut_nodes"
        )
        connection.execute(
            "INSERT INTO exact_cut_sources "
            "SELECT * FROM assignment.exact_cut_sources"
        )
        connection.execute(
            "INSERT INTO exact_logic_segments "
            "SELECT * FROM assignment.exact_logic_segments"
        )
        connection.execute("DROP TABLE replica_net_parts")
        connection.execute("DROP TABLE incoming")
        connection.commit()
        summary = validate_split_netlists_sqlite(
            temporary,
            ir_path,
            assignment_index_path,
            expected_design=expected_design,
            expected_platform=expected_platform,
        )
        connection.close()
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, output_path)
        return summary
    except BaseException:
        try:
            connection.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise


def open_split_netlists_sqlite(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    if (
        connection.execute("PRAGMA application_id").fetchone()[0]
        != SPLIT_NETLIST_APPLICATION_ID
        or connection.execute("PRAGMA user_version").fetchone()[0]
        != SPLIT_NETLIST_USER_VERSION
    ):
        connection.close()
        raise ValidationError("unsupported split-netlist SQLite artifact")
    return connection


class SplitNetlistView:
    """Streaming view of one selected FPGA in the shared split database."""

    def __init__(
        self,
        path: Path,
        fpga: str,
        *,
        compact_owner_hints: bool = False,
    ):
        self.connection = open_split_netlists_sqlite(path)
        self.compact_owner_hints = compact_owner_hints
        try:
            self.metadata = {
                row["key"]: json.loads(row["value"])
                for row in self.connection.execute(
                    "SELECT key,value FROM metadata"
                )
            }
            part = self.connection.execute(
                "SELECT part_index FROM parts WHERE fpga=?", (fpga,)
            ).fetchone()
            if part is None:
                raise ValidationError(f"split netlists omit FPGA {fpga!r}")
            self.fpga = fpga
            self.part_index = int(part[0])
            self.ports = [
                json.loads(row[0])
                for row in self.connection.execute(
                    "SELECT record_json FROM ports WHERE part_index=? ORDER BY id",
                    (self.part_index,),
                )
            ]
            self.resources = {
                row[0]: int(row[1])
                for row in self.connection.execute(
                    "SELECT resource,amount FROM resources "
                    "WHERE part_index=? ORDER BY resource",
                    (self.part_index,),
                )
            }
            self._source_segments: dict[str, list[str]] = defaultdict(list)
            for row in self.connection.execute(
                "SELECT net_id,segment_id FROM exact_cut_sources "
                "ORDER BY net_id,ordinal"
            ):
                self._source_segments[row[0]].append(row[1])
            self._consumer_segments: dict[str, list[str]] = defaultdict(list)
            for row in self.connection.execute(
                "SELECT source_cut_net,segment_id FROM exact_logic_segments "
                "WHERE part_index=? ORDER BY source_cut_net,segment_id",
                (self.part_index,),
            ):
                self._consumer_segments[row[0]].append(row[1])
            self._cut_source = {
                row[0]: int(row[1])
                for row in self.connection.execute(
                    "SELECT DISTINCT net_id,source_part_index FROM cut_sinks"
                )
            }
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self) -> "SplitNetlistView":
        return self

    def __exit__(self, *_args: object) -> None:
        self.connection.close()

    def instances(self):
        statement = """SELECT c.copy_id,c.is_replica,i.source_instance_seq,
            i.original_id,i.name,i.flags,i.type,
            r.value AS resources_json,p.value AS parameters_json,
            a.value AS attributes_json,k.value AS constants_json,
            x.value AS extras_json
            FROM {from_clause}
            JOIN payloads r ON r.seq=i.resources_seq
            LEFT JOIN payloads p ON p.seq=i.parameters_seq
            LEFT JOIN payloads a ON a.seq=i.attributes_seq
            JOIN payloads k ON k.seq=i.constants_seq
            JOIN payloads x ON x.seq=i.extras_seq
            WHERE c.part_index=? AND c.is_replica={replica}
            ORDER BY {identity}"""
        # Preserve the historical identity order without sorting the complete
        # partition.  The base stream walks source_instances' UNIQUE identity
        # index and probes the compact copy PK; the replica stream walks the
        # existing (part_index,copy_id) index.  A bounded two-way merge then
        # produces the same order as ORDER BY COALESCE(copy_id,original_id).
        base_rows = self.connection.execute(
            statement.format(
                from_clause=(
                    "source_instances i INDEXED BY "
                    "sqlite_autoindex_source_instances_1 CROSS JOIN "
                    "instance_copies c INDEXED BY "
                    "sqlite_autoindex_instance_copies_1 ON "
                    "c.source_instance_seq=i.source_instance_seq"
                ),
                replica=0,
                identity="i.original_id",
            ),
            (self.part_index,),
        )
        replica_rows = self.connection.execute(
            statement.format(
                from_clause=(
                    "instance_copies c INDEXED BY instance_copies_identity "
                    "CROSS JOIN source_instances i ON "
                    "i.source_instance_seq=c.source_instance_seq"
                ),
                replica=1,
                identity="c.copy_id",
            ),
            (self.part_index,),
        )
        for row in heapq.merge(
            base_rows,
            replica_rows,
            key=lambda item: item["copy_id"] or item["original_id"],
        ):
            identity = row["copy_id"] or row["original_id"]
            record = {
                "id": identity,
                "type": row["type"],
                "resources": json.loads(row["resources_json"]),
                "constant_connections": json.loads(row["constants_json"]),
                **json.loads(row["extras_json"]),
            }
            record["name"] = (
                row["original_id"] if row["flags"] & 1 else row["name"]
            )
            if row["parameters_json"] is not None:
                record["parameters"] = json.loads(row["parameters_json"])
            if row["attributes_json"] is not None:
                record["attributes"] = json.loads(row["attributes_json"])
            if row["is_replica"]:
                record["replica_of"] = row["original_id"]
                record["replica_target_fpga"] = self.fpga
            if self.compact_owner_hints:
                record["__compact_source_instance_seq"] = int(
                    row["source_instance_seq"]
                )
            yield record

    def has_net(self, original_net: str) -> bool:
        return self.connection.execute(
            """SELECT 1 FROM net_segments s JOIN source_nets n
            ON n.source_net_seq=s.source_net_seq
            WHERE s.part_index=? AND n.original_net=?""",
            (self.part_index, original_net),
        ).fetchone() is not None

    def counts(self) -> dict[str, int]:
        return {
            "instances": int(self.connection.execute(
                "SELECT count(*) FROM instance_copies WHERE part_index=?",
                (self.part_index,),
            ).fetchone()[0]),
            "nets": int(self.connection.execute(
                "SELECT count(*) FROM net_segments WHERE part_index=?",
                (self.part_index,),
            ).fetchone()[0]),
            "ports": len(self.ports),
        }

    def nets(self):
        endpoints = iter(
            self.connection.execute(
                """SELECT n.original_net,e.source_net_seq,e.role,e.ordinal,e.bit,
                e.synthetic,e.source_instance_seq,
                pn.name AS port,x.value AS extras_json,
                CASE WHEN e.synthetic=1 THEN e.synthetic_instance_id
                     WHEN e.source_instance_seq IS NULL THEN NULL
                     ELSE COALESCE(c.copy_id,i.original_id) END AS instance_id
                FROM source_nets n INDEXED BY sqlite_autoindex_source_nets_1
                CROSS JOIN endpoint_segments e INDEXED BY
                  sqlite_autoindex_endpoint_segments_1
                  ON e.source_net_seq=n.source_net_seq
                JOIN pin_names pn ON pn.seq=e.port_seq
                JOIN payloads x ON x.seq=e.extras_seq
                LEFT JOIN instance_copies c
                  ON c.part_index=e.part_index
                 AND c.source_instance_seq=e.source_instance_seq
                LEFT JOIN source_instances i
                  ON i.source_instance_seq=e.source_instance_seq
                WHERE e.part_index=?
                ORDER BY n.original_net,e.role,e.ordinal""",
                (self.part_index,),
            )
        )
        endpoint = next(endpoints, None)
        exact_digest = self.metadata.get("semantic_contract_sha256")
        for row in self.connection.execute(
            """SELECT s.source_net_seq,s.source_kind,n.original_net,
            CASE WHEN n.flags & 1 THEN n.original_net ELSE n.name END AS name,
            n.cut_class
            FROM source_nets n INDEXED BY sqlite_autoindex_source_nets_1
            CROSS JOIN net_segments s INDEXED BY sqlite_autoindex_net_segments_1
              ON s.source_net_seq=n.source_net_seq
            WHERE s.part_index=? ORDER BY n.original_net""",
            (self.part_index,),
        ):
            record = {
                "id": f"{row['original_net']}@{self.fpga}",
                "original_net": row["original_net"],
                "name": row["name"],
                "cut_class": row["cut_class"],
                "source_kind": row["source_kind"],
                "drivers": [],
                "sinks": [],
            }
            net_endpoints = []
            while endpoint is not None and endpoint["original_net"] == row["original_net"]:
                net_endpoints.append(dict(endpoint))
                endpoint = next(endpoints, None)
            # Match the established logical endpoint order, but sort only one
            # net's pins in memory.  Asking SQLite for this secondary order
            # together with identity order creates a multi-gigabyte external
            # sort for the complete FPGA partition.
            net_endpoints.sort(key=lambda item: (
                item["role"],
                item["synthetic"],
                item["instance_id"] is None,
                item["ordinal"],
            ))
            for item in net_endpoints:
                decoded = {
                    "instance": item["instance_id"],
                    "port": item["port"],
                    "bit": item["bit"],
                    **json.loads(item["extras_json"]),
                }
                if (
                    self.compact_owner_hints
                    and item["source_instance_seq"] is not None
                ):
                    decoded["__compact_source_instance_seq"] = int(
                        item["source_instance_seq"]
                    )
                record[("drivers", "sinks")[item["role"]]].append(decoded)
            if exact_digest is not None and row["original_net"] in self._cut_source:
                if row["source_kind"] == "transport_shadow":
                    role = "transport_shadow_sink"
                    segment_ids = self._consumer_segments.get(row["original_net"], [])
                elif self._cut_source[row["original_net"]] == self.part_index:
                    role = "cut_source"
                    segment_ids = self._source_segments.get(row["original_net"], [])
                else:
                    role, segment_ids = "local_logic", []
                record["static_exact_boundary"] = {
                    "semantic_contract_sha256": exact_digest,
                    "role": role,
                    "logic_segment_ids": segment_ids,
                }
            yield record
        if endpoint is not None:
            raise ValidationError("split netlist contains orphaned endpoints")


def validate_split_netlists_sqlite(
    path: Path,
    ir_path: Path,
    assignment_index_path: Path,
    *,
    expected_design: str,
    expected_platform: str,
) -> dict[str, Any]:
    """Check compact construction invariants without replaying the source DB.

    The writer's INSERT...SELECT statements are the transformation.  Running
    the same multi-gigabyte source joins a second time would be redundant
    replay, not independent evidence.  This check therefore validates output
    constraints, assignment cardinality, and transport-boundary invariants.
    """

    # Keep URI processing enabled for the immutable databases attached below.
    connection = sqlite3.connect(":memory:", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("ATTACH DATABASE ? AS split", (_sqlite_uri(path),))
        connection.execute(
            "ATTACH DATABASE ? AS assignment",
            (_sqlite_uri(assignment_index_path),),
        )
        header = {
            row["key"]: json.loads(row["value"])
            for row in connection.execute(
                "SELECT key,value FROM split.metadata"
            )
        }
        if header.get("schema") != SPLIT_NETLIST_STORAGE_SCHEMA:
            raise ValidationError("split-netlist storage schema is invalid")
        if header.get("design", {}).get("name") != expected_design:
            raise ValidationError("split-netlist design disagrees")
        if header.get("platform") != expected_platform:
            raise ValidationError("split-netlist platform disagrees")

        expected_copies = connection.execute(
            "SELECT count(*) FROM assignment.copies"
        ).fetchone()[0]
        observed_copies = connection.execute(
            "SELECT count(*) FROM split.instance_copies"
        ).fetchone()[0]
        checks = {
            "copy_coverage_errors": abs(expected_copies - observed_copies),
            "net_materialization_errors": connection.execute("""
              SELECT abs(
                (SELECT count(*) FROM split.net_segments) -
                (SELECT count(*) FROM (
                   SELECT part_index,source_net_seq
                   FROM split.endpoint_segments
                   GROUP BY part_index,source_net_seq
                ))
              )
            """).fetchone()[0],
            "orphan_endpoint_errors": """
              SELECT count(*) FROM split.endpoint_segments e
              LEFT JOIN split.instance_copies i
                ON i.part_index=e.part_index
               AND i.source_instance_seq=e.source_instance_seq
              WHERE e.source_instance_seq IS NOT NULL
                AND e.synthetic=0
                AND i.source_instance_seq IS NULL
            """,
            "shadow_driver_errors": """
              SELECT count(*) FROM split.net_segments n
              WHERE n.source_kind='transport_shadow' AND (
                SELECT count(*) FROM split.endpoint_segments e
                WHERE e.part_index=n.part_index
                  AND e.source_net_seq=n.source_net_seq
                  AND e.role=0 AND e.synthetic=1
              ) != 1
            """,
            "unexpected_shadow_errors": """
              SELECT count(*) FROM split.endpoint_segments e
              JOIN split.net_segments n
                ON n.part_index=e.part_index
               AND n.source_net_seq=e.source_net_seq
              WHERE e.synthetic=1 AND n.source_kind!='transport_shadow'
            """,
        }
        results = {
            name: (
                connection.execute(query).fetchone()[0]
                if isinstance(query, str)
                else query
            )
            for name, query in checks.items()
        }
        if any(results.values()):
            raise ValidationError(
                "split-netlist SQL coverage proof failed: "
                + ", ".join(
                    f"{name}={value}"
                    for name, value in results.items()
                    if value
                )
            )
        return {
            "status": "pass",
            "provider": "sqlite-constructive-split-certificate-v2",
            "instances": connection.execute(
                "SELECT count(*) FROM split.instance_copies"
            ).fetchone()[0],
            "replica_instances": connection.execute(
                "SELECT count(*) FROM split.instance_copies WHERE is_replica=1"
            ).fetchone()[0],
            "net_segments": connection.execute(
                "SELECT count(*) FROM split.net_segments"
            ).fetchone()[0],
            "endpoints": connection.execute(
                "SELECT count(*) FROM split.endpoint_segments"
            ).fetchone()[0],
            "synthetic_shadow_drivers": connection.execute(
                "SELECT count(*) FROM split.endpoint_segments WHERE synthetic=1"
            ).fetchone()[0],
            "parts": connection.execute(
                "SELECT count(*) FROM split.parts"
            ).fetchone()[0],
            **results,
        }
    finally:
        connection.close()


def read_split_netlist_sqlite(path: Path, fpga: str) -> dict[str, Any]:
    """Materialize one partition only for a legacy physical consumer.

    Phase 6 itself never calls this function.  It is the bounded transition
    adapter for physical backends that have not yet adopted the SQLite view.
    """

    connection = open_split_netlists_sqlite(path)
    try:
        metadata = {
            row["key"]: json.loads(row["value"])
            for row in connection.execute("SELECT key,value FROM metadata")
        }
        part = connection.execute(
            "SELECT part_index FROM parts WHERE fpga=?", (fpga,)
        ).fetchone()
        if part is None:
            raise ValidationError(f"split netlists omit FPGA {fpga!r}")
        part_index = part["part_index"]
        instances = []
        for row in connection.execute(
            """
            SELECT c.source_instance_seq,c.copy_id,c.is_replica,
                   i.original_id,i.name,i.flags,i.type,
                   r.value AS resources_json,
                   p.value AS parameters_json,
                   a.value AS attributes_json,
                   k.value AS constants_json,
                   x.value AS extras_json
            FROM instance_copies c
            JOIN source_instances i
              ON i.source_instance_seq=c.source_instance_seq
            JOIN payloads r ON r.seq=i.resources_seq
            LEFT JOIN payloads p ON p.seq=i.parameters_seq
            LEFT JOIN payloads a ON a.seq=i.attributes_seq
            JOIN payloads k ON k.seq=i.constants_seq
            JOIN payloads x ON x.seq=i.extras_seq
            WHERE c.part_index=?
            ORDER BY CASE WHEN c.copy_id IS NOT NULL
                          THEN c.copy_id ELSE i.original_id END
            """,
            (part_index,),
        ):
            instance_id = row["copy_id"] or row["original_id"]
            record = {
                "id": instance_id,
                "type": row["type"],
                "resources": json.loads(row["resources_json"]),
                "constant_connections": json.loads(row["constants_json"]),
                **json.loads(row["extras_json"]),
            }
            if row["flags"] & 1:
                record["name"] = row["original_id"]
            elif row["name"] is not None:
                record["name"] = row["name"]
            if row["parameters_json"] is not None:
                record["parameters"] = json.loads(row["parameters_json"])
            if row["attributes_json"] is not None:
                record["attributes"] = json.loads(row["attributes_json"])
            if row["is_replica"]:
                record["replica_of"] = row["original_id"]
                record["replica_target_fpga"] = fpga
            instances.append(record)

        source_segments: dict[str, list[str]] = {}
        for row in connection.execute(
            "SELECT net_id,segment_id FROM exact_cut_sources ORDER BY net_id,ordinal"
        ):
            source_segments.setdefault(row["net_id"], []).append(
                row["segment_id"]
            )
        consumer_segments: dict[str, list[str]] = {}
        for row in connection.execute(
            "SELECT source_cut_net,segment_id FROM exact_logic_segments "
            "WHERE part_index=? ORDER BY source_cut_net,segment_id",
            (part_index,),
        ):
            consumer_segments.setdefault(row["source_cut_net"], []).append(
                row["segment_id"]
            )
        cut_source = {
            row["net_id"]: row["source_part_index"]
            for row in connection.execute(
                "SELECT DISTINCT net_id,source_part_index FROM cut_sinks"
            )
        }
        exact_digest = metadata.get("semantic_contract_sha256")
        nets = []
        for row in connection.execute(
            """
            SELECT s.source_net_seq,s.source_kind,n.original_net,
                   CASE WHEN n.flags & 1 THEN n.original_net ELSE n.name END
                     AS name,
                   n.cut_class
            FROM net_segments s
            JOIN source_nets n ON n.source_net_seq=s.source_net_seq
            WHERE s.part_index=?
            ORDER BY n.original_net
            """,
            (part_index,),
        ):
            endpoints = list(
                connection.execute(
                    """
                    SELECT e.role,e.ordinal,e.bit,e.synthetic,
                           pn.name AS port,x.value AS extras_json,
                           CASE
                             WHEN e.synthetic=1 THEN e.synthetic_instance_id
                             WHEN e.source_instance_seq IS NULL THEN NULL
                             ELSE COALESCE(c.copy_id,i.original_id)
                           END AS instance_id
                    FROM endpoint_segments e
                    JOIN pin_names pn ON pn.seq=e.port_seq
                    JOIN payloads x ON x.seq=e.extras_seq
                    LEFT JOIN instance_copies c
                      ON c.part_index=e.part_index
                     AND c.source_instance_seq=e.source_instance_seq
                    LEFT JOIN source_instances i
                      ON i.source_instance_seq=e.source_instance_seq
                    WHERE e.part_index=? AND e.source_net_seq=?
                    ORDER BY e.role,e.synthetic,
                             (CASE
                                WHEN e.synthetic=1
                                  THEN e.synthetic_instance_id
                                WHEN e.source_instance_seq IS NULL THEN NULL
                                ELSE COALESCE(c.copy_id,i.original_id)
                              END IS NULL),
                             e.ordinal
                    """,
                    (part_index, row["source_net_seq"]),
                )
            )
            record = {
                "id": f"{row['original_net']}@{fpga}",
                "original_net": row["original_net"],
                "name": row["name"],
                "cut_class": row["cut_class"],
                "source_kind": row["source_kind"],
                "drivers": [],
                "sinks": [],
            }
            for endpoint in endpoints:
                value = {
                    "instance": endpoint["instance_id"],
                    "port": endpoint["port"],
                    "bit": endpoint["bit"],
                    **json.loads(endpoint["extras_json"]),
                }
                record[("drivers", "sinks")[endpoint["role"]]].append(value)
            if exact_digest is not None and row["original_net"] in cut_source:
                if row["source_kind"] == "transport_shadow":
                    role = "transport_shadow_sink"
                    segment_ids = consumer_segments.get(
                        row["original_net"], []
                    )
                elif cut_source[row["original_net"]] == part_index:
                    role = "cut_source"
                    segment_ids = source_segments.get(row["original_net"], [])
                else:
                    role = "local_logic"
                    segment_ids = []
                record["static_exact_boundary"] = {
                    "semantic_contract_sha256": exact_digest,
                    "role": role,
                    "logic_segment_ids": segment_ids,
                }
            nets.append(record)

        return {
            "schema": "emuflow.fpga-netlist/v1",
            "design": metadata["design"],
            "platform": metadata["platform"],
            "fpga": fpga,
            "ports": [
                json.loads(row["record_json"])
                for row in connection.execute(
                    "SELECT record_json FROM ports WHERE part_index=? ORDER BY id",
                    (part_index,),
                )
            ],
            "instances": instances,
            "nets": nets,
            "resources": {
                row["resource"]: row["amount"]
                for row in connection.execute(
                    "SELECT resource,amount FROM resources "
                    "WHERE part_index=? ORDER BY resource",
                    (part_index,),
                )
            },
            **(
                {"semantic_contract_sha256": exact_digest}
                if exact_digest is not None
                else {}
            ),
        }
    finally:
        connection.close()
