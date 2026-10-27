"""Typed split readers and validators; production splitting is native."""

from __future__ import annotations

import json
import heapq
import sqlite3
from pathlib import Path
from collections import defaultdict
from typing import Any, Mapping

from .sqlite_artifacts import immutable_sqlite_uri
from .errors import ValidationError
from .phase3_assignment_index import (
    open_phase3_assignment_index,
)
from .phase3_exact_binary import (
    iter_phase3_exact_node_sources,
    iter_phase3_exact_segments,
    phase3_exact_binary_counts,
)


PREVIOUS_SPLIT_NETLIST_STORAGE_SCHEMA = "emuflow.split-netlists-sqlite/v2"
SPLIT_NETLIST_STORAGE_SCHEMA = "emuflow.split-netlists-sqlite/v3"
SPLIT_NETLIST_APPLICATION_ID = 0x454D5553  # ASCII "EMUS"
PREVIOUS_SPLIT_NETLIST_USER_VERSION = 2
SPLIT_NETLIST_USER_VERSION = 3


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
    return immutable_sqlite_uri(path)


def _binary_artifact_path(
    database_path: Path, descriptor: Mapping[str, Any]
) -> Path:
    artifact = descriptor.get("artifact")
    if not isinstance(artifact, str) or Path(artifact).name != artifact:
        raise ValidationError("Static Exact binary path is invalid")
    from .split_overlay import split_binary_path
    return split_binary_path(database_path,descriptor)


def _exact_relation_maps(
    path: Path,
    metadata: Mapping[str, Any],
    fpga: str,
    part_index: int,
    connection: sqlite3.Connection,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    descriptor = metadata.get("semantic_contract_binary")
    source_segments: dict[str, list[str]] = defaultdict(list)
    consumer_segments: dict[str, list[str]] = defaultdict(list)
    local_source_nets = {
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT net_id FROM cut_sinks "
            "WHERE source_part_index=?",
            (part_index,),
        )
    }
    if isinstance(descriptor, Mapping):
        binary_path = _binary_artifact_path(path, descriptor)
        for net_id, numbers in iter_phase3_exact_node_sources(
            binary_path, descriptor
        ):
            if net_id in local_source_nets:
                source_segments[net_id] = [
                    f"segment{number:06d}" for number in numbers
                ]
        for segment in iter_phase3_exact_segments(
            binary_path, descriptor, fpga
        ):
            source_net = segment.get("source_cut_net")
            if isinstance(source_net, str):
                consumer_segments[source_net].append(segment["id"])
        return source_segments, consumer_segments

    for row in connection.execute(
        "SELECT x.net_id,x.segment_id FROM exact_cut_sources x "
        "JOIN cut_sinks c ON c.net_id=x.net_id "
        "WHERE c.source_part_index=? "
        "GROUP BY x.net_id,x.ordinal ORDER BY x.net_id,x.ordinal",
        (part_index,),
    ):
        source_segments[row[0]].append(row[1])
    for row in connection.execute(
        "SELECT source_cut_net,segment_id FROM exact_logic_segments "
        "WHERE part_index=? ORDER BY source_cut_net,segment_id",
        (part_index,),
    ):
        consumer_segments[row[0]].append(row[1])
    return source_segments, consumer_segments


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


def read_split_resource_reservations(assignment_index_path: Path) -> dict[str, Any]:
    """Read the bounded resource seal without loading assignment ownership."""
    connection = open_phase3_assignment_index(assignment_index_path)
    try:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='constraints'"
        ).fetchone()
    finally:
        connection.close()
    constraints = json.loads(row[0]) if row is not None else {}
    if not isinstance(constraints, dict):
        raise ValidationError("Phase 3 resource constraints are malformed")
    reservations = constraints.get("resource_reservations_by_fpga", {})
    if not isinstance(reservations, dict) or any(
        not isinstance(value, dict) for value in reservations.values()
    ):
        raise ValidationError("Phase 3 resource reservations are malformed")
    return reservations


def write_split_netlists_sqlite(
    output_path: Path,ir_path: Path,assignment_index_path: Path,
    schedule: Mapping[str,Any],*,expected_design: str,expected_platform: str,
) -> dict[str,Any]:
    """Publish source-bound memberships without a copied DUT population."""
    from .split_overlay import build_native_split
    return build_native_split(output_path,ir_path,assignment_index_path,schedule,
        expected_design=expected_design,expected_platform=expected_platform)


