"""Set-oriented Phase 7 placement-IR materialization.

Python prepares only the bounded transport overlay.  The complete DUT remains
in the shared Phase 6 SQLite database and is copied, joined, and rewritten by
SQLite itself; no iterator of Python cell/net/endpoint dictionaries is created
on the production path.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .emuir_compact import DDL
from .emuir_storage import (
    EMUIR_COMPACT_STORAGE_SCHEMA,
    EMUIR_SQLITE_APPLICATION_ID,
    _extras,
    _json,
)
from .errors import ValidationError


def _sqlite_uri(path: Path) -> str:
    return f"file:{path.resolve()}?mode=ro&immutable=1"


def _cell_row(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        record["id"],
        record.get("name"),
        record["type"],
        _json(record["resources"]),
        _json(record["parameters"]) if "parameters" in record else None,
        _json(record["attributes"]) if "attributes" in record else None,
        _json(record.get("constant_connections", [])),
        _extras(
            record,
            {
                "id",
                "name",
                "type",
                "resources",
                "parameters",
                "attributes",
                "constant_connections",
            },
        ),
    )


def _endpoint_row(
    net_id: str,
    role: int,
    order_group: int,
    order_a: int,
    order_b: int,
    order_c: int,
    endpoint: Mapping[str, Any],
) -> tuple[Any, ...]:
    return (
        net_id,
        role,
        order_group,
        order_a,
        order_b,
        order_c,
        endpoint.get("instance"),
        endpoint["port"],
        int(endpoint["bit"]),
        _extras(endpoint, {"instance", "port", "bit"}),
    )


def write_placement_emuir_from_split(
    split_path: Path,
    fpga: str,
    output_path: Path,
    *,
    design: Mapping[str, Any],
    ports: Sequence[Mapping[str, Any]],
    clocks: Sequence[Mapping[str, Any]],
    warnings: Sequence[Any],
    transport_instances: Sequence[Mapping[str, Any]],
    transport_nets: Sequence[Mapping[str, Any]],
    added_endpoints: Sequence[tuple[str, int, int, Mapping[str, Any]]],
    port_added_endpoints: Sequence[
        tuple[str, int, int, Mapping[str, Any]]
    ],
    recompute_fanout: Iterable[str],
    expected_source_nets: Iterable[str],
    expected_rx_bindings: Iterable[tuple[str, str]],
) -> None:
    """Materialize one FPGA without decoding the DUT in Python.

    ``added_endpoints`` records are ``(net, role, order, endpoint)``.  Port
    additions are ``(driver_port, driver_bit, order, endpoint)`` and are bound
    to their one DUT net inside SQLite.
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(
        f".{output_path.name}.{uuid.uuid4().hex}.tmp"
    )
    connection = sqlite3.connect(temporary)
    try:
        connection.executescript(
            f"""
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            PRAGMA cache_size=-131072;
            PRAGMA application_id={EMUIR_SQLITE_APPLICATION_ID};
            PRAGMA user_version=2;
            {DDL}
            CREATE TEMP TABLE stage_cells(
              id TEXT PRIMARY KEY,
              name TEXT,
              type TEXT NOT NULL,
              resources_json TEXT NOT NULL,
              parameters_json TEXT,
              attributes_json TEXT,
              constants_json TEXT NOT NULL,
              extras_json TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TEMP TABLE stage_nets(
              id TEXT PRIMARY KEY,
              name TEXT,
              yosys_bit INTEGER,
              bus_index INTEGER,
              fanout INTEGER,
              cut_class TEXT NOT NULL,
              extras_json TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TEMP TABLE stage_endpoints(
              net_id TEXT NOT NULL,
              role INTEGER NOT NULL,
              order_group INTEGER NOT NULL,
              order_a INTEGER NOT NULL,
              order_b INTEGER NOT NULL,
              order_c INTEGER NOT NULL,
              instance_id TEXT,
              port TEXT NOT NULL,
              bit INTEGER NOT NULL,
              extras_json TEXT NOT NULL
            );
            CREATE INDEX stage_endpoint_order ON stage_endpoints(
              net_id,role,order_group,order_a,order_b,order_c
            );
            CREATE TEMP TABLE stage_aliases(
              net_id TEXT NOT NULL,
              ordinal INTEGER NOT NULL,
              alias TEXT NOT NULL,
              PRIMARY KEY(net_id,ordinal)
            ) WITHOUT ROWID;
            CREATE TEMP TABLE requested_nets(
              kind INTEGER NOT NULL,
              net_id TEXT NOT NULL,
              PRIMARY KEY(kind,net_id)
            ) WITHOUT ROWID;
            CREATE TEMP TABLE expected_rx(
              net_id TEXT NOT NULL,
              instance_id TEXT NOT NULL,
              PRIMARY KEY(net_id,instance_id)
            ) WITHOUT ROWID;
            CREATE TEMP TABLE fanout_recompute(
              net_id TEXT PRIMARY KEY
            ) WITHOUT ROWID;
            CREATE TEMP TABLE port_additions(
              driver_port TEXT NOT NULL,
              driver_bit INTEGER NOT NULL,
              overlay_order INTEGER NOT NULL,
              instance_id TEXT,
              port TEXT NOT NULL,
              bit INTEGER NOT NULL,
              extras_json TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "ATTACH DATABASE ? AS split", (_sqlite_uri(split_path),)
        )
        part = connection.execute(
            "SELECT part_index FROM split.parts WHERE fpga=?", (fpga,)
        ).fetchone()
        if part is None:
            raise ValidationError(f"split netlists omit FPGA {fpga!r}")
        part_index = int(part[0])

        connection.execute(
            """INSERT INTO stage_cells
            SELECT COALESCE(c.copy_id,i.original_id),
                   CASE WHEN i.flags & 1 THEN i.original_id ELSE i.name END,
                   i.type,r.value,p.value,a.value,k.value,
                   CASE WHEN c.is_replica=1 THEN
                     json_set(x.value,'$.replica_of',i.original_id,
                              '$.replica_target_fpga',?)
                   ELSE x.value END
            FROM split.instance_copies c
            JOIN split.source_instances i
              ON i.source_instance_seq=c.source_instance_seq
            JOIN split.payloads r ON r.seq=i.resources_seq
            LEFT JOIN split.payloads p ON p.seq=i.parameters_seq
            LEFT JOIN split.payloads a ON a.seq=i.attributes_seq
            JOIN split.payloads k ON k.seq=i.constants_seq
            JOIN split.payloads x ON x.seq=i.extras_seq
            WHERE c.part_index=?""",
            (fpga, part_index),
        )
        connection.executemany(
            "INSERT INTO stage_cells VALUES (?,?,?,?,?,?,?,?)",
            (_cell_row(record) for record in transport_instances),
        )

        connection.execute(
            """CREATE TEMP TABLE base_sink_counts AS
            SELECT source_net_seq,count(*) AS fanout
            FROM split.endpoint_segments
            WHERE part_index=? AND role=1
            GROUP BY source_net_seq""",
            (part_index,),
        )
        connection.execute(
            "CREATE UNIQUE INDEX base_sink_identity "
            "ON base_sink_counts(source_net_seq)"
        )
        connection.execute(
            """CREATE TEMP TABLE exact_info(
              net_id TEXT PRIMARY KEY,
              role TEXT NOT NULL,
              segment_ids_json TEXT NOT NULL
            ) WITHOUT ROWID"""
        )
        digest_row = connection.execute(
            "SELECT value FROM split.metadata "
            "WHERE key='semantic_contract_sha256'"
        ).fetchone()
        digest = json.loads(digest_row[0]) if digest_row is not None else None
        if digest is not None:
            connection.execute(
                """INSERT INTO exact_info(net_id,role,segment_ids_json)
                SELECT DISTINCT cs.net_id,
                  CASE
                    WHEN s.source_kind='transport_shadow'
                      THEN 'transport_shadow_sink'
                    WHEN cs.source_part_index=? THEN 'cut_source'
                    ELSE 'local_logic'
                  END,
                  '[]'
                FROM split.cut_sinks cs
                JOIN split.source_nets n ON n.original_net=cs.net_id
                JOIN split.net_segments s
                  ON s.source_net_seq=n.source_net_seq AND s.part_index=?""",
                (part_index, part_index),
            )
            connection.execute(
                """UPDATE exact_info SET segment_ids_json=COALESCE((
                  SELECT json_group_array(segment_id) FROM (
                    SELECT segment_id FROM split.exact_cut_sources x
                    WHERE x.net_id=exact_info.net_id ORDER BY ordinal
                  )
                ),'[]') WHERE role='cut_source'"""
            )
            connection.execute(
                """UPDATE exact_info SET segment_ids_json=COALESCE((
                  SELECT json_group_array(segment_id) FROM (
                    SELECT segment_id FROM split.exact_logic_segments x
                    WHERE x.source_cut_net=exact_info.net_id
                      AND x.part_index=? ORDER BY segment_id
                  )
                ),'[]') WHERE role='transport_shadow_sink'""",
                (part_index,),
            )

        connection.execute(
            """INSERT INTO stage_nets
            SELECT n.original_net,
                   CASE WHEN n.flags & 1 THEN n.original_net ELSE n.name END,
                   NULL,0,COALESCE(b.fanout,0),n.cut_class,
                   CASE WHEN e.net_id IS NULL THEN
                     json_object('original_net',n.original_net,
                                 'source_kind',s.source_kind)
                   ELSE
                     json_set(
                       json_object('original_net',n.original_net,
                                   'source_kind',s.source_kind),
                       '$.static_exact_boundary',
                       json_object(
                         'semantic_contract_sha256',?,
                         'role',e.role,
                         'logic_segment_ids',json(e.segment_ids_json)
                       )
                     )
                   END
            FROM split.net_segments s
            JOIN split.source_nets n
              ON n.source_net_seq=s.source_net_seq
            LEFT JOIN base_sink_counts b
              ON b.source_net_seq=s.source_net_seq
            LEFT JOIN exact_info e ON e.net_id=n.original_net
            WHERE s.part_index=?""",
            (digest, part_index),
        )

        # Remove synthetic RX placeholders here; bounded transport additions
        # below replace them with the actual prefixed transport drivers.
        connection.execute(
            """WITH resolved AS (
              SELECT n.original_net,e.role,e.synthetic,e.ordinal,pn.name AS port,
                     e.bit,x.value AS extras_json,
                     CASE
                       WHEN e.synthetic=1 THEN e.synthetic_instance_id
                       WHEN e.source_instance_seq IS NULL THEN NULL
                       ELSE COALESCE(c.copy_id,i.original_id)
                     END AS instance_id
              FROM split.endpoint_segments e
              JOIN split.source_nets n
                ON n.source_net_seq=e.source_net_seq
              JOIN split.pin_names pn ON pn.seq=e.port_seq
              JOIN split.payloads x ON x.seq=e.extras_seq
              LEFT JOIN split.instance_copies c
                ON c.part_index=e.part_index
               AND c.source_instance_seq=e.source_instance_seq
              LEFT JOIN split.source_instances i
                ON i.source_instance_seq=e.source_instance_seq
              WHERE e.part_index=?
            )
            INSERT INTO stage_endpoints
            SELECT original_net,role,0,synthetic,
                   CASE WHEN instance_id IS NULL THEN 1 ELSE 0 END,
                   ordinal,instance_id,port,bit,extras_json
            FROM resolved
            WHERE role<>0 OR instance_id IS NULL
               OR instance_id NOT LIKE '__emuflow_rx_%'""",
            (part_index,),
        )

        connection.executemany(
            "INSERT OR IGNORE INTO requested_nets VALUES (?,?)",
            (
                [(0, net_id) for net_id in sorted(set(expected_source_nets))]
                + [
                    (1, net_id)
                    for net_id, _instance_id
                    in sorted(set(expected_rx_bindings))
                ]
            ),
        )
        connection.executemany(
            "INSERT OR IGNORE INTO expected_rx VALUES (?,?)",
            sorted(set(expected_rx_bindings)),
        )
        missing = connection.execute(
            """SELECT r.kind,r.net_id FROM requested_nets r
            LEFT JOIN stage_nets n ON n.id=r.net_id
            WHERE n.id IS NULL ORDER BY r.kind,r.net_id LIMIT 1"""
        ).fetchone()
        if missing is not None:
            kind = "source" if int(missing[0]) == 0 else "RX"
            raise ValidationError(
                f"transport {kind} net {missing[1]!r} is not local"
            )
        rx_mismatch = connection.execute(
            """WITH actual AS (
              SELECT n.original_net AS net_id,e.synthetic_instance_id AS instance_id
              FROM split.endpoint_segments e
              JOIN split.source_nets n
                ON n.source_net_seq=e.source_net_seq
              WHERE e.part_index=? AND e.role=0 AND e.synthetic=1
                AND e.synthetic_instance_id LIKE '__emuflow_rx_%'
            ), mismatch AS (
              SELECT 'unexpected' AS kind,a.net_id,a.instance_id FROM actual a
              LEFT JOIN expected_rx x
                ON x.net_id=a.net_id AND x.instance_id=a.instance_id
              WHERE x.net_id IS NULL
              UNION ALL
              SELECT 'missing',x.net_id,x.instance_id FROM expected_rx x
              LEFT JOIN actual a
                ON a.net_id=x.net_id AND a.instance_id=x.instance_id
              WHERE a.net_id IS NULL
            ) SELECT kind,net_id,instance_id FROM mismatch LIMIT 1""",
            (part_index,),
        ).fetchone()
        if rx_mismatch is not None:
            raise ValidationError(
                "transport RX placeholder coverage is incomplete: "
                f"{rx_mismatch[0]} {rx_mismatch[2]!r} on "
                f"{rx_mismatch[1]!r}"
            )

        connection.executemany(
            "INSERT INTO stage_endpoints VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                _endpoint_row(net, role, 1, 0, 0, order, endpoint)
                for net, role, order, endpoint in added_endpoints
            ),
        )
        connection.executemany(
            "INSERT INTO port_additions VALUES (?,?,?,?,?,?,?)",
            (
                (
                    driver_port,
                    driver_bit,
                    order,
                    endpoint.get("instance"),
                    endpoint["port"],
                    int(endpoint["bit"]),
                    _extras(endpoint, {"instance", "port", "bit"}),
                )
                for driver_port, driver_bit, order, endpoint
                in port_added_endpoints
            ),
        )
        connection.execute(
            """INSERT INTO stage_endpoints
            SELECT n.original_net,1,1,1,0,a.overlay_order,
                   a.instance_id,a.port,a.bit,a.extras_json
            FROM port_additions a
            JOIN split.pin_names pn ON pn.name=a.driver_port
            JOIN split.endpoint_segments e
              ON e.part_index=? AND e.role=0 AND e.source_instance_seq IS NULL
             AND e.port_seq=pn.seq AND e.bit=a.driver_bit
            JOIN split.source_nets n
              ON n.source_net_seq=e.source_net_seq""",
            (part_index,),
        )
        connection.execute(
            """INSERT OR IGNORE INTO fanout_recompute(net_id)
            SELECT DISTINCT n.original_net
            FROM port_additions a
            JOIN split.pin_names pn ON pn.name=a.driver_port
            JOIN split.endpoint_segments e
              ON e.part_index=? AND e.role=0 AND e.source_instance_seq IS NULL
             AND e.port_seq=pn.seq AND e.bit=a.driver_bit
            JOIN split.source_nets n
              ON n.source_net_seq=e.source_net_seq""",
            (part_index,),
        )
        unmatched_port = connection.execute(
            """SELECT a.driver_port,a.driver_bit,count(e.source_net_seq)
            FROM port_additions a
            LEFT JOIN split.pin_names pn ON pn.name=a.driver_port
            LEFT JOIN split.endpoint_segments e
              ON e.part_index=? AND e.role=0 AND e.source_instance_seq IS NULL
             AND e.port_seq=pn.seq AND e.bit=a.driver_bit
            GROUP BY a.driver_port,a.driver_bit
            HAVING count(e.source_net_seq)<>1 LIMIT 1""",
            (part_index,),
        ).fetchone()
        if unmatched_port is not None:
            raise ValidationError(
                "shared transport input does not bind one DUT net"
            )

        connection.executemany(
            "INSERT OR IGNORE INTO fanout_recompute VALUES (?)",
            ((net_id,) for net_id in sorted(set(recompute_fanout))),
        )

        for net in transport_nets:
            connection.execute(
                "INSERT INTO stage_nets VALUES (?,?,?,?,?,?,?)",
                (
                    net["id"],
                    net.get("name"),
                    net.get("yosys_bit"),
                    net.get("bus_index"),
                    int(net.get("fanout", len(net["sinks"]))),
                    net["cut_class"],
                    _extras(
                        net,
                        {
                            "id",
                            "name",
                            "yosys_bit",
                            "bus_index",
                            "fanout",
                            "cut_class",
                            "aliases",
                            "drivers",
                            "sinks",
                        },
                    ),
                ),
            )
            for ordinal, alias in enumerate(net.get("aliases", [])):
                connection.execute(
                    "INSERT INTO stage_aliases VALUES (?,?,?)",
                    (net["id"], ordinal, alias),
                )
            for role, collection in enumerate(("drivers", "sinks")):
                connection.executemany(
                    "INSERT INTO stage_endpoints VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        _endpoint_row(
                            net["id"], role, 0, 0, 0, ordinal, endpoint
                        )
                        for ordinal, endpoint in enumerate(net[collection])
                    ),
                )

        connection.execute(
            """UPDATE stage_nets SET fanout=(
              SELECT count(*) FROM stage_endpoints e
              WHERE e.net_id=stage_nets.id AND e.role=1
            ) WHERE id IN (SELECT net_id FROM fanout_recompute)"""
        )

        missing_owner = connection.execute(
            """SELECT e.net_id,e.instance_id FROM stage_endpoints e
            LEFT JOIN stage_cells c ON c.id=e.instance_id
            WHERE e.instance_id IS NOT NULL AND c.id IS NULL
            LIMIT 1"""
        ).fetchone()
        if missing_owner is not None:
            raise ValidationError(
                f"net {missing_owner[0]!r} references missing instance "
                f"{missing_owner[1]!r}"
            )
        missing_net = connection.execute(
            """SELECT e.net_id FROM stage_endpoints e
            LEFT JOIN stage_nets n ON n.id=e.net_id
            WHERE n.id IS NULL LIMIT 1"""
        ).fetchone()
        if missing_net is not None:
            raise ValidationError(
                f"endpoint references missing net {missing_net[0]!r}"
            )

        # Intern each payload once, then materialize canonically ordered compact
        # records.  All graph-sized work below remains inside SQLite.
        connection.executescript(
            """
            INSERT OR IGNORE INTO payloads(value)
              SELECT resources_json FROM stage_cells;
            INSERT OR IGNORE INTO payloads(value)
              SELECT parameters_json FROM stage_cells
              WHERE parameters_json IS NOT NULL;
            INSERT OR IGNORE INTO payloads(value)
              SELECT attributes_json FROM stage_cells
              WHERE attributes_json IS NOT NULL;
            INSERT OR IGNORE INTO payloads(value)
              SELECT constants_json FROM stage_cells;
            INSERT OR IGNORE INTO payloads(value)
              SELECT extras_json FROM stage_cells;
            INSERT OR IGNORE INTO payloads(value)
              SELECT extras_json FROM stage_nets;
            INSERT OR IGNORE INTO payloads(value)
              SELECT extras_json FROM stage_endpoints;

            INSERT INTO cell_records(
              id,name,flags,type,resources_seq,parameters_seq,attributes_seq,
              constants_seq,extras_seq
            )
            SELECT c.id,
                   CASE WHEN c.name=c.id THEN NULL ELSE c.name END,
                   CASE WHEN c.name=c.id THEN 1 ELSE 0 END,
                   c.type,r.seq,p.seq,a.seq,k.seq,x.seq
            FROM stage_cells c
            JOIN payloads r ON r.value=c.resources_json
            LEFT JOIN payloads p ON p.value=c.parameters_json
            LEFT JOIN payloads a ON a.value=c.attributes_json
            JOIN payloads k ON k.value=c.constants_json
            JOIN payloads x ON x.value=c.extras_json
            ORDER BY c.id;

            INSERT INTO net_records(
              id,name,flags,yosys_bit,bus_index,fanout,cut_class,extras_seq
            )
            SELECT n.id,
                   CASE WHEN n.name=n.id THEN NULL ELSE n.name END,
                   CASE WHEN n.name=n.id THEN 1 ELSE 0 END,
                   n.yosys_bit,n.bus_index,n.fanout,n.cut_class,x.seq
            FROM stage_nets n JOIN payloads x ON x.value=n.extras_json
            ORDER BY n.id;

            INSERT INTO pin_names(name)
              SELECT DISTINCT port FROM stage_endpoints ORDER BY port;

            INSERT INTO endpoint_records(
              net_seq,role,ordinal,instance_seq,port_seq,bit,extras_seq
            )
            SELECT net.seq,e.role,
                   row_number() OVER (
                     PARTITION BY e.net_id,e.role
                     ORDER BY e.order_group,e.order_a,e.order_b,e.order_c
                   )-1,
                   cell.seq,pin.seq,e.bit,x.seq
            FROM stage_endpoints e
            JOIN net_records net ON net.id=e.net_id
            LEFT JOIN cell_records cell ON cell.id=e.instance_id
            JOIN pin_names pin ON pin.name=e.port
            JOIN payloads x ON x.value=e.extras_json
            ORDER BY net.seq,e.role,e.order_group,e.order_a,e.order_b,e.order_c;

            INSERT INTO alias_records(net_seq,ordinal,alias)
            SELECT n.seq,a.ordinal,a.alias FROM stage_aliases a
            JOIN net_records n ON n.id=a.net_id
            ORDER BY n.seq,a.ordinal;
            """
        )

        normalized_ports = [
            {
                **record,
                "constant_connections": record.get(
                    "constant_connections", []
                ),
            }
            for record in ports
        ]
        connection.executemany(
            "INSERT INTO port_records(record_json) VALUES (?)",
            ((_json(record),) for record in normalized_ports),
        )
        connection.executemany(
            "INSERT INTO clocks(record_json) VALUES (?)",
            ((_json(record),) for record in clocks),
        )
        counts = {
            "cell_records": int(
                connection.execute(
                    "SELECT count(*) FROM cell_records"
                ).fetchone()[0]
            ),
            "net_records": int(
                connection.execute(
                    "SELECT count(*) FROM net_records"
                ).fetchone()[0]
            ),
            "endpoint_records": int(
                connection.execute(
                    "SELECT count(*) FROM endpoint_records"
                ).fetchone()[0]
            ),
        }
        connection.executemany(
            "INSERT INTO metadata VALUES (?,?)",
            sorted(
                {
                    "storage_schema": EMUIR_COMPACT_STORAGE_SCHEMA,
                    "logical_schema": "emuflow.emuir/v1",
                    "design_json": _json(design),
                    "warnings_json": _json(list(warnings)),
                    "record_counts_json": _json(counts),
                }.items()
            ),
        )
        connection.execute(
            "CREATE INDEX endpoint_owner "
            "ON endpoint_records(instance_seq,port_seq,bit)"
        )
        connection.commit()
        connection.close()
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, output_path)
    except BaseException:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise
