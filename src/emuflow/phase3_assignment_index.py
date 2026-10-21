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
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

from .combinational_cut import semantic_contract_sha256
from .errors import ValidationError
from .ir import EmuIR
from .io import read_json
from .phase3_exact_binary import (
    EXACT_BINARY_ARTIFACT,
    iter_phase3_exact_node_sources,
    iter_phase3_exact_segments,
    phase3_exact_binary_counts,
    read_phase3_exact_binary_contract,
    read_phase3_exact_binary_partition,
    validate_phase3_exact_binary,
    write_phase3_exact_binary,
)
from .phase3_runtime import phase3_runtime_call
from .phase3_exact_records import (
    STATIC_EXACT_CAPTURE_BIT_ERROR,
    STATIC_EXACT_CAPTURE_BIT_MAX_BYTES,
    validate_static_exact_capture_bit,
)
from .routing import static_exact_contract_from_assignment


LEGACY_ASSIGNMENT_INDEX_SCHEMA = "emuflow.phase3-assignment-sqlite/v1"
PREVIOUS_ASSIGNMENT_INDEX_SCHEMA = "emuflow.phase3-assignment-sqlite/v2"
ASSIGNMENT_INDEX_SCHEMA = "emuflow.phase3-assignment-sqlite/v3"
ASSIGNMENT_INDEX_APPLICATION_ID = 0x454D5541  # ASCII "EMUA"
ASSIGNMENT_INDEX_USER_VERSION = 3
SUPPORTED_ASSIGNMENT_INDEX_USER_VERSIONS = {1, 2, 3}
_SQLITE_SIGNED_INTEGER_MAX = (1 << 63) - 1


CORE_DDL = """
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
"""

EXACT_DDL = """
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
CREATE TABLE exact_segment_bindings(
  segment_id TEXT PRIMARY KEY,
  kind INTEGER NOT NULL CHECK(kind BETWEEN 0 AND 2),
  part_index INTEGER NOT NULL,
  source_cut_net TEXT,
  sink_cut_net TEXT,
  capture_requirement TEXT,
  source_semantics INTEGER CHECK(source_semantics IS NULL OR source_semantics=1)
) WITHOUT ROWID;
CREATE INDEX exact_segment_bindings_part
  ON exact_segment_bindings(part_index, segment_id);
CREATE TABLE exact_capture_requirements(
  capture_id TEXT PRIMARY KEY,
  cut_net TEXT NOT NULL,
  part_index INTEGER NOT NULL,
  kind INTEGER NOT NULL CHECK(kind IN (0,1)),
  endpoint TEXT NOT NULL,
  port TEXT,
  bit INTEGER
) WITHOUT ROWID;
CREATE INDEX exact_capture_requirements_part
  ON exact_capture_requirements(part_index, capture_id);
"""

DDL = CORE_DDL + EXACT_DDL


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _decode_legacy_capture_bit(value: Any) -> int:
    """Decode v2's INTEGER/BLOB capture-bit compatibility column."""

    if isinstance(value, (bytes, bytearray, memoryview)):
        encoded = bytes(value)
        if len(encoded) > STATIC_EXACT_CAPTURE_BIT_MAX_BYTES:
            raise ValidationError(STATIC_EXACT_CAPTURE_BIT_ERROR)
        if not encoded or (len(encoded) > 1 and encoded[0] == 0):
            raise ValidationError(
                "legacy Static Exact capture bit encoding is invalid"
            )
        value = int.from_bytes(encoded, "big")
        if value <= _SQLITE_SIGNED_INTEGER_MAX:
            raise ValidationError(
                "legacy Static Exact capture bit encoding is invalid"
            )
    return validate_static_exact_capture_bit(value)


