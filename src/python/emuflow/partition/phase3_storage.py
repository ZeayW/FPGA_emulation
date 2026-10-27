from __future__ import annotations

import json
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Dict, Mapping

from emuflow.common.io import file_sha256


INDEXED_CLUSTERS_SCHEMA = "emuflow.phase3-clusters-storage/v3"
PACKED_ASSIGNMENT_SCHEMA = "emuflow.phase3-assignment-storage/v4"
CLUSTERS_SQLITE_APPLICATION_ID = 0x454D5543  # ASCII "EMUC"
CLUSTERS_SQLITE_USER_VERSION = 1
CLUSTERS_SQLITE_FORMAT = "sqlite-phase3-clusters/v1"
CLUSTERS_STORE_NAME = "clusters.sqlite"


_CLUSTER_SECTION_CODEC = "zlib-level-1-typed-columns"
_CLUSTER_SECTIONS = {
    "cluster_ids",
    "member_counts",
    "member_names",
    "resource_counts",
    "resource_names",
    "resource_values",
    "fixed_indices",
    "fixed_values",
    "group_counts",
    "group_values",
    "extra_values",
    "eligible_nets",
    "document",
}


def _cluster_store_path(selector_path: Path, selector: Mapping[str, Any]) -> Path:
    artifact = selector.get("artifact")
    if (
        not isinstance(artifact, str)
        or Path(artifact).name != artifact
        or artifact
        not in {CLUSTERS_STORE_NAME, f"clusters.{selector.get('store_sha256')}.sqlite"}
    ):
        raise ValueError(f"{selector_path}: indexed clusters artifact is invalid")
    return selector_path.with_name(artifact)


