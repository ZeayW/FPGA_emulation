"""Compact, queryable Phase 3 assignment data for downstream data planes.

The SQLite artifact owns the production instance, partition, cut and Static
Exact relations.  The sibling JSON is a small selector and compatibility
envelope.  Phase 4--7 consumers can therefore scan typed records without
expanding packed clusters or republishing the same graph as JSON.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator, Mapping

from .sqlite_artifacts import immutable_sqlite_uri
from .errors import ValidationError
from .phase3_exact_binary import (
    iter_phase3_exact_node_sources,
    iter_phase3_exact_segments,
    phase3_exact_binary_counts,
    validate_phase3_exact_binary,
)
from .phase3_runtime import phase3_runtime_call


PACKED_EXACT_ASSIGNMENT_INDEX_SCHEMA = "emuflow.phase3-assignment-sqlite/v3"
ASSIGNMENT_INDEX_SCHEMA = "emuflow.phase3-assignment-sqlite/v4"
ASSIGNMENT_INDEX_APPLICATION_ID = 0x454D5541  # ASCII "EMUA"
ASSIGNMENT_INDEX_USER_VERSION = 4
SUPPORTED_ASSIGNMENT_INDEX_USER_VERSIONS = {3, 4}
CUT_RECORDS_SCHEMA = "emuflow.phase3-cut-records/v1"
_ASSIGNMENT_INDEX_SCHEMA_BY_VERSION = {
    3: PACKED_EXACT_ASSIGNMENT_INDEX_SCHEMA,
    4: ASSIGNMENT_INDEX_SCHEMA,
}


CORE_DDL = (Path(__file__).parent / "data/schema/phase3_assignment_core.sql").read_text(encoding="utf-8")

CUT_RECORD_DDL = (Path(__file__).parent / "data/schema/phase3_assignment_cuts.sql").read_text(encoding="utf-8")

CURRENT_DDL = CORE_DDL + CUT_RECORD_DDL


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _normalized_cut_record(
    value: Any,
    part_index: Mapping[str, int],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError("cut-net assignment is invalid")
    allowed = {
        "net",
        "cut_class",
        "source_fpgas",
        "sink_fpgas",
        "sink_endpoints",
        "transport_round",
        "dependency_level",
        "combinational_dependency_depth",
        "predecessor_cut_nets",
    }
    if set(value) - allowed:
        raise ValidationError("cut-net assignment has unsupported fields")
    net_id = value.get("net")
    cut_class = value.get("cut_class")
    sources = value.get("source_fpgas")
    sinks = value.get("sink_fpgas")
    sink_endpoints = value.get("sink_endpoints")
    if (
        not isinstance(net_id, str)
        or not net_id
        or not isinstance(cut_class, str)
        or not cut_class
        or not isinstance(sources, list)
        or len(sources) != 1
        or sources[0] not in part_index
        or not isinstance(sinks, list)
        or not sinks
        or sinks != sorted(set(sinks))
        or any(sink not in part_index or sink == sources[0] for sink in sinks)
        or isinstance(sink_endpoints, bool)
        or not isinstance(sink_endpoints, int)
        or sink_endpoints <= 0
    ):
        raise ValidationError("cut-net assignment is invalid")
    transport_round = value.get("transport_round")
    if transport_round is not None and (
        isinstance(transport_round, bool)
        or not isinstance(transport_round, int)
        or transport_round < 0
    ):
        raise ValidationError("cut-net transport round is invalid")
    dependency_fields = (
        "dependency_level",
        "combinational_dependency_depth",
        "predecessor_cut_nets",
    )
    dependency_presence = [field in value for field in dependency_fields]
    if any(dependency_presence) != all(dependency_presence):
        raise ValidationError("cut-net dependency fields are incomplete")
    result = {
        "net": net_id,
        "cut_class": cut_class,
        "source_fpgas": list(sources),
        "sink_fpgas": list(sinks),
        "sink_endpoints": sink_endpoints,
    }
    if transport_round is not None:
        result["transport_round"] = transport_round
    if all(dependency_presence):
        level = value["dependency_level"]
        depth = value["combinational_dependency_depth"]
        predecessors = value["predecessor_cut_nets"]
        if (
            isinstance(level, bool)
            or not isinstance(level, int)
            or level < 0
            or isinstance(depth, bool)
            or not isinstance(depth, int)
            or depth < 0
            or not isinstance(predecessors, list)
            or predecessors != sorted(set(predecessors))
            or not all(
                isinstance(predecessor, str) and predecessor
                for predecessor in predecessors
            )
        ):
            raise ValidationError("cut-net dependency fields are invalid")
        result.update(
            {
                "dependency_level": level,
                "combinational_dependency_depth": depth,
                "predecessor_cut_nets": list(predecessors),
            }
        )
    return result


def _iter_cut_records(
    connection: sqlite3.Connection,
) -> Iterator[dict[str, Any]]:
    """Merge the normalized v4 cut columns using bounded ordered cursors."""

    parts = {
        int(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT part_index,fpga FROM parts ORDER BY part_index"
        )
    }
    sink_rows = iter(
        connection.execute(
            "SELECT c.cut_index,s.source_part_index,s.sink_part_index "
            "FROM cut_sinks s LEFT JOIN cuts c ON c.net_id=s.net_id "
            "ORDER BY c.cut_index IS NULL,c.cut_index,s.sink_part_index"
        )
    )
    predecessor_rows = iter(
        connection.execute(
            "SELECT cut_index,ordinal,predecessor_net_id "
            "FROM cut_predecessors ORDER BY cut_index,ordinal"
        )
    )
    sink_row = next(sink_rows, None)
    predecessor_row = next(predecessor_rows, None)
    expected_index = 0
    previous_net = None
    for row in connection.execute(
        "SELECT cut_index,net_id,source_part_index,cut_class,"
        "sink_endpoints,transport_round,dependency_level,"
        "combinational_dependency_depth FROM cuts ORDER BY cut_index"
    ):
        cut_index = int(row[0])
        net_id = str(row[1])
        source_index = int(row[2])
        if (
            cut_index != expected_index
            or source_index not in parts
            or (previous_net is not None and net_id <= previous_net)
        ):
            raise ValidationError("assignment index cut order is invalid")
        expected_index += 1
        previous_net = net_id
        sinks = []
        while sink_row is not None and int(sink_row[0]) == cut_index:
            if int(sink_row[1]) != source_index:
                raise ValidationError(
                    "assignment index cut source is inconsistent"
                )
            sink_index = int(sink_row[2])
            if sink_index not in parts:
                raise ValidationError("assignment index cut sink is invalid")
            sinks.append(parts[sink_index])
            sink_row = next(sink_rows, None)
        if not sinks or sinks != sorted(set(sinks)):
            raise ValidationError("assignment index cut sinks are invalid")
        predecessors = []
        ordinal = 0
        while (
            predecessor_row is not None
            and int(predecessor_row[0]) == cut_index
        ):
            if int(predecessor_row[1]) != ordinal:
                raise ValidationError(
                    "assignment index cut predecessor order is invalid"
                )
            predecessors.append(str(predecessor_row[2]))
            ordinal += 1
            predecessor_row = next(predecessor_rows, None)
        if predecessors != sorted(set(predecessors)):
            raise ValidationError(
                "assignment index cut predecessors are invalid"
            )
        record: dict[str, Any] = {
            "net": net_id,
            "cut_class": str(row[3]),
            "source_fpgas": [parts[source_index]],
            "sink_fpgas": sinks,
            "sink_endpoints": int(row[4]),
        }
        if row[5] is not None:
            record["transport_round"] = int(row[5])
        dependency_level = row[6]
        dependency_depth = row[7]
        if dependency_level is not None or dependency_depth is not None:
            if dependency_level is None or dependency_depth is None:
                raise ValidationError(
                    "assignment index cut dependency is incomplete"
                )
            record.update(
                {
                    "dependency_level": int(dependency_level),
                    "combinational_dependency_depth": int(dependency_depth),
                    "predecessor_cut_nets": predecessors,
                }
            )
        elif predecessors:
            raise ValidationError(
                "assignment index cut dependency is incomplete"
            )
        yield record
    if sink_row is not None or predecessor_row is not None:
        raise ValidationError("assignment index contains orphan cut records")


def iter_phase3_cut_records(
    path: Path,
    descriptor: Mapping[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield sealed v4 cut records without materializing the relation."""

    connection = open_phase3_assignment_index(path)
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        metadata = {
            str(row[0]): json.loads(row[1])
            for row in connection.execute(
                "SELECT key,value FROM metadata WHERE key IN "
                "('schema','cut_records_schema','cut_records',"
                "'cut_records_sha256')"
            )
        }
        expected = {
            "artifact": path.name,
            "schema": metadata.get("cut_records_schema"),
            "records": metadata.get("cut_records"),
            "sha256": metadata.get("cut_records_sha256"),
        }
        if (
            version != ASSIGNMENT_INDEX_USER_VERSION
            or metadata.get("schema") != ASSIGNMENT_INDEX_SCHEMA
            or expected["schema"] != CUT_RECORDS_SCHEMA
            or isinstance(expected["records"], bool)
            or not isinstance(expected["records"], int)
            or expected["records"] < 0
            or not isinstance(expected["sha256"], str)
            or len(expected["sha256"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in expected["sha256"]
            )
            or (descriptor is not None and dict(descriptor) != expected)
        ):
            raise ValidationError("Phase 3 cut-record descriptor is invalid")
        digest = hashlib.sha256()
        observed = 0
        for record in _iter_cut_records(connection):
            digest.update(_json(record).encode("utf-8"))
            digest.update(b"\n")
            observed += 1
            yield record
        if observed != expected["records"] or digest.hexdigest() != expected[
            "sha256"
        ]:
            raise ValidationError("Phase 3 cut-record identity disagrees")
    finally:
        connection.close()




def open_phase3_assignment_index(
    path: Path, *, check_same_thread: bool = True
) -> sqlite3.Connection:
    connection = sqlite3.connect(
        immutable_sqlite_uri(path), uri=True, check_same_thread=check_same_thread
    )
    connection.row_factory = sqlite3.Row
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if (
        application_id != ASSIGNMENT_INDEX_APPLICATION_ID
        or version not in SUPPORTED_ASSIGNMENT_INDEX_USER_VERSIONS
    ):
        connection.close()
        raise ValidationError("unsupported Phase 3 assignment index")
    try:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='schema'"
        ).fetchone()
        schema = json.loads(row[0]) if row is not None else None
    except (sqlite3.Error, TypeError, json.JSONDecodeError) as error:
        connection.close()
        raise ValidationError("Phase 3 assignment index schema is invalid") from error
    if schema != _ASSIGNMENT_INDEX_SCHEMA_BY_VERSION[version]:
        connection.close()
        raise ValidationError("Phase 3 assignment index schema is invalid")
    return connection