def open_split_netlists_sqlite(path: Path) -> sqlite3.Connection:
    from .split_overlay import SplitProjectionConnection,attach_split_projection
    connection = sqlite3.connect(
        immutable_sqlite_uri(path), uri=True, factory=SplitProjectionConnection
    )
    connection.row_factory = sqlite3.Row
    if (
        connection.execute("PRAGMA application_id").fetchone()[0]
        != SPLIT_NETLIST_APPLICATION_ID
        or connection.execute("PRAGMA user_version").fetchone()[0]
        not in (PREVIOUS_SPLIT_NETLIST_USER_VERSION,SPLIT_NETLIST_USER_VERSION)
    ):
        connection.close()
        raise ValidationError("unsupported split-netlist SQLite artifact")
    try:
        attach_split_projection(connection,path)
    except BaseException:
        connection.close()
        raise
    return connection


class SplitNetlistView:
    """Streaming view of one selected FPGA in the shared split database."""

    def __init__(
        self,
        path: Path,
        fpga: str,
    ):
        self.path = path
        self.connection = open_split_netlists_sqlite(path)
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
            yield record

    def has_net(self, original_net: str) -> bool:
        return self.connection.execute(
            """SELECT 1 FROM net_segments s JOIN source_nets n
            ON n.source_net_seq=s.source_net_seq
            WHERE s.part_index=? AND n.original_net=?""",
            (self.part_index, original_net),
        ).fetchone() is not None

    def net_ids(self) -> set[str]:
        """Return the logical net identities present in this partition.

        Large transport overlays need to test hundreds of thousands of net
        identities while lowering a partition.  Materializing this compact
        identity set once avoids one indexed SQLite statement per crossing.
        """

        return {
            str(row[0])
            for row in self.connection.execute(
                """SELECT n.original_net
                FROM net_segments s
                JOIN source_nets n ON n.source_net_seq=s.source_net_seq
                WHERE s.part_index=?""",
                (self.part_index,),
            )
        }

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
        # Full exact segment membership is needed only by the explicit legacy
        # JSON expansion API.  Production placement lowering joins the split
        # database directly and must not pay this graph-sized read at view
        # construction time.
        source_segments: dict[str, list[str]] = {}
        consumer_segments: dict[str, list[str]] = {}
        cut_source = {}
        if self.metadata.get("semantic_contract_sha256") is not None:
            cut_source = {
                row[0]: int(row[1]) for row in self.connection.execute(
                    "SELECT DISTINCT net_id,source_part_index FROM cut_sinks"
                )
            }
            source_segments, consumer_segments = _exact_relation_maps(
                self.path,
                self.metadata,
                self.fpga,
                self.part_index,
                self.connection,
            )
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
                record[("drivers", "sinks")[item["role"]]].append(decoded)
            if exact_digest is not None and row["original_net"] in cut_source:
                if row["source_kind"] == "transport_shadow":
                    role = "transport_shadow_sink"
                    segment_ids = consumer_segments.get(row["original_net"], [])
                elif cut_source[row["original_net"]] == self.part_index:
                    role = "cut_source"
                    segment_ids = source_segments.get(row["original_net"], [])
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
    exact_binary_path: Path | None = None,
) -> dict[str, Any]:
    """Check compact construction invariants without replaying the source DB.

    The writer's INSERT...SELECT statements are the transformation.  Running
    the same multi-gigabyte source joins a second time would be redundant
    replay, not independent evidence.  This check therefore validates output
    constraints, assignment cardinality, and transport-boundary invariants.
    """

    from .split_overlay import bind_split_sources,validate_native_split
    if bind_split_sources(path)[0].schema=='split-netlists-v3':
        return validate_native_split(path,ir_path,assignment_index_path,
            expected_design=expected_design,expected_platform=expected_platform)

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
        if header.get("schema") != PREVIOUS_SPLIT_NETLIST_STORAGE_SCHEMA:
            raise ValidationError("split-netlist storage schema is invalid")
        if header.get("design", {}).get("name") != expected_design:
            raise ValidationError("split-netlist design disagrees")
        if header.get("platform") != expected_platform:
            raise ValidationError("split-netlist platform disagrees")
        descriptor = header.get("semantic_contract_binary")
        if isinstance(descriptor, Mapping):
            binary_path = exact_binary_path or _binary_artifact_path(
                path, descriptor
            )
            phase3_exact_binary_counts(binary_path, descriptor)
            if (
                descriptor.get("semantic_contract_sha256")
                != header.get("semantic_contract_sha256")
            ):
                raise ValidationError(
                    "split-netlist Static Exact identity disagrees"
                )

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
