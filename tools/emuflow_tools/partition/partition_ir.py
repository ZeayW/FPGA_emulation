"""Bounded-memory EmuIR view for Phase 3 partitioning.

The partitioner needs cell identity/type/resources and net connectivity.  It
does not need the synthesis parameters, attributes, aliases, names, constants,
or other payloads carried by the complete logical EmuIR.  Reconstructing those
records for a multi-million-cell design inflated a compact SQLite artifact by
almost an order of magnitude in Python before partitioning began.

This module exposes the existing ``ir.value`` read contract with reusable,
streamed sequences backed by the compact EmuIR tables.  Algorithms may scan
the graph more than once, but no scan retains decoded rows unless the owning
algorithm explicitly builds a compact index from them.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from emuflow.ir.emuir_compact import is_compact_emuir
from emuflow.ir.emuir_storage import _decode, is_emuir_sqlite, open_emuir_sqlite_read_only
from emuflow.ir.ir import EmuIR


class _CompactPartitionInstances(Sequence[dict[str, Any]]):
    def __init__(self, path: Path, count: int):
        self._path = path
        self._count = count

    def __len__(self) -> int:
        return self._count

    def __getitem__(self, index: int) -> dict[str, Any]:
        if not isinstance(index, int):
            raise TypeError("partition instance view supports integer indexing")
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        connection = open_emuir_sqlite_read_only(self._path)
        try:
            row = connection.execute(
                """SELECT c.id,c.type,p.value AS resources_json
                   FROM cell_records c JOIN payloads p ON p.seq=c.resources_seq
                  WHERE c.seq=?""",
                (index,),
            ).fetchone()
            if row is None:
                raise ValueError("compact EmuIR instance sequence is incomplete")
            return {
                "id": row["id"],
                "type": row["type"],
                "resources": _decode(row["resources_json"], "resources"),
            }
        finally:
            connection.close()

    def __iter__(self) -> Iterator[dict[str, Any]]:
        connection = open_emuir_sqlite_read_only(self._path)
        try:
            for row in connection.execute(
                """SELECT c.id,c.type,p.value AS resources_json
                   FROM cell_records c JOIN payloads p ON p.seq=c.resources_seq
                  ORDER BY c.seq"""
            ):
                yield {
                    "id": row["id"],
                    "type": row["type"],
                    "resources": _decode(row["resources_json"], "resources"),
                }
        finally:
            connection.close()


class _CompactPartitionNets(Sequence[dict[str, Any]]):
    def __init__(self, path: Path, count: int):
        self._path = path
        self._count = count

    def __len__(self) -> int:
        return self._count

    def __getitem__(self, index: int) -> dict[str, Any]:
        if not isinstance(index, int):
            raise TypeError("partition net view supports integer indexing")
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        connection = open_emuir_sqlite_read_only(self._path)
        try:
            row = connection.execute(
                "SELECT seq,id,cut_class FROM net_records WHERE seq=?",
                (index,),
            ).fetchone()
            if row is None:
                raise ValueError("compact EmuIR net sequence is incomplete")
            return self._record(connection, row)
        finally:
            connection.close()

    @staticmethod
    def _endpoint(row: Any) -> dict[str, Any]:
        return {
            "instance": row["instance_id"],
            "port": row["port"],
            "bit": row["bit"],
        }

    @classmethod
    def _record(cls, connection: Any, row: Any) -> dict[str, Any]:
        record = {
            "id": row["id"],
            "cut_class": row["cut_class"],
            "drivers": [],
            "sinks": [],
        }
        for endpoint in connection.execute(
            """SELECT e.role,c.id AS instance_id,p.name AS port,e.bit
                 FROM endpoint_records e
                 LEFT JOIN cell_records c ON c.seq=e.instance_seq
                 JOIN pin_names p ON p.seq=e.port_seq
                WHERE e.net_seq=? ORDER BY e.role,e.ordinal""",
            (row["seq"],),
        ):
            if endpoint["role"] not in (0, 1):
                raise ValueError("compact EmuIR endpoint role is invalid")
            record[("drivers", "sinks")[endpoint["role"]]].append(
                cls._endpoint(endpoint)
            )
        return record

    def __iter__(self) -> Iterator[dict[str, Any]]:
        connection = open_emuir_sqlite_read_only(self._path)
        try:
            endpoint_rows = iter(connection.execute(
                """SELECT e.net_seq,e.role,c.id AS instance_id,
                          p.name AS port,e.bit
                     FROM endpoint_records e
                     LEFT JOIN cell_records c ON c.seq=e.instance_seq
                     JOIN pin_names p ON p.seq=e.port_seq
                    ORDER BY e.net_seq,e.role,e.ordinal"""
            ))
            endpoint = next(endpoint_rows, None)
            for row in connection.execute(
                "SELECT seq,id,cut_class FROM net_records ORDER BY seq"
            ):
                record = {
                    "id": row["id"],
                    "cut_class": row["cut_class"],
                    "drivers": [],
                    "sinks": [],
                }
                while endpoint is not None and endpoint["net_seq"] == row["seq"]:
                    if endpoint["role"] not in (0, 1):
                        raise ValueError("compact EmuIR endpoint role is invalid")
                    record[("drivers", "sinks")[endpoint["role"]]].append(
                        self._endpoint(endpoint)
                    )
                    endpoint = next(endpoint_rows, None)
                if endpoint is not None and endpoint["net_seq"] < row["seq"]:
                    raise ValueError("compact EmuIR has orphaned endpoints")
                yield record
            if endpoint is not None:
                raise ValueError("compact EmuIR has orphaned endpoints")
        finally:
            connection.close()


class CompactPartitionIR:
    """Duck-typed read-only EmuIR containing only partition facts."""

    def __init__(self, path: Path):
        self.path = path
        connection = open_emuir_sqlite_read_only(path)
        try:
            metadata = dict(connection.execute(
                "SELECT key,value FROM metadata WHERE key IN "
                "('logical_schema','design_json','record_counts_json')"
            ))
            counts = json.loads(metadata["record_counts_json"])
            design = json.loads(metadata["design_json"])
        finally:
            connection.close()
        self.value = {
            "schema": metadata["logical_schema"],
            "design": design,
            "instances": _CompactPartitionInstances(
                path, int(counts["cell_records"])
            ),
            "nets": _CompactPartitionNets(path, int(counts["net_records"])),
        }


def load_partition_ir(path: Path) -> EmuIR | CompactPartitionIR:
    """Load a small legacy IR or stream a compact production IR."""

    if is_emuir_sqlite(path) and is_compact_emuir(path):
        return CompactPartitionIR(path)
    return EmuIR.load(path)