def _exact_binary_reference(
    connection: sqlite3.Connection, path: Path
) -> tuple[Path, Mapping[str, Any]]:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key='semantic_contract_binary'"
    ).fetchone()
    descriptor = json.loads(row[0]) if row is not None else None
    if not isinstance(descriptor, Mapping):
        raise ValidationError(
            "assignment index lacks its Static Exact binary descriptor"
        )
    artifact = descriptor.get("artifact")
    if not isinstance(artifact, str) or Path(artifact).name != artifact:
        raise ValidationError("Static Exact binary artifact name is invalid")
    return path.with_name(artifact), descriptor






def phase3_exact_semantic_counts(path: Path) -> dict[str, int]:
    """Return bounded Static Exact population counts from the index."""

    connection = open_phase3_assignment_index(path)
    try:
        binary_path, descriptor = _exact_binary_reference(connection, path)
        counts = phase3_exact_binary_counts(binary_path, descriptor)
        return {
            "logic_segments": counts["logic_segments"],
            "capture_requirements": counts["capture_requirements"],
        }
    finally:
        connection.close()


def read_phase3_exact_split_relations(
    path: Path,
) -> tuple[
    set[str], dict[str, list[str]], dict[tuple[str, int], list[str]]
]:
    """Read the bounded exact relations needed to construct Phase 6 metadata."""

    connection = open_phase3_assignment_index(path)
    try:
        parts = {
            str(row[1]): int(row[0])
            for row in connection.execute(
                "SELECT part_index,fpga FROM parts ORDER BY part_index"
            )
        }
        binary_path, descriptor = _exact_binary_reference(connection, path)
        source_segments = {
            net: [_identity_segment(number) for number in numbers]
            for net, numbers in iter_phase3_exact_node_sources(binary_path, descriptor)
        }
        consumer_segments: dict[tuple[str, int], list[str]] = defaultdict(list)
        for segment in iter_phase3_exact_segments(binary_path, descriptor):
            source = segment.get("source_cut_net")
            if isinstance(source, str):
                consumer_segments[(source, parts[segment["fpga"]])].append(
                    segment["id"]
                )
        return set(source_segments), source_segments, dict(consumer_segments)
    finally:
        connection.close()


def _identity_segment(number: int) -> str:
    if isinstance(number, bool) or not isinstance(number, int) or number < 0:
        raise ValidationError("Static Exact binary segment number is invalid")
    return f"segment{number:06d}"


def phase3_exact_relation_counts(path: Path) -> dict[str, int]:
    """Return storage-level relation counts for qualification evidence."""

    connection = open_phase3_assignment_index(path)
    try:
        cut_sinks = int(
            connection.execute("SELECT count(*) FROM cut_sinks").fetchone()[0]
        )
        metadata = {
            str(row[0]): json.loads(row[1])
            for row in connection.execute(
                "SELECT key,value FROM metadata WHERE key IN "
                "('semantic_contract_binary','semantic_contract_sha256')"
            )
        }
        if metadata.get("semantic_contract_binary") is not None or metadata.get(
            "semantic_contract_sha256"
        ) is not None:
            binary_path, descriptor = _exact_binary_reference(connection, path)
            counts = phase3_exact_binary_counts(binary_path, descriptor)
        else:
            counts = {"cut_nodes": 0, "source_segments": 0, "consumer_segments": 0}
        return {
            "cut_sinks": cut_sinks,
            "exact_cut_nodes": counts["cut_nodes"],
            "exact_cut_source_segments": counts["source_segments"],
            "exact_consumer_segments": counts["consumer_segments"],
        }
    finally:
        connection.close()


