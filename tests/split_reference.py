"""Independent retained v2 DUT transformation oracle; never a production entry."""
import json,os,shutil,sqlite3,uuid
from pathlib import Path
from typing import Any,Mapping
from emuflow.errors import ValidationError
from emuflow.platform import Platform
from emuflow.native_tools import native_file_generation
from emuflow.phase3_assignment_index import ASSIGNMENT_INDEX_SCHEMA,open_phase3_assignment_index
from emuflow.phase3_exact_binary import phase3_exact_binary_counts
from emuflow.store_publication import (StoreSource,bind_store_source,bind_view_sources,publish_sqlite_store,require_store_sources_unchanged)
from emuflow.split_netlist_storage import (DDL,SPLIT_NETLIST_APPLICATION_ID,
    PREVIOUS_SPLIT_NETLIST_USER_VERSION as SPLIT_NETLIST_USER_VERSION,
    PREVIOUS_SPLIT_NETLIST_STORAGE_SCHEMA as SPLIT_NETLIST_STORAGE_SCHEMA,
    _binary_artifact_path,_sqlite_uri,_json,read_compact_emuir_control_metadata,validate_split_netlists_sqlite)

def _schedule_incoming_rows(schedule, parts):
    if len(schedule['entries']) > 16384:
        raise ValidationError('split relation fixture exceeds hop bound')
    return [(net, parts[fpga], entry) for net, fpga, entry
            in schedule.iter_incoming_records()]


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

    ir_binding = bind_store_source(ir_path, "emuir-compact-v2")
    assignment_generation = native_file_generation(assignment_index_path)
    source_bindings = (ir_binding,) + bind_view_sources(
        (schedule, "tdm-schedule-store-v4")
    )
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
    if assignment_metadata.get("schema") not in {
        "emuflow.phase3-assignment-sqlite/v3",
        ASSIGNMENT_INDEX_SCHEMA,
    }:
        raise ValidationError("Phase 6 assignment index schema is invalid")
    source_bindings += (
        StoreSource(
            Path(assignment_index_path).resolve(),
            {
                "emuflow.phase3-assignment-sqlite/v3": "assignment-index-v3",
                ASSIGNMENT_INDEX_SCHEMA: "assignment-index-v4",
            }[assignment_metadata["schema"]],
            assignment_generation,
        ),
    )
    if assignment_metadata.get("design") != expected_design:
        raise ValidationError("Phase 6 assignment index design disagrees")
    if assignment_metadata.get("platform") != expected_platform:
        raise ValidationError("Phase 6 assignment index platform disagrees")
    part_index = {row["fpga"]: row["part_index"] for row in parts}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    exact_binary = assignment_metadata.get("semantic_contract_binary")
    staged_binary: Path | None = None
    final_binary: Path | None = None
    binary_binding = None
    linked_binary = False
    if isinstance(exact_binary, Mapping):
        source_binary = _binary_artifact_path(
            assignment_index_path, exact_binary
        )
        binary_schema = {
            "emuflow.phase3-static-exact-binary/v1": "phase3-exact-data-v1",
            "emuflow.phase3-static-exact-binary/v2": "phase3-exact-data-v2",
        }.get(exact_binary.get("schema"))
        if binary_schema is None:
            raise ValidationError("split Static Exact binary schema is unsupported")
        binary_binding = bind_store_source(source_binary, binary_schema)
        phase3_exact_binary_counts(source_binary, exact_binary)
        final_binary = _binary_artifact_path(output_path, exact_binary)
        staged_binary = final_binary.with_name(".fixture-exact.bin")
        shutil.copyfile(source_binary, staged_binary)
        linked_binary = staged_binary is not None and os.path.samefile(
            source_binary, staged_binary
        )
        try:
            generation = native_file_generation(binary_binding.path)
            if generation[:5] != binary_binding.generation[:5] or (
                not linked_binary and generation != binary_binding.generation
            ):
                raise ValidationError("split Static Exact source generation changed")
            binary_binding = StoreSource(
                binary_binding.path, binary_binding.schema, generation
            )
        except BaseException:
            if staged_binary is not None:
                staged_binary.unlink(missing_ok=True)
            raise
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
        source = read_compact_emuir_control_metadata(ir_path)
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
            "semantic_contract_binary": exact_binary,
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
        has_legacy_exact = connection.execute(
            "SELECT 1 FROM assignment.sqlite_master "
            "WHERE type='table' AND name='exact_cut_nodes'"
        ).fetchone()
        if not isinstance(exact_binary, Mapping) and has_legacy_exact is not None:
            connection.execute(
                "INSERT INTO exact_cut_nodes "
                "SELECT * FROM assignment.exact_cut_nodes"
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
            exact_binary_path=(
                staged_binary
                if staged_binary is not None
                else final_binary
            ),
        )
        connection.close()
        require_store_sources_unchanged(source_bindings)
        if binary_binding is not None:
            require_store_sources_unchanged((binary_binding,))
        if staged_binary is not None and final_binary is not None:
            os.replace(staged_binary, final_binary)
            staged_binary = None
        if binary_binding is not None:
            generation = native_file_generation(binary_binding.path)
            # Creating/renaming our read-only hardlink changes ctime. Only
            # this known alias operation may advance it; inode/content/mtime
            # must retain their original binding. The shared publisher checks
            # the resulting generation immediately before committing the DB.
            if generation[:5] != binary_binding.generation[:5] or (
                not linked_binary and generation != binary_binding.generation
            ):
                raise ValidationError("split Static Exact source generation changed")
            source_bindings += (
                StoreSource(binary_binding.path, binary_binding.schema, generation),
            )
        publish_sqlite_store(
            temporary, output_path, "split-netlists-v2", sources=source_bindings
        )
        return summary
    except BaseException:
        try:
            connection.close()
        finally:
            temporary.unlink(missing_ok=True)
            if staged_binary is not None:
                staged_binary.unlink(missing_ok=True)
        raise