def _encode_legacy_capture_bit(value: Any) -> Any:
    bit = validate_static_exact_capture_bit(value)
    if bit <= _SQLITE_SIGNED_INTEGER_MAX:
        return bit
    return sqlite3.Binary(
        bit.to_bytes(max(1, (bit.bit_length() + 7) // 8), "big")
    )


def _validate_legacy_capture_mapping(capture: Any) -> None:
    if not isinstance(capture, Mapping):
        raise ValidationError("legacy Static Exact capture is invalid")
    kind = capture.get("kind")
    if kind == "architectural-state":
        validate_static_exact_capture_bit(capture.get("bit"))
    elif kind == "top-output":
        if capture.get("bit") is not None:
            raise ValidationError("legacy Static Exact capture is invalid")
    else:
        raise ValidationError("legacy Static Exact capture is invalid")


def _validate_exact_contract_capture_bits(
    exact_contract: Mapping[str, Any],
) -> None:
    captures = exact_contract.get("capture_requirements")
    if not isinstance(captures, list):
        raise ValidationError("Static Exact capture requirements are invalid")
    for capture in captures:
        _validate_legacy_capture_mapping(capture)


def _validate_legacy_sqlite_capture_bits(
    connection: sqlite3.Connection, version: int
) -> None:
    if version == 1:
        for row in connection.execute(
            "SELECT payload_json FROM exact_capture_requirements"
        ):
            try:
                capture = json.loads(row[0])
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValidationError(
                    "legacy Static Exact capture JSON is invalid"
                ) from error
            _validate_legacy_capture_mapping(capture)
        return
    for row in connection.execute(
        "SELECT kind,bit FROM exact_capture_requirements"
    ):
        kind = int(row[0])
        if kind == 1:
            _decode_legacy_capture_bit(row[1])
        elif kind == 0:
            if row[1] is not None:
                raise ValidationError("legacy Static Exact capture is invalid")
        else:
            raise ValidationError("legacy Static Exact capture is invalid")


def _exact_binding_rows(
    exact_contract: Mapping[str, Any], part_index: Mapping[str, int]
):
    for segment in exact_contract["logic_segments"]:
        fpga = segment.get("fpga")
        segment_id = segment.get("id")
        kind = segment.get("kind")
        if (
            fpga not in part_index
            or not isinstance(segment_id, str)
            or kind not in {"launch_to_tx", "rx_to_tx", "rx_to_capture"}
        ):
            raise ValidationError("exact logic segment binding is invalid")
        source_cut_net = segment.get("source_cut_net")
        sink_cut_net = segment.get("sink_cut_net")
        capture_requirement = segment.get("capture_requirement")
        source_semantics = segment.get("source_semantics")
        if kind == "launch_to_tx":
            valid = (
                source_cut_net is None
                and isinstance(sink_cut_net, str)
                and capture_requirement is None
                and source_semantics
                in {None, "configuration-stable-constant"}
            )
        elif kind == "rx_to_tx":
            valid = (
                isinstance(source_cut_net, str)
                and isinstance(sink_cut_net, str)
                and capture_requirement is None
                and source_semantics is None
            )
        else:
            valid = (
                isinstance(source_cut_net, str)
                and sink_cut_net is None
                and isinstance(capture_requirement, str)
                and source_semantics is None
            )
        if not valid:
            raise ValidationError("exact logic segment payload is invalid")
        yield (
            segment_id,
            {"launch_to_tx": 0, "rx_to_tx": 1, "rx_to_capture": 2}[kind],
            part_index[fpga],
            source_cut_net,
            sink_cut_net,
            capture_requirement,
            1 if source_semantics is not None else None,
        )


def _exact_capture_rows(
    exact_contract: Mapping[str, Any], part_index: Mapping[str, int]
):
    for capture in exact_contract["capture_requirements"]:
        fpga = capture.get("fpga")
        capture_id = capture.get("id")
        if fpga not in part_index or not isinstance(capture_id, str):
            raise ValidationError("exact capture requirement is invalid")
        cut_net = capture.get("cut_net")
        kind = capture.get("kind")
        endpoint = capture.get("endpoint")
        port = capture.get("port")
        bit = capture.get("bit")
        if (
            not isinstance(cut_net, str)
            or kind not in {"top-output", "architectural-state"}
            or not isinstance(endpoint, str)
            or (
                kind == "architectural-state"
                and (
                    not isinstance(port, str)
                )
            )
            or (kind == "top-output" and (port is not None or bit is not None))
        ):
            raise ValidationError("exact capture requirement payload is invalid")
        stored_bit = (
            _encode_legacy_capture_bit(bit)
            if kind == "architectural-state"
            else None
        )
        yield (
            capture_id,
            cut_net,
            part_index[fpga],
            0 if kind == "top-output" else 1,
            endpoint,
            port,
            stored_bit,
        )


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
    # ``build_partition_assignment`` owns the in-process Static Exact
    # contract and computes this identity when it creates the final Phase 3
    # assignment.  Recomputing it here makes publication serialize the same
    # multi-million-record contract a second time immediately before the
    # binary writer scans it again.  The production publisher consumes the
    # already validated owner-provided identity; the explicit legacy migration
    # below still reconstructs and hashes detached historical input.
    exact_digest = (
        assignment.get("semantic_contract_sha256")
        if exact_contract is not None
        else None
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    exact_binary_path = path.with_name(EXACT_BINARY_ARTIFACT)
    exact_binary_temporary = path.with_name(
        f".{EXACT_BINARY_ARTIFACT}.{uuid.uuid4().hex}.tmp"
    )
    exact_binary = (
        phase3_runtime_call(
            "assignment_index.write_exact_binary",
            write_phase3_exact_binary,
            exact_binary_temporary,
            exact_contract,
            part_ids,
            semantic_digest=exact_digest,
        )
        if exact_contract is not None
        else None
    )
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
            {CORE_DDL}
            """
        )
        metadata = {
            "schema": ASSIGNMENT_INDEX_SCHEMA,
            "design": assignment.get("design"),
            "platform": assignment.get("platform"),
            "provider": assignment.get("provider"),
            "seed": assignment.get("seed"),
            "source_instance_count": len(ir.value["instances"]),
            "constraints": assignment.get("constraints"),
            "semantic_contract_sha256": exact_digest,
            "semantic_contract_schema": (
                exact_contract.get("schema")
                if exact_contract is not None
                else None
            ),
            "semantic_contract_header": (
                {
                    key: value
                    for key, value in exact_contract.items()
                    if key
                    not in {
                        "cut_nodes",
                        "dependency_edges",
                        "logic_segments",
                        "capture_requirements",
                    }
                }
                if exact_contract is not None
                else None
            ),
            "semantic_contract_binary": exact_binary,
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

        phase3_runtime_call(
            "assignment_index.write_primary_copies",
            connection.executemany,
            "INSERT INTO copies VALUES (?,?,?,?)",
            primary_rows(),
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

        phase3_runtime_call(
            "assignment_index.write_replica_copies",
            connection.executemany,
            "INSERT INTO copies VALUES (?,?,?,?)",
            replica_rows(),
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

        phase3_runtime_call(
            "assignment_index.write_cut_sinks",
            connection.executemany,
            "INSERT INTO cut_sinks VALUES (?,?,?)",
            cut_rows(),
        )

        phase3_runtime_call(
            "assignment_index.commit", connection.commit
        )
        summary = phase3_runtime_call(
            "assignment_index.validate",
            validate_phase3_assignment_index,
            temporary,
            expected_design=str(assignment.get("design")),
            expected_platform=str(assignment.get("platform")),
            validate_binary=False,
            exact_binary_path=exact_binary_temporary,
        )
        connection.close()
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        previous_binary = path.with_name(
            f".{EXACT_BINARY_ARTIFACT}.{uuid.uuid4().hex}.previous"
        )
        moved_previous = False
        try:
            if exact_contract is not None:
                if exact_binary_path.exists():
                    os.replace(exact_binary_path, previous_binary)
                    moved_previous = True
                os.replace(exact_binary_temporary, exact_binary_path)
            os.replace(temporary, path)
        except BaseException:
            if exact_contract is not None:
                exact_binary_path.unlink(missing_ok=True)
                if moved_previous:
                    os.replace(previous_binary, exact_binary_path)
            raise
        previous_binary.unlink(missing_ok=True)
        if exact_contract is None:
            exact_binary_path.unlink(missing_ok=True)
        return summary
    except BaseException:
        try:
            connection.close()
        finally:
            temporary.unlink(missing_ok=True)
            exact_binary_temporary.unlink(missing_ok=True)
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

    try:
        assignment = read_json(assignment_path)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValidationError(
            "legacy Phase 3 assignment JSON is invalid"
        ) from error
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
    if exact_contract is not None:
        # Detached legacy JSON is untrusted. Validate every capture before the
        # canonical digest formats any integer or SQLite binds an INTEGER.
        _validate_exact_contract_capture_bits(exact_contract)
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
            PRAGMA user_version=2;
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
            "schema": PREVIOUS_ASSIGNMENT_INDEX_SCHEMA,
            "design": assignment.get("design"),
            "platform": assignment.get("platform"),
            "provider": assignment.get("provider"),
            "seed": assignment.get("seed"),
            "source_instance_count": source_count,
            "constraints": assignment.get("constraints"),
            "semantic_contract_sha256": exact_digest,
            "semantic_contract_schema": (
                exact_contract.get("schema")
                if exact_contract is not None
                else None
            ),
            "semantic_contract_header": (
                {
                    key: value
                    for key, value in exact_contract.items()
                    if key
                    not in {
                        "cut_nodes",
                        "dependency_edges",
                        "logic_segments",
                        "capture_requirements",
                    }
                }
                if exact_contract is not None
                else None
            ),
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
            connection.executemany(
                "INSERT INTO exact_segment_bindings VALUES (?,?,?,?,?,?,?)",
                _exact_binding_rows(exact_contract, part_index),
            )
            connection.executemany(
                "INSERT INTO exact_capture_requirements VALUES (?,?,?,?,?,?,?)",
                _exact_capture_rows(exact_contract, part_index),
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
        or version not in SUPPORTED_ASSIGNMENT_INDEX_USER_VERSIONS
    ):
        connection.close()
        raise ValidationError("unsupported Phase 3 assignment index")
    return connection


def read_phase3_exact_semantic_partition(
    path: Path, fpga: str
) -> dict[str, Any]:
    """Read one FPGA's Static Exact records from the Phase 3 index.

    The packed assignment may contain a multi-gigabyte compressed semantic
    document.  Physical timing needs one FPGA at a time, so the production
    handoff keeps the canonical records in a per-FPGA binary section and never
    expands that document into a process-wide Python graph.  SQLite v1/v2
    artifacts remain readable.
    """

    connection = open_phase3_assignment_index(path)
    try:
        metadata = {
            row["key"]: json.loads(row["value"])
            for row in connection.execute("SELECT key,value FROM metadata")
        }
        part = connection.execute(
            "SELECT part_index FROM parts WHERE fpga=?", (fpga,)
        ).fetchone()
        if part is None:
            raise ValidationError(f"assignment index omits FPGA {fpga!r}")
        part_index = int(part[0])
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == 3:
            descriptor = metadata.get("semantic_contract_binary")
            if not isinstance(descriptor, Mapping):
                raise ValidationError(
                    "assignment index lacks its Static Exact binary descriptor"
                )
            artifact = descriptor.get("artifact")
            if not isinstance(artifact, str) or Path(artifact).name != artifact:
                raise ValidationError("Static Exact binary artifact name is invalid")
            return read_phase3_exact_binary_partition(
                path.with_name(artifact), descriptor, fpga
            )
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {
            "exact_segment_bindings",
            "exact_capture_requirements",
        }
        if not required.issubset(tables):
            raise ValidationError(
                "assignment index lacks indexed Static Exact payloads"
            )
        if version == 1:
            segments = [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT payload_json FROM exact_segment_bindings "
                    "WHERE part_index=? ORDER BY segment_id",
                    (part_index,),
                )
            ]
            captures = []
            for row in connection.execute(
                "SELECT payload_json FROM exact_capture_requirements "
                "WHERE part_index=? ORDER BY capture_id",
                (part_index,),
            ):
                try:
                    capture = json.loads(row[0])
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise ValidationError(
                        "legacy Static Exact capture JSON is invalid"
                    ) from error
                _validate_legacy_capture_mapping(capture)
                captures.append(capture)
        else:
            segment_kinds = ("launch_to_tx", "rx_to_tx", "rx_to_capture")
            segments = []
            for row in connection.execute(
                "SELECT segment_id,kind,source_cut_net,sink_cut_net,"
                "capture_requirement,source_semantics "
                "FROM exact_segment_bindings WHERE part_index=? "
                "ORDER BY segment_id",
                (part_index,),
            ):
                segment = {
                    "id": str(row[0]),
                    "kind": segment_kinds[int(row[1])],
                    "fpga": fpga,
                }
                for key, value in zip(
                    (
                        "source_cut_net",
                        "sink_cut_net",
                        "capture_requirement",
                        "source_semantics",
                    ),
                    row[2:],
                ):
                    if value is not None:
                        segment[key] = (
                            "configuration-stable-constant"
                            if key == "source_semantics"
                            else str(value)
                        )
                segments.append(segment)
            captures = []
            for row in connection.execute(
                "SELECT capture_id,cut_net,kind,endpoint,port,bit "
                "FROM exact_capture_requirements WHERE part_index=? "
                "ORDER BY capture_id",
                (part_index,),
            ):
                capture = {
                    "id": str(row[0]),
                    "cut_net": str(row[1]),
                    "fpga": fpga,
                    "kind": (
                        "top-output" if int(row[2]) == 0
                        else "architectural-state"
                    ),
                    "endpoint": str(row[3]),
                }
                if row[4] is not None:
                    capture["port"] = str(row[4])
                if int(row[2]) == 1:
                    capture["bit"] = _decode_legacy_capture_bit(row[5])
                elif row[5] is not None:
                    raise ValidationError(
                        "legacy Static Exact capture is invalid"
                    )
                captures.append(capture)
        return {
            "schema": metadata.get("semantic_contract_schema"),
            "sha256": metadata.get("semantic_contract_sha256"),
            "fpga": fpga,
            "logic_segments": segments,
            "capture_requirements": captures,
        }
    finally:
        connection.close()


def read_phase3_exact_semantic_contract(
    path: Path, assignment: Mapping[str, Any]
) -> dict[str, Any]:
    """Reconstruct the canonical Static Exact contract from its typed index.

    Production consumers query bounded per-FPGA slices and never call this
    function.  It preserves the historical ``read_json(assignment.json)``
    behavior for validation, replay and debugging without forcing Phase 3 to
    publish a duplicate compressed JSON graph.
    """

    connection = open_phase3_assignment_index(path)
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        metadata = {
            str(row["key"]): json.loads(row["value"])
            for row in connection.execute("SELECT key,value FROM metadata")
        }
        if version == 3:
            descriptor = metadata.get("semantic_contract_binary")
            if not isinstance(descriptor, Mapping):
                raise ValidationError(
                    "assignment index lacks its Static Exact binary descriptor"
                )
            artifact = descriptor.get("artifact")
            if not isinstance(artifact, str) or Path(artifact).name != artifact:
                raise ValidationError("Static Exact binary artifact name is invalid")
            return read_phase3_exact_binary_contract(
                path.with_name(artifact), descriptor, assignment
            )
        if version != 2:
            raise ValidationError(
                "full indexed Static Exact reconstruction requires index v2/v3"
            )
        header = metadata.get("semantic_contract_header")
        schema = metadata.get("semantic_contract_schema")
        expected_digest = metadata.get("semantic_contract_sha256")
        if (
            not isinstance(header, dict)
            or header.get("schema") != schema
            or not isinstance(expected_digest, str)
            or len(expected_digest) != 64
            or assignment.get("semantic_contract_sha256") != expected_digest
        ):
            raise ValidationError(
                "assignment index semantic contract identity is incomplete"
            )

        parts = {
            int(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT part_index,fpga FROM parts ORDER BY part_index"
            )
        }
        segment_kinds = ("launch_to_tx", "rx_to_tx", "rx_to_capture")
        logic_segments = []
        for row in connection.execute(
            "SELECT segment_id,kind,part_index,source_cut_net,sink_cut_net,"
            "capture_requirement,source_semantics "
            "FROM exact_segment_bindings ORDER BY segment_id"
        ):
            kind = int(row[1])
            part_index = int(row[2])
            if kind < 0 or kind >= len(segment_kinds) or part_index not in parts:
                raise ValidationError(
                    "assignment index semantic segment is invalid"
                )
            segment = {
                "id": str(row[0]),
                "kind": segment_kinds[kind],
                "fpga": parts[part_index],
            }
            for key, value in zip(
                (
                    "source_cut_net",
                    "sink_cut_net",
                    "capture_requirement",
                    "source_semantics",
                ),
                row[3:],
            ):
                if value is not None:
                    segment[key] = (
                        "configuration-stable-constant"
                        if key == "source_semantics"
                        else str(value)
                    )
            logic_segments.append(segment)

        capture_requirements = []
        for row in connection.execute(
            "SELECT capture_id,cut_net,part_index,kind,endpoint,port,bit "
            "FROM exact_capture_requirements ORDER BY capture_id"
        ):
            part_index = int(row[2])
            kind = int(row[3])
            if part_index not in parts or kind not in {0, 1}:
                raise ValidationError(
                    "assignment index semantic capture is invalid"
                )
            capture = {
                "id": str(row[0]),
                "cut_net": str(row[1]),
                "fpga": parts[part_index],
                "kind": "top-output" if kind == 0 else "architectural-state",
                "endpoint": str(row[4]),
            }
            if row[5] is not None:
                capture["port"] = str(row[5])
            if kind == 1:
                capture["bit"] = _decode_legacy_capture_bit(row[6])
            elif row[6] is not None:
                raise ValidationError("legacy Static Exact capture is invalid")
            capture_requirements.append(capture)

        cut_by_net = {
            cut.get("net"): cut
            for cut in assignment.get("cut_nets", [])
            if isinstance(cut, Mapping) and isinstance(cut.get("net"), str)
        }
        source_segments: dict[str, list[str]] = {}
        for row in connection.execute(
            "SELECT net_id,ordinal,segment_id FROM exact_cut_sources "
            "ORDER BY net_id,ordinal"
        ):
            source_segments.setdefault(str(row[0]), []).append(str(row[2]))
        capture_segments: dict[str, list[str]] = {}
        for segment in logic_segments:
            if segment["kind"] == "rx_to_capture":
                capture_segments.setdefault(
                    str(segment["source_cut_net"]), []
                ).append(str(segment["id"]))

        cut_nodes = []
        node_fields = (
            "net",
            "cut_class",
            "source_fpgas",
            "sink_fpgas",
            "dependency_level",
            "combinational_dependency_depth",
            "predecessor_cut_nets",
        )
        for row in connection.execute(
            "SELECT net_id FROM exact_cut_nodes ORDER BY net_id"
        ):
            net_id = str(row[0])
            cut = cut_by_net.get(net_id)
            if cut is None or any(field not in cut for field in node_fields):
                raise ValidationError(
                    "assignment index semantic cut node is incomplete"
                )
            cut_nodes.append(
                {
                    **{field: cut[field] for field in node_fields},
                    "source_segment_ids": source_segments.get(net_id, []),
                    "capture_segment_ids": sorted(
                        capture_segments.get(net_id, [])
                    ),
                }
            )

        dependency_edges = [
            {
                "from": str(row[0]),
                "to": str(row[1]),
                "segment": str(row[2]),
            }
            for row in connection.execute(
                "SELECT source_cut_net,sink_cut_net,segment_id "
                "FROM exact_segment_bindings WHERE kind=1 "
                "ORDER BY sink_cut_net,source_cut_net"
            )
        ]
        contract = {
            **header,
            "cut_nodes": cut_nodes,
            "dependency_edges": dependency_edges,
            "logic_segments": logic_segments,
            "capture_requirements": capture_requirements,
        }
        if semantic_contract_sha256(contract) != expected_digest:
            raise ValidationError(
                "reconstructed Static Exact semantic contract digest disagrees"
            )
        return contract
    finally:
        connection.close()


def phase3_exact_semantic_counts(path: Path) -> dict[str, int]:
    """Return bounded Static Exact population counts from the index."""

    connection = open_phase3_assignment_index(path)
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == 3:
            metadata = {
                row["key"]: json.loads(row["value"])
                for row in connection.execute("SELECT key,value FROM metadata")
            }
            descriptor = metadata.get("semantic_contract_binary")
            if not isinstance(descriptor, Mapping):
                raise ValidationError(
                    "assignment index lacks its Static Exact binary descriptor"
                )
            artifact = descriptor.get("artifact")
            if not isinstance(artifact, str) or Path(artifact).name != artifact:
                raise ValidationError("Static Exact binary artifact name is invalid")
            counts = phase3_exact_binary_counts(
                path.with_name(artifact), descriptor
            )
            return {
                "logic_segments": counts["logic_segments"],
                "capture_requirements": counts["capture_requirements"],
            }
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {
            "exact_segment_bindings",
            "exact_capture_requirements",
        }
        if not required.issubset(tables):
            raise ValidationError(
                "assignment index lacks indexed Static Exact payloads"
            )
        return {
            "logic_segments": int(
                connection.execute(
                    "SELECT count(*) FROM exact_segment_bindings"
                ).fetchone()[0]
            ),
            "capture_requirements": int(
                connection.execute(
                    "SELECT count(*) FROM exact_capture_requirements"
                ).fetchone()[0]
            ),
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
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        parts = {
            str(row[1]): int(row[0])
            for row in connection.execute(
                "SELECT part_index,fpga FROM parts ORDER BY part_index"
            )
        }
        if version == 3:
            metadata = {
                str(row[0]): json.loads(row[1])
                for row in connection.execute(
                    "SELECT key,value FROM metadata WHERE key="
                    "'semantic_contract_binary'"
                )
            }
            descriptor = metadata.get("semantic_contract_binary")
            if not isinstance(descriptor, Mapping):
                raise ValidationError(
                    "assignment index lacks its Static Exact binary descriptor"
                )
            artifact = descriptor.get("artifact")
            if not isinstance(artifact, str) or Path(artifact).name != artifact:
                raise ValidationError("Static Exact binary artifact name is invalid")
            binary_path = path.with_name(artifact)
            source_segments = {
                net: [_identity_segment(number) for number in numbers]
                for net, numbers in iter_phase3_exact_node_sources(
                    binary_path, descriptor
                )
            }
            consumer_segments: dict[tuple[str, int], list[str]] = defaultdict(list)
            for segment in iter_phase3_exact_segments(binary_path, descriptor):
                source = segment.get("source_cut_net")
                if isinstance(source, str):
                    consumer_segments[(source, parts[segment["fpga"]])].append(
                        segment["id"]
                    )
            return set(source_segments), source_segments, dict(consumer_segments)

        exact_nodes = {
            str(row[0])
            for row in connection.execute("SELECT net_id FROM exact_cut_nodes")
        }
        source_segments: dict[str, list[str]] = defaultdict(list)
        for row in connection.execute(
            "SELECT net_id,segment_id FROM exact_cut_sources "
            "ORDER BY net_id,ordinal"
        ):
            source_segments[str(row[0])].append(str(row[1]))
        consumer_segments: dict[tuple[str, int], list[str]] = defaultdict(list)
        for row in connection.execute(
            "SELECT source_cut_net,part_index,segment_id "
            "FROM exact_logic_segments "
            "ORDER BY source_cut_net,part_index,segment_id"
        ):
            consumer_segments[(str(row[0]), int(row[1]))].append(str(row[2]))
        return exact_nodes, dict(source_segments), dict(consumer_segments)
    finally:
        connection.close()


def _identity_segment(number: int) -> str:
    if isinstance(number, bool) or not isinstance(number, int) or number < 0:
        raise ValidationError("Static Exact binary segment number is invalid")
    return f"segment{number:06d}"


def phase3_exact_relation_counts(path: Path) -> dict[str, int]:
    """Return storage-level relation counts for qualification evidence."""

    connection = sqlite3.connect(
        f"file:{path.resolve()}?mode=ro&immutable=1", uri=True
    )
    try:
        cut_sinks = int(
            connection.execute("SELECT count(*) FROM cut_sinks").fetchone()[0]
        )
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='semantic_contract_binary'"
        ).fetchone()
        descriptor = json.loads(row[0]) if row is not None else None
        if descriptor is not None:
            if not isinstance(descriptor, Mapping):
                raise ValidationError(
                    "assignment index lacks its Static Exact binary descriptor"
                )
            artifact = descriptor.get("artifact")
            if not isinstance(artifact, str) or Path(artifact).name != artifact:
                raise ValidationError("Static Exact binary artifact name is invalid")
            counts = phase3_exact_binary_counts(path.with_name(artifact), descriptor)
            return {
                "cut_sinks": cut_sinks,
                "exact_cut_nodes": counts["cut_nodes"],
                "exact_cut_source_segments": counts["source_segments"],
                "exact_consumer_segments": counts["consumer_segments"],
            }
        tables = {
            str(item[0])
            for item in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "exact_cut_nodes" not in tables:
            return {
                "cut_sinks": cut_sinks,
                "exact_cut_nodes": 0,
                "exact_cut_source_segments": 0,
                "exact_consumer_segments": 0,
            }
        return {
            "cut_sinks": cut_sinks,
            "exact_cut_nodes": int(connection.execute(
                "SELECT count(*) FROM exact_cut_nodes"
            ).fetchone()[0]),
            "exact_cut_source_segments": int(connection.execute(
                "SELECT count(*) FROM exact_cut_sources"
            ).fetchone()[0]),
            "exact_consumer_segments": int(connection.execute(
                "SELECT count(*) FROM exact_logic_segments"
            ).fetchone()[0]),
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
        if metadata.get("schema") not in {
            LEGACY_ASSIGNMENT_INDEX_SCHEMA,
            PREVIOUS_ASSIGNMENT_INDEX_SCHEMA,
            ASSIGNMENT_INDEX_SCHEMA,
        }:
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
        has_exact_payloads = {
            "exact_segment_bindings",
            "exact_capture_requirements",
        }.issubset(tables)
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
        elif has_exact_payloads:
            version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            _validate_legacy_sqlite_capture_bits(connection, version)
            exact_segment_bindings = int(
                connection.execute(
                    "SELECT count(*) FROM exact_segment_bindings"
                ).fetchone()[0]
            )
            exact_capture_requirements = int(
                connection.execute(
                    "SELECT count(*) FROM exact_capture_requirements"
                ).fetchone()[0]
            )
            invalid_exact_parts = int(
                connection.execute(
                    """SELECT count(*) FROM (
                         SELECT part_index FROM exact_segment_bindings
                         UNION ALL
                         SELECT part_index FROM exact_capture_requirements
                       ) x LEFT JOIN parts p USING(part_index)
                       WHERE p.part_index IS NULL"""
                ).fetchone()[0]
            )
            if invalid_exact_parts:
                raise ValidationError(
                    "assignment index Static Exact FPGA binding is invalid"
                )
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
            metadata.get("schema")
            not in {
                LEGACY_ASSIGNMENT_INDEX_SCHEMA,
                PREVIOUS_ASSIGNMENT_INDEX_SCHEMA,
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