def validate_phase3_assignment_index(
    path: Path,
    *,
    expected_design: str | None = None,
    expected_platform: str | None = None,
    validate_binary: bool = True,
    exact_binary_path: Path | None = None,
) -> dict[str, Any]:
    connection = open_phase3_assignment_index(path)
    try:
        metadata = {
            row["key"]: json.loads(row["value"])
            for row in connection.execute("SELECT key,value FROM metadata")
        }
        if metadata.get("schema") not in set(
            _ASSIGNMENT_INDEX_SCHEMA_BY_VERSION.values()
        ):
            raise ValidationError("Phase 3 assignment index schema is invalid")
        if expected_design is not None and metadata.get("design") != expected_design:
            raise ValidationError("assignment index design disagrees")
        if (
            expected_platform is not None
            and metadata.get("platform") != expected_platform
        ):
            raise ValidationError("assignment index platform disagrees")
        primary = connection.execute(
            "SELECT count(*) FROM copies WHERE is_replica=0"
        ).fetchone()[0]
        replicas = connection.execute(
            "SELECT count(*) FROM copies WHERE is_replica=1"
        ).fetchone()[0]
        if primary != metadata.get("source_instance_count"):
            raise ValidationError("assignment index source coverage disagrees")
        if connection.execute(
            "SELECT count(*) FROM parts"
        ).fetchone()[0] <= 0:
            raise ValidationError("assignment index has no FPGA partitions")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        cut_records = 0
        cut_records_sha256 = None
        if version == ASSIGNMENT_INDEX_USER_VERSION:
            if not {"cuts", "cut_predecessors"}.issubset(tables):
                raise ValidationError(
                    "Phase 3 assignment index lacks typed cut records"
                )
            from .phase3_assignment_native import validate_native_assignment_cuts

            cuts = phase3_runtime_call(
                "assignment_index.validate_cuts", validate_native_assignment_cuts, path
            )
            cut_records, cut_records_sha256 = cuts["records"], cuts["sha256"]
            if cut_records != metadata.get(
                "cut_records"
            ) or cut_records_sha256 != metadata.get("cut_records_sha256"):
                raise ValidationError("Phase 3 cut-record identity disagrees")
        has_exact_payloads = False
        exact_segment_bindings = 0
        exact_capture_requirements = 0
        binary_validation = None
        descriptor = metadata.get("semantic_contract_binary")
        if isinstance(descriptor, Mapping):
            artifact = descriptor.get("artifact")
            if not isinstance(artifact, str) or Path(artifact).name != artifact:
                raise ValidationError("Static Exact binary artifact name is invalid")
            if validate_binary:
                binary_validation = validate_phase3_exact_binary(
                    exact_binary_path or path.with_name(artifact), descriptor
                )
            else:
                binary_validation = {
                    "status": "descriptor-pass",
                    **phase3_exact_binary_counts(
                        exact_binary_path or path.with_name(artifact), descriptor
                    ),
                }
            exact_segment_bindings = binary_validation["logic_segments"]
            exact_capture_requirements = binary_validation["capture_requirements"]
            has_exact_payloads = True
        elif metadata.get("semantic_contract_sha256") is not None:
            raise ValidationError(
                "assignment index lacks its Static Exact binary descriptor"
            )
        return {
            "status": "pass",
            "schema": metadata["schema"],
            "design": metadata["design"],
            "platform": metadata["platform"],
            "parts": connection.execute("SELECT count(*) FROM parts").fetchone()[0],
            "source_instances": primary,
            "replicas": replicas,
            "cut_sinks": (
                cuts["sinks"]
                if version == ASSIGNMENT_INDEX_USER_VERSION
                else connection.execute("SELECT count(*) FROM cut_sinks").fetchone()[0]
            ),
            "cut_records_schema": metadata.get("cut_records_schema"),
            "cut_records": cut_records,
            "cut_records_sha256": cut_records_sha256,
            "semantic_contract_sha256": metadata.get("semantic_contract_sha256"),
            "exact_segment_bindings": exact_segment_bindings,
            "exact_capture_requirements": exact_capture_requirements,
            "indexed_semantic_payloads": has_exact_payloads,
            "semantic_binary": binary_validation,
        }
    finally:
        connection.close()