def _open_cluster_store(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


def _read_cluster_store_metadata(
    selector_path: Path,
    selector: Mapping[str, Any],
) -> tuple[Path, Dict[str, Any]]:
    if (
        selector.get("schema") != INDEXED_CLUSTERS_SCHEMA
        or selector.get("logical_schema") != "emuflow.clusters/v1"
        or selector.get("format") != CLUSTERS_SQLITE_FORMAT
        or selector.get("application_id") != CLUSTERS_SQLITE_APPLICATION_ID
        or selector.get("user_version") != CLUSTERS_SQLITE_USER_VERSION
        or isinstance(selector.get("store_bytes"), bool)
        or not isinstance(selector.get("store_bytes"), int)
        or selector["store_bytes"] < 0
        or not isinstance(selector.get("store_sha256"), str)
        or len(selector["store_sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in selector["store_sha256"])
        or not isinstance(selector.get("counts"), dict)
    ):
        raise ValueError(f"{selector_path}: indexed clusters header is malformed")
    counts = selector["counts"]
    if set(counts) != {"clusters", "instances", "eligible_nets"} or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in counts.values()
    ):
        raise ValueError(f"{selector_path}: indexed clusters counts are malformed")
    store_path = _cluster_store_path(selector_path, selector)
    try:
        stat = store_path.stat()
    except FileNotFoundError as error:
        raise ValueError(
            f"{selector_path}: indexed clusters artifact is missing"
        ) from error
    if stat.st_size != selector["store_bytes"] or file_sha256(store_path) != selector[
        "store_sha256"
    ]:
        raise ValueError(f"{selector_path}: indexed clusters seal disagrees")
    connection = _open_cluster_store(store_path)
    try:
        if (
            connection.execute("PRAGMA application_id").fetchone()[0]
            != CLUSTERS_SQLITE_APPLICATION_ID
            or connection.execute("PRAGMA user_version").fetchone()[0]
            != CLUSTERS_SQLITE_USER_VERSION
        ):
            raise ValueError(
                f"{selector_path}: indexed clusters SQLite identity is invalid"
            )
        metadata = {
            row["key"]: json.loads(row["value"])
            for row in connection.execute(
                "SELECT key,value FROM metadata ORDER BY key"
            )
        }
        if (
            set(metadata) != {"counts", "eligible_nets_present"}
            or metadata.get("counts") != counts
            or not isinstance(metadata.get("eligible_nets_present"), bool)
            or (
                not metadata["eligible_nets_present"]
                and counts["eligible_nets"] != 0
            )
        ):
            raise ValueError(
                f"{selector_path}: indexed clusters metadata is invalid"
            )
        sections = {
            row["section"]: row
            for row in connection.execute(
                "SELECT section,codec,records,raw_bytes,compressed_bytes,chunks "
                "FROM sections ORDER BY section"
            )
        }
        if set(sections) != _CLUSTER_SECTIONS or any(
            row["codec"] != _CLUSTER_SECTION_CODEC
            or any(
                isinstance(row[field], bool)
                or not isinstance(row[field], int)
                or row[field] < 0
                for field in (
                    "records", "raw_bytes", "compressed_bytes", "chunks"
                )
            )
            or row["chunks"] == 0
            for row in sections.values()
        ):
            raise ValueError(
                f"{selector_path}: indexed cluster sections are invalid"
            )
        expected_records = {
            "cluster_ids": counts["clusters"],
            "member_counts": counts["clusters"],
            "member_names": counts["instances"],
            "resource_counts": counts["clusters"],
            "group_counts": counts["clusters"],
            "extra_values": counts["clusters"],
            "eligible_nets": counts["eligible_nets"],
            "document": 1,
        }
        if any(
            sections[name]["records"] != amount
            for name, amount in expected_records.items()
        ) or (
            sections["fixed_indices"]["records"]
            != sections["fixed_values"]["records"]
        ) or (
            sections["resource_names"]["records"]
            != sections["resource_values"]["records"]
        ):
            raise ValueError(
                f"{selector_path}: indexed cluster section counts disagree"
            )
        for name in ("member_counts", "resource_counts", "fixed_indices", "group_counts"):
            if sections[name]["raw_bytes"] != sections[name]["records"] * 4:
                raise ValueError(
                    f"{selector_path}: indexed cluster integer columns are invalid"
                )
        chunk_summaries = {
            row["section"]: (row["amount"], row["size"])
            for row in connection.execute(
                "SELECT section,count(*) AS amount,sum(length(payload)) AS size "
                "FROM section_chunks GROUP BY section ORDER BY section"
            )
        }
        if set(chunk_summaries) != _CLUSTER_SECTIONS or any(
            chunk_summaries[name]
            != (sections[name]["chunks"], sections[name]["compressed_bytes"])
            for name in _CLUSTER_SECTIONS
        ):
            raise ValueError(
                f"{selector_path}: indexed cluster chunks disagree"
            )
    except (json.JSONDecodeError, sqlite3.DatabaseError) as error:
        raise ValueError(
            f"{selector_path}: indexed clusters SQLite is corrupt"
        ) from error
    finally:
        connection.close()
    return store_path, metadata


def read_phase3_clusters_selector(path: Path, value: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate a source-sealed current selector without expanding clusters."""
    _read_cluster_store_metadata(path, value)
    return dict(value)


class IndexedCutNetRecords(list):
    """Repeatable list-compatible view of the sealed typed cut relation."""

    def __init__(self, path: Path, descriptor: Mapping[str, Any]):
        super().__init__()
        self.path = path.resolve()
        self.descriptor = dict(descriptor)
        if (
            set(self.descriptor)
            != {"artifact", "schema", "records", "sha256"}
            or self.descriptor.get("artifact") != self.path.name
            or self.descriptor.get("schema")
            != "emuflow.phase3-cut-records/v1"
            or isinstance(self.descriptor.get("records"), bool)
            or not isinstance(self.descriptor.get("records"), int)
            or self.descriptor["records"] < 0
            or not isinstance(self.descriptor.get("sha256"), str)
            or len(self.descriptor["sha256"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.descriptor["sha256"]
            )
            or not self.path.is_file()
        ):
            raise ValueError("packed assignment cut-record index is malformed")
        connection = sqlite3.connect(
            f"file:{self.path}?mode=ro&immutable=1", uri=True
        )
        try:
            metadata = {
                str(row[0]): json.loads(row[1])
                for row in connection.execute(
                    "SELECT key,value FROM metadata WHERE key IN "
                    "('schema','cut_records_schema','cut_records',"
                    "'cut_records_sha256')"
                )
            }
        finally:
            connection.close()
        if (
            metadata.get("schema") != "emuflow.phase3-assignment-sqlite/v4"
            or metadata.get("cut_records_schema") != self.descriptor["schema"]
            or metadata.get("cut_records") != self.descriptor["records"]
            or metadata.get("cut_records_sha256") != self.descriptor["sha256"]
        ):
            raise ValueError("packed assignment cut-record identity disagrees")

    def __iter__(self) -> Iterator[dict[str, Any]]:
        from emuflow.partition.phase3_assignment_index import iter_phase3_cut_records

        return iter_phase3_cut_records(self.path, self.descriptor)

    def __len__(self) -> int:
        return int(self.descriptor["records"])

    def __getitem__(self, key: int | slice) -> Any:
        if isinstance(key, slice):
            return list(self)[key]
        if isinstance(key, bool) or not isinstance(key, int):
            raise TypeError("cut-record index must be an integer or slice")
        index = key if key >= 0 else len(self) + key
        if index < 0 or index >= len(self):
            raise IndexError("cut-record index out of range")
        for ordinal, record in enumerate(self):
            if ordinal == index:
                return record
        raise IndexError("cut-record index out of range")

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, list):
            return list(self) == list(other)
        return NotImplemented


class IndexedInstanceAssignment(Mapping[str, str]):
    """Lazy instance-to-FPGA relation backed by assignment.sqlite."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        from emuflow.partition.phase3_assignment_index import open_phase3_assignment_index

        self.connection = open_phase3_assignment_index(
            self.path, check_same_thread=False
        )
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key='source_instance_count'"
        ).fetchone()
        if row is None:
            self.connection.close()
            raise ValueError(
                "assignment index lacks the source instance count"
            )
        try:
            count = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as error:
            self.connection.close()
            raise ValueError(
                "assignment index source instance count is malformed"
            ) from error
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            self.connection.close()
            raise ValueError(
                "assignment index source instance count is malformed"
            )
        self.count = count
        self.cache: OrderedDict[str, str | None] = OrderedDict()
        self.lock = threading.RLock()

    def get(self, key: str, default: Any = None) -> Any:
        if not isinstance(key, str):
            return default
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                value = self.cache[key]
            else:
                row = self.connection.execute(
                    """SELECT p.fpga FROM copies c JOIN parts p
                         ON p.part_index=c.part_index
                        WHERE c.copy_id=? AND c.is_replica=0""",
                    (key,),
                ).fetchone()
                value = None if row is None else str(row[0])
                self.cache[key] = value
                if len(self.cache) > 65536:
                    self.cache.popitem(last=False)
        return default if value is None else value

    def __getitem__(self, key: str) -> str:
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __iter__(self) -> Iterator[str]:
        def rows() -> Iterator[str]:
            connection = sqlite3.connect(
                f"file:{self.path}?mode=ro&immutable=1", uri=True
            )
            try:
                for row in connection.execute(
                    "SELECT copy_id FROM copies WHERE is_replica=0 "
                    "ORDER BY source_instance_seq"
                ):
                    yield str(row[0])
            finally:
                connection.close()

        return rows()

    def __len__(self) -> int:
        # The producer already seals the exact primary-copy count.  Running
        # COUNT(*) over a multi-million-row WITHOUT ROWID table turns a cheap
        # control-plane question into a full 1.5 GiB index scan on NFS.
        return self.count

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    def __del__(self) -> None:
        try:
            self.connection.close()
        except (AttributeError, sqlite3.Error):
            pass


def read_phase3_assignment_view(
    path: Path, *, include_instance_assignment: bool = False,
    include_semantic_contract: bool = False,
) -> Dict[str, Any]:
    """Read current assignment references and lazy typed cut/ownership views."""
    if include_semantic_contract:
        raise ValueError('whole Static Exact contract materialization is retired')
    with path.open('r', encoding='utf-8') as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f'{path}: expected a JSON object')
    schema = value.get('schema')
    if schema != PACKED_ASSIGNMENT_SCHEMA:
        raise ValueError('retired or unsupported Phase 3 assignment storage schema; current v4 selector required')
    document, partitions, descriptor = value.get('document'), value.get('partitions'), value.get('cut_nets_index')
    if (not isinstance(document, dict) or not isinstance(partitions, list)
            or any(not isinstance(row, dict) for row in partitions)
            or not isinstance(descriptor, Mapping) or 'cut_nets' in document
            or value.get('semantic_contract') is not None):
        raise ValueError('current assignment storage header is malformed')
    artifact = descriptor.get('artifact')
    if not isinstance(artifact, str) or Path(artifact).name != artifact:
        raise ValueError('packed assignment cut-record index is malformed')
    index_path = path.with_name(artifact)
    cuts = IndexedCutNetRecords(index_path, descriptor)
    from emuflow.partition.phase3_assignment_index import open_phase3_assignment_index
    connection = open_phase3_assignment_index(index_path)
    try:
        metadata = {row[0]: json.loads(row[1]) for row in connection.execute('SELECT key,value FROM metadata')}
    finally:
        connection.close()
    for field in ('design', 'platform', 'provider', 'seed', 'constraints'):
        if document.get(field) != metadata.get(field):
            raise ValueError('assignment selector and typed owner identity disagree')
    result = {**document, 'partitions': partitions, 'cut_nets': cuts, 'cut_records_indexed': True}
    indexed = value.get('semantic_contract_index')
    digest = metadata.get('semantic_contract_sha256')
    if indexed is not None:
        if (not isinstance(indexed, dict) or set(indexed) != {'artifact', 'schema', 'sha256'}
                or indexed.get('artifact') != index_path.name
                or indexed.get('schema') != 'emuflow.static-exact-combinational-cut/v3'
                or indexed.get('sha256') != result.get('semantic_contract_sha256')
                or indexed.get('sha256') != digest
                or metadata.get('semantic_contract_schema') != indexed.get('schema')):
            raise ValueError('packed assignment semantic index is malformed')
        result['semantic_contract_indexed'] = True
        result['semantic_contract_schema'] = indexed['schema']
    elif digest is not None or result.get('semantic_contract_sha256') is not None:
        raise ValueError('assignment semantic owner reference is missing')
    if include_instance_assignment:
        result['instance_assignment'] = IndexedInstanceAssignment(index_path)
    return result


def read_phase3_cut_timing_view(path: Path, *, include_instance_assignment: bool = False) -> Dict[str, Any]:
    """Use the current typed cut relation; never restore old semantic trees."""
    index = path.with_name('assignment.sqlite')
    if index.is_file():
        from emuflow.partition.phase3_assignment_index import open_phase3_assignment_index
        connection = open_phase3_assignment_index(index)
        connection.close()
    return read_phase3_assignment_view(path, include_instance_assignment=include_instance_assignment)
