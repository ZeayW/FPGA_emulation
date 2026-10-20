"""Check output-copy timing coverage against actual Phase 6 net bindings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import ValidationError
from .split_netlist_storage import open_split_netlists_sqlite


def validate_split_output_realizations(
    registry: Mapping[str, Any],
    split_database: Path,
    original_paths: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Require every original output path to cover every connected split copy.

    The expected output and terminal net come from the original TimingPathDB;
    owners come from the completed split database, independently of projection.
    Port declarations alone do not prove that a particular bit is connected.
    """
    if not isinstance(registry, Mapping) or registry.get("schema") != "emuflow.output-realizations/v1":
        raise ValidationError("split output realization registry is invalid")
    records = registry.get("realizations")
    if not isinstance(records, list):
        raise ValidationError("split output realizations are invalid")
    by_path: dict[str, list[Mapping[str, Any]]] = {}
    ids = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise ValidationError("split output realization record is invalid")
        identity, original = record.get("id"), record.get("original_path")
        if (not isinstance(identity, str) or not identity or identity in ids
                or not isinstance(original, str) or not original):
            raise ValidationError("split output realization identity is invalid")
        ids.add(identity)
        by_path.setdefault(original, []).append(record)

    connection = open_split_netlists_sqlite(split_database)
    cache: dict[tuple[str, str, int], set[str]] = {}
    original_ids = set()
    checked_outputs, checked_copies = 0, 0
    try:
        for path in original_paths:
            identity = path.get("id")
            if not isinstance(identity, str) or not identity or identity in original_ids:
                raise ValidationError("split output coverage original path identity is invalid")
            original_ids.add(identity)
            endpoint = path.get("endpoint")
            if not isinstance(endpoint, Mapping) or "instance" not in endpoint:
                raise ValidationError("split output coverage requires structured original path endpoints")
            if endpoint["instance"] is not None:
                if identity in by_path:
                    raise ValidationError("split output registry references a non-output path")
                continue
            nets, port, bit = path.get("path_nets"), endpoint.get("port"), endpoint.get("bit")
            if (not isinstance(nets, list) or not nets or not isinstance(nets[-1], str)
                    or not isinstance(port, str) or not port or isinstance(bit, bool)
                    or not isinstance(bit, int) or bit < 0):
                raise ValidationError("split output original terminal binding is invalid")
            terminal = nets[-1]
            key = (terminal, port, bit)
            if key not in cache:
                owners = set()
                rows = connection.execute(
                    """SELECT p.fpga,port.record_json
                    FROM source_nets n
                    JOIN net_segments s ON s.source_net_seq=n.source_net_seq
                    JOIN parts p ON p.part_index=s.part_index
                    JOIN endpoint_segments e
                      ON e.source_net_seq=s.source_net_seq AND e.part_index=s.part_index
                    JOIN pin_names pin ON pin.seq=e.port_seq
                    LEFT JOIN ports port ON port.part_index=e.part_index AND port.id=pin.name
                    WHERE n.original_net=? AND pin.name=? AND e.bit=?
                      AND e.role=1 AND e.synthetic=0
                      AND e.source_instance_seq IS NULL AND e.synthetic_instance_id IS NULL""",
                    key,
                )
                for row in rows:
                    declaration = json.loads(row["record_json"]) if row["record_json"] is not None else {}
                    width = declaration.get("width")
                    if (declaration.get("direction") != "output" or isinstance(width, bool)
                            or not isinstance(width, int) or not 0 <= bit < width
                            or row["fpga"] in owners):
                        raise ValidationError("split output copy lacks a unique declared output binding")
                    owners.add(row["fpga"])
                cache[key] = owners
            expected = cache[key]
            actual = set()
            for record in by_path.pop(identity, []):
                owner = record.get("fpga")
                if (record.get("terminal_net") != terminal
                        or record.get("endpoint") != {"port": port, "bit": bit}
                        or not isinstance(owner, str) or not owner or owner in actual):
                    raise ValidationError("split output realization disagrees with its original path binding")
                actual.add(owner)
            if not expected or actual != expected:
                raise ValidationError(f"split output realization coverage differs from actual copies: {identity}")
            checked_outputs += 1
            checked_copies += len(expected)
    finally:
        connection.close()
    if by_path:
        raise ValidationError("split output registry contains unknown original paths")
    return {"status": "pass", "original_output_paths": checked_outputs,
            "output_realizations": checked_copies, "terminal_output_bindings": len(cache)}