def validate_phase3_assignment_index_against_emuir(
    path: Path,
    ir_path: Path,
    *,
    expected_design: str,
    expected_platform: str,
) -> dict[str, Any]:
    """Prove the Phase 3 data-plane relation against compact EmuIR.

    This is the production end-to-end validation boundary.  It checks the
    complete instance relation, per-FPGA resources, and every cross-partition
    source/sink pair with set-oriented SQL.  It deliberately does not rebuild
    the multi-million-object EmuIR and partition graph in Python merely to
    replay validation already performed while Phase 3 owned those objects.
    """

    connection = open_phase3_assignment_index(path)
    try:
        connection.execute(
            "ATTACH DATABASE ? AS src",
            (immutable_sqlite_uri(ir_path),),
        )
        if connection.execute("PRAGMA src.user_version").fetchone()[0] != 2:
            raise ValidationError(
                "indexed Phase 3 validation requires compact EmuIR SQLite v2"
            )
        metadata = {
            row["key"]: json.loads(row["value"])
            for row in connection.execute("SELECT key,value FROM metadata")
        }
        source_metadata = dict(
            connection.execute("SELECT key,value FROM src.metadata")
        )
        try:
            source_design = json.loads(source_metadata["design_json"])["name"]
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValidationError("compact EmuIR design metadata is invalid") from error
        if (
            metadata.get("schema")
            not in {
                PACKED_EXACT_ASSIGNMENT_INDEX_SCHEMA,
                ASSIGNMENT_INDEX_SCHEMA,
            }
            or metadata.get("design") != expected_design
            or source_design != expected_design
        ):
            raise ValidationError("indexed Phase 3 design identity disagrees")
        if metadata.get("platform") != expected_platform:
            raise ValidationError("indexed Phase 3 platform identity disagrees")
        descriptor = metadata.get("semantic_contract_binary")
        if isinstance(descriptor, Mapping):
            artifact = descriptor.get("artifact")
            if not isinstance(artifact, str) or Path(artifact).name != artifact:
                raise ValidationError("Static Exact binary artifact name is invalid")
            validate_phase3_exact_binary(path.with_name(artifact), descriptor)

        source_instances = int(connection.execute(
            "SELECT count(*) FROM src.cell_records"
        ).fetchone()[0])
        primary_instances = int(connection.execute(
            "SELECT count(*) FROM copies WHERE is_replica=0"
        ).fetchone()[0])
        coverage_errors = int(connection.execute(
            """
            SELECT count(*) FROM (
              SELECT s.seq
                FROM src.cell_records s
                LEFT JOIN copies c
                  ON c.source_instance_seq=s.seq AND c.is_replica=0
               WHERE c.source_instance_seq IS NULL OR c.copy_id!=s.id
              UNION ALL
              SELECT c.source_instance_seq
                FROM copies c
                LEFT JOIN src.cell_records s ON s.seq=c.source_instance_seq
               WHERE c.is_replica=0 AND s.seq IS NULL
            )
            """
        ).fetchone()[0])
        if (
            source_instances != primary_instances
            or source_instances != metadata.get("source_instance_count")
            or coverage_errors
        ):
            raise ValidationError(
                "indexed Phase 3 instance coverage disagrees with EmuIR"
            )

        cut_pair_errors = int(connection.execute(
            """
            WITH primary_driver_parts AS (
              SELECT DISTINCT e.net_seq,c.part_index
                FROM src.endpoint_records e
                JOIN copies c
                  ON c.source_instance_seq=e.instance_seq AND c.is_replica=0
               WHERE e.role=0 AND e.instance_seq IS NOT NULL
            ), driver_locations AS (
              SELECT DISTINCT e.net_seq,c.part_index
                FROM src.endpoint_records e JOIN copies c
                  ON c.source_instance_seq=e.instance_seq
               WHERE e.role=0 AND e.instance_seq IS NOT NULL
            ), sink_parts AS (
              SELECT DISTINCT e.net_seq,c.part_index
                FROM src.endpoint_records e
                JOIN copies c
                  ON c.source_instance_seq=e.instance_seq
               WHERE e.role=1 AND e.instance_seq IS NOT NULL
            ), expected AS (
              SELECT n.id AS net_id,d.part_index AS source_part_index,
                     s.part_index AS sink_part_index
                FROM primary_driver_parts d
                JOIN sink_parts s ON s.net_seq=d.net_seq
                JOIN src.net_records n ON n.seq=d.net_seq
               WHERE NOT EXISTS (
                 SELECT 1 FROM driver_locations x
                  WHERE x.net_seq=s.net_seq AND x.part_index=s.part_index
               )
            )
            SELECT count(*) FROM (
              SELECT net_id,source_part_index,sink_part_index FROM expected
              EXCEPT
              SELECT net_id,source_part_index,sink_part_index FROM cut_sinks
            )
            """
        ).fetchone()[0]) + int(connection.execute(
            """
            WITH primary_driver_parts AS (
              SELECT DISTINCT e.net_seq,c.part_index
                FROM src.endpoint_records e
                JOIN copies c
                  ON c.source_instance_seq=e.instance_seq AND c.is_replica=0
               WHERE e.role=0 AND e.instance_seq IS NOT NULL
            ), driver_locations AS (
              SELECT DISTINCT e.net_seq,c.part_index
                FROM src.endpoint_records e JOIN copies c
                  ON c.source_instance_seq=e.instance_seq
               WHERE e.role=0 AND e.instance_seq IS NOT NULL
            ), sink_parts AS (
              SELECT DISTINCT e.net_seq,c.part_index
                FROM src.endpoint_records e
                JOIN copies c
                  ON c.source_instance_seq=e.instance_seq
               WHERE e.role=1 AND e.instance_seq IS NOT NULL
            ), expected AS (
              SELECT n.id AS net_id,d.part_index AS source_part_index,
                     s.part_index AS sink_part_index
                FROM primary_driver_parts d
                JOIN sink_parts s ON s.net_seq=d.net_seq
                JOIN src.net_records n ON n.seq=d.net_seq
               WHERE NOT EXISTS (
                 SELECT 1 FROM driver_locations x
                  WHERE x.net_seq=s.net_seq AND x.part_index=s.part_index
               )
            )
            SELECT count(*) FROM (
              SELECT net_id,source_part_index,sink_part_index FROM cut_sinks
              EXCEPT
              SELECT net_id,source_part_index,sink_part_index FROM expected
            )
            """
        ).fetchone()[0])
        if cut_pair_errors:
            raise ValidationError(
                "indexed Phase 3 cut relation disagrees with EmuIR"
            )

        resources_by_fpga: dict[str, dict[str, int]] = {}
        total_resources_by_fpga: dict[str, dict[str, int]] = {}
        for row in connection.execute(
            """
            SELECT p.fpga,c.is_replica,r.key AS resource,SUM(r.value) AS amount
              FROM copies c JOIN parts p ON p.part_index=c.part_index
              JOIN src.cell_records i ON i.seq=c.source_instance_seq
              JOIN src.payloads payload ON payload.seq=i.resources_seq,
                   json_each(payload.value) r
             GROUP BY p.fpga,c.is_replica,r.key ORDER BY p.fpga,c.is_replica,r.key
            """
        ):
            amount = int(row["amount"])
            if amount:
                total = total_resources_by_fpga.setdefault(row["fpga"], {})
                total[row["resource"]] = total.get(row["resource"], 0) + amount
                if not row["is_replica"]:
                    resources_by_fpga.setdefault(row["fpga"], {})[row["resource"]] = amount
        instances_by_fpga = {}
        for row in connection.execute(
            "SELECT p.fpga,SUM(c.is_replica=0) AS amount FROM parts p "
            "LEFT JOIN copies c ON c.part_index=p.part_index GROUP BY p.fpga"
        ):
            instances_by_fpga[row["fpga"]] = int(row["amount"] or 0)
            resources_by_fpga.setdefault(row["fpga"], {})
            total_resources_by_fpga.setdefault(row["fpga"], {})

        return {
            "status": "pass",
            "provider": "sqlite-phase3-relation-certificate-v1",
            "design": expected_design,
            "platform": expected_platform,
            "source_instances": source_instances,
            "primary_instances": primary_instances,
            "replicas": int(connection.execute(
                "SELECT count(*) FROM copies WHERE is_replica=1"
            ).fetchone()[0]),
            "parts": int(connection.execute(
                "SELECT count(*) FROM parts"
            ).fetchone()[0]),
            "cut_pairs": int(connection.execute(
                "SELECT count(*) FROM cut_sinks"
            ).fetchone()[0]),
            "coverage_errors": coverage_errors,
            "cut_pair_errors": cut_pair_errors,
            "resources_by_fpga": resources_by_fpga,
            "total_resources_by_fpga": total_resources_by_fpga,
            "instances_by_fpga": instances_by_fpga,
            "constraints": metadata.get("constraints"),
            "semantic_contract_sha256": metadata.get(
                "semantic_contract_sha256"
            ),
        }
    finally:
        connection.close()
