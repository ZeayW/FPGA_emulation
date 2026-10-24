"""Deterministic buffer trees for synthesized transport-only high-fanout nets."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

from .emuir_storage import (
    EMUIR_COMPACT_STORAGE_SCHEMA,
    EMUIR_SQLITE_APPLICATION_ID,
    open_emuir_sqlite_read_only,
)
from .errors import ValidationError
from .ir import EmuIR
from .resources import ResourceVector


TRANSPORT_FANOUT_BUFFERING_SCHEMA = "emuflow.transport-fanout-buffering/v1"
DEFAULT_TRANSPORT_MAX_FANOUT = 512


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _intern_sqlite_value(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    value: str,
) -> int:
    row = connection.execute(
        f"SELECT seq FROM {table} WHERE {column}=?", (value,)
    ).fetchone()
    if row is not None:
        return int(row[0])
    return int(
        connection.execute(
            f"INSERT INTO {table}({column}) VALUES (?)", (value,)
        ).lastrowid
    )


def _endpoint_key(endpoint: Mapping[str, Any]) -> Tuple[str, str, int]:
    instance = endpoint.get("instance")
    return (
        "" if instance is None else str(instance),
        str(endpoint.get("port")),
        int(endpoint.get("bit", 0)),
    )


def _buffer_instance(name: str, *, owner: str, level: int) -> Dict[str, Any]:
    return {
        "id": name,
        "name": name,
        "type": "$lut",
        "resources": ResourceVector(lut=1).to_dict(),
        "parameters": {
            "LUT": "10",
            "WIDTH": "00000000000000000000000000000001",
        },
        "attributes": {
            "emuflow_high_fanout_buffer": "transport-balanced-tree-v1",
            "emuflow_high_fanout_owner": owner,
            "emuflow_high_fanout_level": str(level),
        },
        "constant_connections": [],
    }


def buffer_transport_high_fanout_nets(
    transport_ir: EmuIR,
    *,
    max_fanout: int = DEFAULT_TRANSPORT_MAX_FANOUT,
) -> Dict[str, Any]:
    """Insert balanced LUT1 trees into an owned transport EmuIR.

    The transform runs before packing and placement, so the placer sees every
    buffer and can co-locate each branch with its sink group.  It is limited to
    the separately synthesized transport design; original DUT topology and
    timing-path identity remain unchanged.  Clock and reset nets keep their
    dedicated handling.

    The input object is mutated deliberately.  Transport synthesis already
    owns this graph, and avoiding a second graph-sized copy keeps Phase 7's
    memory bounded.
    """

    if isinstance(max_fanout, bool) or not isinstance(max_fanout, int) or max_fanout < 2:
        raise ValidationError("transport fanout maximum must be an integer >= 2")

    value = transport_ir.value
    instances = value["instances"]
    nets = value["nets"]
    occupied_instances = {str(instance["id"]) for instance in instances}
    occupied_nets = {str(net["id"]) for net in nets}
    candidates = [
        (index, net)
        for index, net in enumerate(nets)
        if len(net["sinks"]) > max_fanout
        and net.get("cut_class") not in {"clock", "reset"}
        and len(net["drivers"]) == 1
    ]

    added_instances = []
    added_nets = []
    records = []
    for candidate_index, (net_index, net) in enumerate(candidates):
        original_sinks = sorted(
            (dict(endpoint) for endpoint in net["sinks"]),
            key=_endpoint_key,
        )
        owner = str(net["id"])
        downstream_sinks = original_sinks
        level = 0
        buffer_count = 0
        level_widths = []
        candidate_net_start = len(added_nets)
        while len(downstream_sinks) > max_fanout:
            upstream_sinks = []
            width = 0
            for group_index, offset in enumerate(
                range(0, len(downstream_sinks), max_fanout)
            ):
                group = downstream_sinks[offset : offset + max_fanout]
                stem = (
                    f"$emuflow_transport_fanout${candidate_index:06d}"
                    f"$l{level:02d}$g{group_index:06d}"
                )
                cell_id = stem + "$buf"
                output_net_id = stem + "$net"
                if cell_id in occupied_instances or output_net_id in occupied_nets:
                    raise ValidationError(
                        f"transport fanout buffer identifier collision for {owner!r}"
                    )
                occupied_instances.add(cell_id)
                occupied_nets.add(output_net_id)
                added_instances.append(
                    _buffer_instance(cell_id, owner=owner, level=level)
                )
                added_nets.append(
                    {
                        "id": output_net_id,
                        "name": output_net_id,
                        "yosys_bit": None,
                        "bus_index": 0,
                        "aliases": [],
                        "drivers": [
                            {"instance": cell_id, "port": "Y", "bit": 0}
                        ],
                        "sinks": group,
                        "fanout": len(group),
                        "cut_class": "combinational",
                        "fanout_buffer_owner": owner,
                    }
                )
                upstream_sinks.append(
                    {"instance": cell_id, "port": "A", "bit": 0}
                )
                buffer_count += 1
                width += 1
            level_widths.append(width)
            downstream_sinks = upstream_sinks
            level += 1

        net["sinks"] = downstream_sinks
        net["fanout"] = len(downstream_sinks)
        # The first level alone owns the original sinks and is the exact
        # equivalence boundary used for the audit.
        leaf_width = level_widths[0]
        leaf_nets = added_nets[
            candidate_net_start : candidate_net_start + leaf_width
        ]
        reconstructed = sorted(
            (endpoint for branch in leaf_nets for endpoint in branch["sinks"]),
            key=_endpoint_key,
        )
        if reconstructed != original_sinks:
            raise ValidationError(
                f"transport fanout buffering changed endpoints of {owner!r}"
            )
        records.append(
            {
                "net": owner,
                "net_index": net_index,
                "original_fanout": len(original_sinks),
                "root_fanout": len(downstream_sinks),
                "levels": level,
                "level_buffer_counts": level_widths,
                "buffer_cells": buffer_count,
            }
        )

    instances.extend(added_instances)
    nets.extend(added_nets)
    transport_ir.validate()

    return {
        "schema": TRANSPORT_FANOUT_BUFFERING_SCHEMA,
        "status": "pass",
        "policy": {
            "scope": "synthesized-transport-only",
            "topology": "balanced-lut1-tree",
            "maximum_fanout": max_fanout,
            "excluded_cut_classes": ["clock", "reset"],
            "placement": "before-pack-and-place",
        },
        "summary": {
            "buffered_nets": len(records),
            "buffer_cells": len(added_instances),
            "added_nets": len(added_nets),
            "maximum_original_fanout": max(
                (record["original_fanout"] for record in records), default=0
            ),
            "maximum_result_fanout": max(
                (
                    len(net["sinks"])
                    for net in nets
                    if net.get("cut_class") not in {"clock", "reset"}
                ),
                default=0,
            ),
        },
        "nets": records,
    }


def buffer_transport_high_fanout_store(
    path: Path,
    *,
    max_fanout: int = DEFAULT_TRANSPORT_MAX_FANOUT,
) -> Dict[str, Any]:
    """Insert transport buffer trees without materializing the EmuIR graph.

    The indexed Yosys importer has already closed ``path`` when this transform
    runs.  Work on an adjacent staging copy and atomically replace the source
    only after the compact-store invariants pass, so an interrupted transform
    leaves the imported artifact intact.  At most one candidate net's sink
    rows are held in memory.
    """

    if isinstance(max_fanout, bool) or not isinstance(max_fanout, int) or max_fanout < 2:
        raise ValidationError("transport fanout maximum must be an integer >= 2")

    path = Path(path)
    validation = open_emuir_sqlite_read_only(path)
    try:
        version = int(validation.execute("PRAGMA user_version").fetchone()[0])
        storage_row = validation.execute(
            "SELECT value FROM metadata WHERE key='storage_schema'"
        ).fetchone()
        if version != 2 or storage_row is None or storage_row[0] != EMUIR_COMPACT_STORAGE_SCHEMA:
            raise ValidationError(
                "streaming transport fanout buffering requires compact EmuIR v2"
            )
        candidates = [
            dict(row)
            for row in validation.execute(
                """SELECT n.seq,n.id,
                sum(CASE WHEN e.role=0 THEN 1 ELSE 0 END) AS drivers,
                sum(CASE WHEN e.role=1 THEN 1 ELSE 0 END) AS sinks
                FROM net_records n JOIN endpoint_records e ON e.net_seq=n.seq
                WHERE n.cut_class NOT IN ('clock','reset')
                GROUP BY n.seq,n.id
                HAVING drivers=1 AND sinks>?
                ORDER BY n.seq""",
                (max_fanout,),
            )
        ]
        original_maximum = int(
            validation.execute(
                "SELECT coalesce(max(fanout),0) FROM net_records "
                "WHERE cut_class NOT IN ('clock','reset')"
            ).fetchone()[0]
        )
    finally:
        validation.close()

    if not candidates:
        return {
            "schema": TRANSPORT_FANOUT_BUFFERING_SCHEMA,
            "status": "pass",
            "policy": {
                "scope": "synthesized-transport-only",
                "topology": "balanced-lut1-tree",
                "maximum_fanout": max_fanout,
                "excluded_cut_classes": ["clock", "reset"],
                "placement": "before-pack-and-place",
                "storage": "compact-emuir-streaming",
            },
            "summary": {
                "buffered_nets": 0,
                "buffer_cells": 0,
                "added_nets": 0,
                "maximum_original_fanout": 0,
                "maximum_result_fanout": original_maximum,
            },
            "nets": [],
        }

    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.fanout.tmp")
    connection: sqlite3.Connection | None = None
    try:
        shutil.copyfile(path, temporary)
        connection = sqlite3.connect(temporary)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute("PRAGMA cache_size=-65536")
        if int(connection.execute("PRAGMA application_id").fetchone()[0]) != EMUIR_SQLITE_APPLICATION_ID:
            raise ValidationError("transport EmuIR SQLite header changed during buffering")

        next_cell_seq = int(
            connection.execute(
                "SELECT coalesce(max(seq),-1)+1 FROM cell_records"
            ).fetchone()[0]
        )
        next_net_seq = int(
            connection.execute(
                "SELECT coalesce(max(seq),-1)+1 FROM net_records"
            ).fetchone()[0]
        )
        connection.execute("BEGIN IMMEDIATE")
        resources_seq = _intern_sqlite_value(
            connection,
            "payloads",
            "value",
            _canonical_json(ResourceVector(lut=1).to_dict()),
        )
        parameters_seq = _intern_sqlite_value(
            connection,
            "payloads",
            "value",
            _canonical_json(
                {
                    "LUT": "10",
                    "WIDTH": "00000000000000000000000000000001",
                }
            ),
        )
        constants_seq = _intern_sqlite_value(
            connection, "payloads", "value", "[]"
        )
        empty_extras_seq = _intern_sqlite_value(
            connection, "payloads", "value", "{}"
        )
        input_port_seq = _intern_sqlite_value(
            connection, "pin_names", "name", "A"
        )
        output_port_seq = _intern_sqlite_value(
            connection, "pin_names", "name", "Y"
        )

        records = []
        added_cells = 0
        added_nets = 0
        for candidate_index, candidate in enumerate(candidates):
            net_seq = int(candidate["seq"])
            owner = str(candidate["id"])
            original_sinks = [
                (
                    row["instance_seq"],
                    int(row["port_seq"]),
                    int(row["bit"]),
                    int(row["extras_seq"]),
                )
                for row in connection.execute(
                    """SELECT e.instance_seq,e.port_seq,e.bit,e.extras_seq
                    FROM endpoint_records e
                    LEFT JOIN cell_records c ON c.seq=e.instance_seq
                    JOIN pin_names p ON p.seq=e.port_seq
                    WHERE e.net_seq=? AND e.role=1
                    ORDER BY coalesce(c.id,''),p.name,e.bit,e.ordinal""",
                    (net_seq,),
                )
            ]
            if len(original_sinks) != int(candidate["sinks"]):
                raise ValidationError(
                    f"transport fanout changed while buffering {owner!r}"
                )
            connection.execute(
                "DELETE FROM endpoint_records WHERE net_seq=? AND role=1",
                (net_seq,),
            )

            downstream_sinks = original_sinks
            level = 0
            buffer_count = 0
            level_widths = []
            leaf_net_seqs = []
            while len(downstream_sinks) > max_fanout:
                upstream_sinks = []
                width = 0
                for group_index, offset in enumerate(
                    range(0, len(downstream_sinks), max_fanout)
                ):
                    group = downstream_sinks[offset : offset + max_fanout]
                    stem = (
                        f"$emuflow_transport_fanout${candidate_index:06d}"
                        f"$l{level:02d}$g{group_index:06d}"
                    )
                    cell_id = stem + "$buf"
                    output_net_id = stem + "$net"
                    if connection.execute(
                        "SELECT 1 FROM cell_records WHERE id=?", (cell_id,)
                    ).fetchone() or connection.execute(
                        "SELECT 1 FROM net_records WHERE id=?", (output_net_id,)
                    ).fetchone():
                        raise ValidationError(
                            f"transport fanout buffer identifier collision for {owner!r}"
                        )
                    attributes_seq = _intern_sqlite_value(
                        connection,
                        "payloads",
                        "value",
                        _canonical_json(
                            {
                                "emuflow_high_fanout_buffer": "transport-balanced-tree-v1",
                                "emuflow_high_fanout_owner": owner,
                                "emuflow_high_fanout_level": str(level),
                            }
                        ),
                    )
                    connection.execute(
                        "INSERT INTO cell_records VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            next_cell_seq,
                            cell_id,
                            None,
                            1,
                            "$lut",
                            resources_seq,
                            parameters_seq,
                            attributes_seq,
                            constants_seq,
                            empty_extras_seq,
                        ),
                    )
                    net_extras_seq = _intern_sqlite_value(
                        connection,
                        "payloads",
                        "value",
                        _canonical_json({"fanout_buffer_owner": owner}),
                    )
                    connection.execute(
                        "INSERT INTO net_records VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            next_net_seq,
                            output_net_id,
                            None,
                            1,
                            None,
                            0,
                            len(group),
                            "combinational",
                            net_extras_seq,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO endpoint_records VALUES (?,?,?,?,?,?,?)",
                        (
                            next_net_seq,
                            0,
                            0,
                            next_cell_seq,
                            output_port_seq,
                            0,
                            empty_extras_seq,
                        ),
                    )
                    connection.executemany(
                        "INSERT INTO endpoint_records VALUES (?,?,?,?,?,?,?)",
                        (
                            (
                                next_net_seq,
                                1,
                                ordinal,
                                sink[0],
                                sink[1],
                                sink[2],
                                sink[3],
                            )
                            for ordinal, sink in enumerate(group)
                        ),
                    )
                    if level == 0:
                        leaf_net_seqs.append(next_net_seq)
                    upstream_sinks.append(
                        (next_cell_seq, input_port_seq, 0, empty_extras_seq)
                    )
                    next_cell_seq += 1
                    next_net_seq += 1
                    added_cells += 1
                    added_nets += 1
                    buffer_count += 1
                    width += 1
                level_widths.append(width)
                downstream_sinks = upstream_sinks
                level += 1

            connection.executemany(
                "INSERT INTO endpoint_records VALUES (?,?,?,?,?,?,?)",
                (
                    (net_seq, 1, ordinal, sink[0], sink[1], sink[2], sink[3])
                    for ordinal, sink in enumerate(downstream_sinks)
                ),
            )
            connection.execute(
                "UPDATE net_records SET fanout=? WHERE seq=?",
                (len(downstream_sinks), net_seq),
            )
            leaf_sinks = int(
                connection.execute(
                    "SELECT count(*) FROM endpoint_records "
                    f"WHERE role=1 AND net_seq IN ({','.join('?' for _ in leaf_net_seqs)})",
                    leaf_net_seqs,
                ).fetchone()[0]
            )
            if leaf_sinks != len(original_sinks):
                raise ValidationError(
                    f"transport fanout buffering changed endpoints of {owner!r}"
                )
            records.append(
                {
                    "net": owner,
                    "net_index": net_seq,
                    "original_fanout": len(original_sinks),
                    "root_fanout": len(downstream_sinks),
                    "levels": level,
                    "level_buffer_counts": level_widths,
                    "buffer_cells": buffer_count,
                }
            )

        counts = {
            "cell_records": int(
                connection.execute("SELECT count(*) FROM cell_records").fetchone()[0]
            ),
            "net_records": int(
                connection.execute("SELECT count(*) FROM net_records").fetchone()[0]
            ),
            "endpoint_records": int(
                connection.execute("SELECT count(*) FROM endpoint_records").fetchone()[0]
            ),
        }
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='record_counts_json'",
            (_canonical_json(counts),),
        )
        maximum_result_fanout = int(
            connection.execute(
                "SELECT coalesce(max(fanout),0) FROM net_records "
                "WHERE cut_class NOT IN ('clock','reset')"
            ).fetchone()[0]
        )
        orphan = connection.execute(
            """SELECT 1 FROM endpoint_records e LEFT JOIN cell_records c
            ON c.seq=e.instance_seq WHERE e.instance_seq IS NOT NULL
            AND c.seq IS NULL LIMIT 1"""
        ).fetchone()
        if orphan is not None:
            raise ValidationError("transport fanout buffering created an orphan endpoint")
        connection.commit()
        connection.close()
        connection = None
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)

        return {
            "schema": TRANSPORT_FANOUT_BUFFERING_SCHEMA,
            "status": "pass",
            "policy": {
                "scope": "synthesized-transport-only",
                "topology": "balanced-lut1-tree",
                "maximum_fanout": max_fanout,
                "excluded_cut_classes": ["clock", "reset"],
                "placement": "before-pack-and-place",
                "storage": "compact-emuir-streaming",
            },
            "summary": {
                "buffered_nets": len(records),
                "buffer_cells": added_cells,
                "added_nets": added_nets,
                "maximum_original_fanout": max(
                    (record["original_fanout"] for record in records), default=0
                ),
                "maximum_result_fanout": maximum_result_fanout,
            },
            "nets": records,
        }
    except BaseException:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)
        raise
