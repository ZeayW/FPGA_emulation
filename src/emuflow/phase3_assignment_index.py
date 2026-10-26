"""Compact, queryable Phase 3 assignment data for downstream data planes.

The SQLite artifact owns the production instance, partition, cut and Static
Exact relations.  The sibling JSON is a small selector and compatibility
envelope.  Phase 4--7 consumers can therefore scan typed records without
expanding packed clusters or republishing the same graph as JSON.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator, Mapping

from .sqlite_artifacts import immutable_sqlite_uri
from .errors import ValidationError
from .store_publication import (
    StoreSource,
    bind_store_source,
    publish_sqlite_store,
    require_store_sources_unchanged,
    validate_store_destination,
)
from .ir import EmuIR
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
from .routing import static_exact_contract_from_assignment


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
    native_owner = getattr(instance_assignment, "_native_publication", False)
    source_bindings = ()
    if native_owner:
        if instance_assignment._native_primary_artifact is None:
            raise ValidationError("native assignment primary ownership is missing")
        source_bindings = (
            StoreSource(
                Path(instance_assignment.path).resolve(),
                "emuir-compact-v2",
                instance_assignment._native_source_generation,
            ),
            StoreSource(
                Path(instance_assignment._native_primary_artifact).resolve(),
                "partition-cluster-columns-v2",
                instance_assignment._native_primary_generation,
            ),
        )
    if native_owner:
        from .phase3_assignment_native import native_assignment_cut_source

        cut_source = native_assignment_cut_source(assignment)
        if cut_source is not None:
            source_bindings += cut_source.auxiliary_sources
            source_bindings += tuple(
                StoreSource(source_path, schema, generation)
                for source_path, schema, generation in zip(
                    cut_source.paths,
                    ("exact-nodes-v2", "exact-captures-v1"),
                    cut_source.generations,
                )
            )
    source_bindings = validate_store_destination(path, source_bindings)
    assignment_parts = getattr(instance_assignment, "part_ids", None)
    if assignment_parts is None:
        assignment_parts = instance_assignment.values()
    if any(target not in part_index for target in assignment_parts):
        raise ValidationError("Phase 3 assignment references an unknown FPGA")

    exact_contract = static_exact_contract_from_assignment(assignment)
    # ``build_partition_assignment`` owns the in-process Static Exact
    # contract and computes this identity when it creates the final Phase 3
    # assignment.  Recomputing it here makes publication serialize the same
    # multi-million-record contract a second time immediately before the
    # binary writer scans it again.  The production publisher consumes the
    # already validated owner-provided identity.
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
    connection = None
    try:
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
        if native_owner:
            from .phase3_assignment_native import publish_native_assignment_index
            native_writer_metrics = phase3_runtime_call(
                "assignment_index.native_publish", publish_native_assignment_index,
                temporary, ir, assignment, part_ids, metadata,
            )
        else:
            connection = sqlite3.connect(temporary)
            connection.executescript(
                f"""
                PRAGMA journal_mode=OFF;
                PRAGMA synchronous=OFF;
                PRAGMA temp_store=FILE;
                PRAGMA cache_size=-65536;
                PRAGMA application_id={ASSIGNMENT_INDEX_APPLICATION_ID};
                PRAGMA user_version={ASSIGNMENT_INDEX_USER_VERSION};
                {CURRENT_DDL}
                """
            )
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

            compact_rows = getattr(
                instance_assignment, "iter_source_assignments", None
            )

            def primary_rows():
                if callable(compact_rows):
                    observed = 0
                    for source_seq, identity, target in compact_rows():
                        if source_seq != observed or target not in part_index:
                            raise ValidationError(
                                "Phase 3 compact assignment coverage is invalid"
                            )
                        observed += 1
                        yield (source_seq, part_index[target], identity, 0)
                    if observed != len(source_instances):
                        raise ValidationError(
                            "Phase 3 compact assignment coverage is incomplete"
                        )
                    return
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

            raw_cuts = assignment.get("cut_nets")
            if not isinstance(raw_cuts, list):
                raise ValidationError("cut-net assignment is invalid")
            cut_digest = hashlib.sha256()
            cut_rows: list[tuple[Any, ...]] = []
            sink_rows: list[tuple[Any, ...]] = []
            predecessor_rows: list[tuple[Any, ...]] = []

            def flush_cut_rows() -> None:
                if cut_rows:
                    connection.executemany(
                        "INSERT INTO cuts VALUES (?,?,?,?,?,?,?,?)", cut_rows
                    )
                    cut_rows.clear()
                if sink_rows:
                    connection.executemany(
                        "INSERT INTO cut_sinks VALUES (?,?,?)", sink_rows
                    )
                    sink_rows.clear()
                if predecessor_rows:
                    connection.executemany(
                        "INSERT INTO cut_predecessors VALUES (?,?,?)",
                        predecessor_rows,
                    )
                    predecessor_rows.clear()

            previous_net = None
            for cut_index, cut in enumerate(raw_cuts):
                normalized = _normalized_cut_record(cut, part_index)
                net_id = normalized["net"]
                if previous_net is not None and net_id <= previous_net:
                    raise ValidationError("cut-net assignment order is not canonical")
                previous_net = net_id
                encoded = _json(normalized).encode("utf-8")
                cut_digest.update(encoded)
                cut_digest.update(b"\n")
                source_index = part_index[normalized["source_fpgas"][0]]
                cut_rows.append(
                    (
                        cut_index,
                        net_id,
                        source_index,
                        normalized["cut_class"],
                        normalized["sink_endpoints"],
                        normalized.get("transport_round"),
                        normalized.get("dependency_level"),
                        normalized.get("combinational_dependency_depth"),
                    )
                )
                for sink in normalized["sink_fpgas"]:
                    sink_rows.append((net_id, source_index, part_index[sink]))
                    if len(sink_rows) >= 8192:
                        flush_cut_rows()
                for ordinal, predecessor in enumerate(
                    normalized.get("predecessor_cut_nets", [])
                ):
                    predecessor_rows.append((cut_index, ordinal, predecessor))
                    if len(predecessor_rows) >= 8192:
                        flush_cut_rows()
                if len(cut_rows) >= 8192:
                    flush_cut_rows()
            flush_cut_rows()
            cut_records_sha256 = cut_digest.hexdigest()
            connection.executemany(
                "INSERT INTO metadata(key,value) VALUES (?,?)",
                (
                    ("cut_records_schema", _json(CUT_RECORDS_SCHEMA)),
                    ("cut_records", _json(len(raw_cuts))),
                    ("cut_records_sha256", _json(cut_records_sha256)),
                ),
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
        if connection is not None:
            connection.close()
        summary["writer_implementation"] = (
            "native-primary-assignment-index-v1" if native_owner else "python-reference-v1"
        )
        if native_owner:
            summary["native_writer_metrics"] = native_writer_metrics
        require_store_sources_unchanged(source_bindings)
        previous_binary = path.with_name(
            f".{EXACT_BINARY_ARTIFACT}.{uuid.uuid4().hex}.previous"
        )
        moved_previous = False
        installed_binary = False
        try:
            if exact_contract is not None:
                if exact_binary_path.exists():
                    os.replace(exact_binary_path, previous_binary)
                    moved_previous = True
                os.replace(exact_binary_temporary, exact_binary_path)
                installed_binary = True
            publication_sources = source_bindings
            if exact_contract is not None:
                publication_sources += (
                    bind_store_source(exact_binary_path, "phase3-exact-data-v2"),
                )
            publish_sqlite_store(
                temporary, path, "assignment-index-v4", sources=publication_sources
            )
        except BaseException:
            if exact_contract is not None:
                if installed_binary:
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
            if connection is not None:
                connection.close()
        finally:
            temporary.unlink(missing_ok=True)
            exact_binary_temporary.unlink(missing_ok=True)
        raise


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


def read_phase3_exact_semantic_partition(
    path: Path, fpga: str
) -> dict[str, Any]:
    """Read one FPGA's Static Exact records from a supported v3/v4 index."""

    connection = open_phase3_assignment_index(path)
    try:
        part = connection.execute(
            "SELECT part_index FROM parts WHERE fpga=?", (fpga,)
        ).fetchone()
        if part is None:
            raise ValidationError(f"assignment index omits FPGA {fpga!r}")
        binary_path, descriptor = _exact_binary_reference(connection, path)
        return read_phase3_exact_binary_partition(binary_path, descriptor, fpga)
    finally:
        connection.close()


def read_phase3_exact_semantic_contract(
    path: Path, assignment: Mapping[str, Any]
) -> dict[str, Any]:
    """Explicitly reconstruct the v3/v4 contract for validation or debugging.

    Production consumers use bounded per-FPGA records instead.
    """

    connection = open_phase3_assignment_index(path)
    try:
        binary_path, descriptor = _exact_binary_reference(connection, path)
        return read_phase3_exact_binary_contract(binary_path, descriptor, assignment)
    finally:
        connection.close()


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
