"""Compact, queryable Phase 3 assignment data for downstream data planes.

The JSON assignment remains the human-readable Phase 3 contract used by the
router and scheduler.  This SQLite artifact is the machine handoff used by
Phase 6.  It is written while Phase 3 already owns the validated EmuIR and
assignment objects, so a downstream consumer never has to expand the packed
cluster JSON merely to recover ``instance -> FPGA``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Mapping

from .combinational_cut import semantic_contract_sha256
from .errors import ValidationError
from .ir import EmuIR
from .io import read_json
from .routing import static_exact_contract_from_assignment


ASSIGNMENT_INDEX_SCHEMA = "emuflow.phase3-assignment-sqlite/v1"
ASSIGNMENT_INDEX_APPLICATION_ID = 0x454D5541  # ASCII "EMUA"
ASSIGNMENT_INDEX_USER_VERSION = 1


DDL = """
CREATE TABLE metadata(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE parts(
  part_index INTEGER PRIMARY KEY,
  fpga TEXT NOT NULL UNIQUE
);
CREATE TABLE copies(
  source_instance_seq INTEGER NOT NULL,
  part_index INTEGER NOT NULL,
  copy_id TEXT NOT NULL,
  is_replica INTEGER NOT NULL CHECK(is_replica IN (0,1)),
  PRIMARY KEY(source_instance_seq, copy_id),
  UNIQUE(copy_id)
) WITHOUT ROWID;
CREATE INDEX copies_part_source
  ON copies(part_index, source_instance_seq);
CREATE TABLE cut_sinks(
  net_id TEXT NOT NULL,
  source_part_index INTEGER NOT NULL,
  sink_part_index INTEGER NOT NULL,
  PRIMARY KEY(net_id, sink_part_index)
) WITHOUT ROWID;
CREATE INDEX cut_sinks_sink
  ON cut_sinks(sink_part_index, net_id);
CREATE TABLE exact_cut_nodes(
  net_id TEXT PRIMARY KEY
) WITHOUT ROWID;
CREATE TABLE exact_cut_sources(
  net_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  segment_id TEXT NOT NULL,
  PRIMARY KEY(net_id, ordinal)
) WITHOUT ROWID;
CREATE TABLE exact_logic_segments(
  source_cut_net TEXT NOT NULL,
  part_index INTEGER NOT NULL,
  segment_id TEXT NOT NULL,
  PRIMARY KEY(source_cut_net, part_index, segment_id)
) WITHOUT ROWID;
CREATE INDEX exact_logic_segments_part
  ON exact_logic_segments(part_index, source_cut_net);
"""


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def write_phase3_assignment_index(
    path: Path,
    ir: EmuIR,
    assignment: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically publish the validated instance/partition relation.

    ``source_instance_seq`` is the stable row order of the paired EmuIR
    SQLite artifact.  Phase 6 can therefore join integer columns and never
    materialize millions of instance identifier strings in Python.
    """

    instance_assignment = assignment.get("instance_assignment")
    partitions = assignment.get("partitions")
    if not isinstance(instance_assignment, Mapping) or not isinstance(
        partitions, list
    ):
        raise ValidationError("Phase 3 assignment cannot be indexed")
    part_ids = sorted(
        partition.get("fpga")
        for partition in partitions
        if isinstance(partition, Mapping)
        and isinstance(partition.get("fpga"), str)
    )
    if len(part_ids) != len(partitions) or len(set(part_ids)) != len(part_ids):
        raise ValidationError("Phase 3 assignment has invalid partitions")
    part_index = {fpga: index for index, fpga in enumerate(part_ids)}
    if any(target not in part_index for target in instance_assignment.values()):
        raise ValidationError("Phase 3 assignment references an unknown FPGA")

    exact_contract = static_exact_contract_from_assignment(assignment)
    exact_digest = (
        semantic_contract_sha256(exact_contract)
        if exact_contract is not None
        else None
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    connection = sqlite3.connect(temporary)
    try:
        connection.executescript(
            f"""
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            PRAGMA cache_size=-65536;
            PRAGMA application_id={ASSIGNMENT_INDEX_APPLICATION_ID};
            PRAGMA user_version={ASSIGNMENT_INDEX_USER_VERSION};
            {DDL}
            """
        )
        metadata = {
            "schema": ASSIGNMENT_INDEX_SCHEMA,
            "design": assignment.get("design"),
            "platform": assignment.get("platform"),
            "provider": assignment.get("provider"),
            "seed": assignment.get("seed"),
            "source_instance_count": len(ir.value["instances"]),
            "semantic_contract_sha256": exact_digest,
        }
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES (?,?)",
            ((key, _json(value)) for key, value in sorted(metadata.items())),
        )
        connection.executemany(
            "INSERT INTO parts(part_index,fpga) VALUES (?,?)",
            enumerate(part_ids),
        )

        source_instances = ir.value["instances"]
        if len(source_instances) != len(instance_assignment):
            raise ValidationError(
                "Phase 3 assignment instance coverage disagrees with EmuIR"
            )

        def primary_rows():
            for source_seq, instance in enumerate(source_instances):
                identity = instance["id"]
                target = instance_assignment.get(identity)
                if target is None:
                    raise ValidationError(
                        f"Phase 3 assignment omits instance {identity!r}"
                    )
                yield (source_seq, part_index[target], identity, 0)

        connection.executemany(
            "INSERT INTO copies VALUES (?,?,?,?)", primary_rows()
        )

        replication = assignment.get("replication")
        replica_records = (
            replication.get("replicas", [])
            if isinstance(replication, Mapping)
            else []
        )

        def replica_rows():
            for record in replica_records:
                target = record.get("target_fpga")
                if target not in part_index:
                    raise ValidationError("replica targets an unknown FPGA")
                for item in record.get("instances", []):
                    original = item.get("original_instance")
                    copy_id = item.get("replica_instance")
                    if not isinstance(original, str) or not isinstance(
                        copy_id, str
                    ):
                        raise ValidationError("replica instance is invalid")
                    row = connection.execute(
                        "SELECT source_instance_seq FROM copies "
                        "WHERE copy_id=? AND is_replica=0",
                        (original,),
                    ).fetchone()
                    if row is None:
                        raise ValidationError("replica instance is invalid")
                    yield (
                        int(row[0]),
                        part_index[target],
                        copy_id,
                        1,
                    )

        connection.executemany(
            "INSERT INTO copies VALUES (?,?,?,?)", replica_rows()
        )

        def cut_rows():
            for cut in assignment.get("cut_nets", []):
                sources = cut.get("source_fpgas")
                sinks = cut.get("sink_fpgas")
                net_id = cut.get("net")
                if (
                    not isinstance(sources, list)
                    or len(sources) != 1
                    or sources[0] not in part_index
                    or not isinstance(net_id, str)
                    or not isinstance(sinks, list)
                ):
                    raise ValidationError("cut-net assignment is invalid")
                source = sources[0]
                for sink in sinks:
                    if sink not in part_index:
                        raise ValidationError("cut net targets an unknown FPGA")
                    yield (net_id, part_index[source], part_index[sink])

        connection.executemany(
            "INSERT INTO cut_sinks VALUES (?,?,?)", cut_rows()
        )

        if exact_contract is not None:
            connection.executemany(
                "INSERT INTO exact_cut_nodes VALUES (?)",
                ((node["net"],) for node in exact_contract["cut_nodes"]),
            )
            connection.executemany(
                "INSERT INTO exact_cut_sources VALUES (?,?,?)",
                (
                    (node["net"], ordinal, segment_id)
                    for node in exact_contract["cut_nodes"]
                    for ordinal, segment_id in enumerate(
                        node["source_segment_ids"]
                    )
                ),
            )

            def exact_rows():
                for segment in exact_contract["logic_segments"]:
                    source_cut = segment.get("source_cut_net")
                    fpga = segment.get("fpga")
                    if isinstance(source_cut, str) and fpga in part_index:
                        yield (source_cut, part_index[fpga], segment["id"])

            connection.executemany(
                "INSERT INTO exact_logic_segments VALUES (?,?,?)",
                exact_rows(),
            )

        connection.commit()
        summary = validate_phase3_assignment_index(
            temporary,
            expected_design=str(assignment.get("design")),
            expected_platform=str(assignment.get("platform")),
        )
        connection.close()
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return summary
    except BaseException:
        try:
            connection.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise


def migrate_phase3_assignment_index(
    path: Path,
    ir_path: Path,
    assignment_path: Path,
) -> dict[str, Any]:
    """Create the new sidecar for an immutable pre-sidecar Phase 3 result.

    This is an explicit migration/debug operation, not a production Phase 6
    fallback.  It may expand the old packed assignment, but it never expands
    the much larger EmuIR circuit graph.  New Phase 3 runs write the sidecar
    directly and never call this function.
    """

    assignment = read_json(assignment_path)
    instance_assignment = assignment.get("instance_assignment")
    partitions = assignment.get("partitions")
    if not isinstance(instance_assignment, Mapping) or not isinstance(
        partitions, list
    ):
        raise ValidationError("legacy Phase 3 assignment cannot be migrated")
    part_ids = sorted(
        partition.get("fpga")
        for partition in partitions
        if isinstance(partition, Mapping)
        and isinstance(partition.get("fpga"), str)
    )
    if len(part_ids) != len(partitions) or len(set(part_ids)) != len(part_ids):
        raise ValidationError("legacy Phase 3 assignment has invalid partitions")
    part_index = {fpga: index for index, fpga in enumerate(part_ids)}

    exact_contract = static_exact_contract_from_assignment(assignment)
    exact_digest = (
        semantic_contract_sha256(exact_contract)
        if exact_contract is not None
        else None
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    # URI mode is required because the immutable compact EmuIR is attached
    # below through a ``file:...?mode=ro&immutable=1`` URI.  Without it,
    # SQLite may interpret the full URI as a literal filename on production
    # filesystems even though ordinary temporary-path tests still pass.
    connection = sqlite3.connect(temporary, uri=True)
    try:
        connection.executescript(
            f"""
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            PRAGMA cache_size=-65536;
            PRAGMA application_id={ASSIGNMENT_INDEX_APPLICATION_ID};
            PRAGMA user_version={ASSIGNMENT_INDEX_USER_VERSION};
            {DDL}
            CREATE TEMP TABLE requested(
              id TEXT PRIMARY KEY,
              part_index INTEGER NOT NULL,
              copy_id TEXT NOT NULL,
              is_replica INTEGER NOT NULL
            ) WITHOUT ROWID;
            """
        )
        connection.execute(
            "ATTACH DATABASE ? AS src",
            (f"file:{ir_path.resolve()}?mode=ro&immutable=1",),
        )
        if connection.execute("PRAGMA src.user_version").fetchone()[0] != 2:
            raise ValidationError(
                "assignment migration requires compact EmuIR SQLite v2"
            )
        source_count = connection.execute(
            "SELECT count(*) FROM src.cell_records"
        ).fetchone()[0]
        metadata = {
            "schema": ASSIGNMENT_INDEX_SCHEMA,
            "design": assignment.get("design"),
            "platform": assignment.get("platform"),
            "provider": assignment.get("provider"),
            "seed": assignment.get("seed"),
            "source_instance_count": source_count,
            "semantic_contract_sha256": exact_digest,
            "migration": "legacy-assignment-without-emuir-expansion-v1",
        }
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES (?,?)",
            ((key, _json(value)) for key, value in sorted(metadata.items())),
        )
        connection.executemany(
            "INSERT INTO parts(part_index,fpga) VALUES (?,?)",
            enumerate(part_ids),
        )
        connection.executemany(
            "INSERT INTO requested VALUES (?,?,?,0)",
            (
                (identity, part_index[target], identity)
                for identity, target in instance_assignment.items()
            ),
        )
        connection.execute(
            """
            INSERT INTO copies
            SELECT c.seq,r.part_index,r.copy_id,r.is_replica
            FROM requested r JOIN src.cell_records c ON c.id=r.id
            """
        )
        observed = connection.execute(
            "SELECT count(*) FROM copies WHERE is_replica=0"
        ).fetchone()[0]
        if observed != source_count or observed != len(instance_assignment):
            raise ValidationError(
                "legacy assignment coverage disagrees with compact EmuIR"
            )
        connection.execute("DELETE FROM requested")
        replication = assignment.get("replication")
        if isinstance(replication, Mapping):
            replica_rows = []
            for record in replication.get("replicas", []):
                target = record.get("target_fpga")
                if target not in part_index:
                    raise ValidationError("replica targets an unknown FPGA")
                for item in record.get("instances", []):
                    replica_rows.append((
                        item.get("original_instance"),
                        part_index[target],
                        item.get("replica_instance"),
                    ))
            connection.executemany(
                "INSERT INTO requested VALUES (?,?,?,1)", replica_rows
            )
            connection.execute(
                """
                INSERT INTO copies
                SELECT c.seq,r.part_index,r.copy_id,r.is_replica
                FROM requested r JOIN src.cell_records c ON c.id=r.id
                """
            )
            replica_count = connection.execute(
                "SELECT count(*) FROM copies WHERE is_replica=1"
            ).fetchone()[0]
            if replica_count != len(replica_rows):
                raise ValidationError("legacy replica coverage disagrees")

        cut_rows = []
        for cut in assignment.get("cut_nets", []):
            sources = cut.get("source_fpgas")
            sinks = cut.get("sink_fpgas")
            if (
                not isinstance(sources, list)
                or len(sources) != 1
                or sources[0] not in part_index
                or not isinstance(sinks, list)
            ):
                raise ValidationError("legacy cut-net assignment is invalid")
            cut_rows.extend(
                (cut["net"], part_index[sources[0]], part_index[sink])
                for sink in sinks
            )
        connection.executemany(
            "INSERT INTO cut_sinks VALUES (?,?,?)", cut_rows
        )
        if exact_contract is not None:
            connection.executemany(
                "INSERT INTO exact_cut_nodes VALUES (?)",
                ((node["net"],) for node in exact_contract["cut_nodes"]),
            )
            connection.executemany(
                "INSERT INTO exact_cut_sources VALUES (?,?,?)",
                (
                    (node["net"], ordinal, segment_id)
                    for node in exact_contract["cut_nodes"]
                    for ordinal, segment_id in enumerate(
                        node["source_segment_ids"]
                    )
                ),
            )
            connection.executemany(
                "INSERT INTO exact_logic_segments VALUES (?,?,?)",
                (
                    (
                        segment["source_cut_net"],
                        part_index[segment["fpga"]],
                        segment["id"],
                    )
                    for segment in exact_contract["logic_segments"]
                    if isinstance(segment.get("source_cut_net"), str)
                    and segment.get("fpga") in part_index
                ),
            )
        connection.commit()
        summary = validate_phase3_assignment_index(
            temporary,
            expected_design=str(assignment.get("design")),
            expected_platform=str(assignment.get("platform")),
        )
        connection.close()
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return summary
    except BaseException:
        try:
            connection.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise


def open_phase3_assignment_index(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if (
        application_id != ASSIGNMENT_INDEX_APPLICATION_ID
        or version != ASSIGNMENT_INDEX_USER_VERSION
    ):
        connection.close()
        raise ValidationError("unsupported Phase 3 assignment index")
    return connection


def validate_phase3_assignment_index(
    path: Path,
    *,
    expected_design: str | None = None,
    expected_platform: str | None = None,
) -> dict[str, Any]:
    connection = open_phase3_assignment_index(path)
    try:
        metadata = {
            row["key"]: json.loads(row["value"])
            for row in connection.execute("SELECT key,value FROM metadata")
        }
        if metadata.get("schema") != ASSIGNMENT_INDEX_SCHEMA:
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
        return {
            "status": "pass",
            "schema": ASSIGNMENT_INDEX_SCHEMA,
            "design": metadata["design"],
            "platform": metadata["platform"],
            "parts": connection.execute(
                "SELECT count(*) FROM parts"
            ).fetchone()[0],
            "source_instances": primary,
            "replicas": replicas,
            "cut_sinks": connection.execute(
                "SELECT count(*) FROM cut_sinks"
            ).fetchone()[0],
            "semantic_contract_sha256": metadata.get(
                "semantic_contract_sha256"
            ),
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
            (f"file:{ir_path.resolve()}?mode=ro&immutable=1",),
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
            metadata.get("schema") != ASSIGNMENT_INDEX_SCHEMA
            or metadata.get("design") != expected_design
            or source_design != expected_design
        ):
            raise ValidationError("indexed Phase 3 design identity disagrees")
        if metadata.get("platform") != expected_platform:
            raise ValidationError("indexed Phase 3 platform identity disagrees")

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
        for row in connection.execute(
            """
            SELECT p.fpga,r.key AS resource,SUM(r.value) AS amount
              FROM copies c JOIN parts p ON p.part_index=c.part_index
              JOIN src.cell_records i ON i.seq=c.source_instance_seq
              JOIN src.payloads payload ON payload.seq=i.resources_seq,
                   json_each(payload.value) r
             WHERE c.is_replica=0
             GROUP BY p.fpga,r.key ORDER BY p.fpga,r.key
            """
        ):
            amount = int(row["amount"])
            if amount:
                resources_by_fpga.setdefault(row["fpga"], {})[
                    row["resource"]
                ] = amount
        for row in connection.execute(
            "SELECT fpga FROM parts ORDER BY part_index"
        ):
            resources_by_fpga.setdefault(row["fpga"], {})

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
            "semantic_contract_sha256": metadata.get(
                "semantic_contract_sha256"
            ),
        }
    finally:
        connection.close()
