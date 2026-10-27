from __future__ import annotations

import base64
import json
import sqlite3
import threading
import zlib
from array import array
from collections import OrderedDict
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping

from .io import file_sha256


PACKED_CLUSTERS_SCHEMA = "emuflow.phase3-clusters-storage/v1"
COMPRESSED_CLUSTERS_SCHEMA = "emuflow.phase3-clusters-storage/v2"
INDEXED_CLUSTERS_SCHEMA = "emuflow.phase3-clusters-storage/v3"
LEGACY_PACKED_ASSIGNMENT_SCHEMA = "emuflow.phase3-assignment-storage/v1"
OLDER_PACKED_ASSIGNMENT_SCHEMA = "emuflow.phase3-assignment-storage/v2"
PREVIOUS_PACKED_ASSIGNMENT_SCHEMA = "emuflow.phase3-assignment-storage/v3"
PACKED_ASSIGNMENT_SCHEMA = "emuflow.phase3-assignment-storage/v4"
_COMPRESSED_JSON_CODEC = "zlib-level-1-base64-canonical-json"
_COMPRESSED_INDEX_CODEC = "zlib-level-1-base64-unsigned-index"
CLUSTERS_SQLITE_APPLICATION_ID = 0x454D5543  # ASCII "EMUC"
CLUSTERS_SQLITE_USER_VERSION = 1
CLUSTERS_SQLITE_FORMAT = "sqlite-phase3-clusters/v1"
CLUSTERS_STORE_NAME = "clusters.sqlite"


_CLUSTERS_DDL = (Path(__file__).parent / "data/schema/phase3_clusters.sql").read_text(
    encoding="utf-8"
)

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


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)




def _decode_u32_section(raw: bytes, records: int, context: str) -> List[int]:
    if len(raw) != records * 4:
        raise ValueError(f"{context}: uint32 section length is invalid")
    return [
        int.from_bytes(raw[offset : offset + 4], "little")
        for offset in range(0, len(raw), 4)
    ]


def _decode_string_section(raw: bytes, records: int, context: str) -> List[str]:
    result: List[str] = []
    offset = 0
    try:
        for _ in range(records):
            if offset + 4 > len(raw):
                raise ValueError("missing string length")
            length = int.from_bytes(raw[offset : offset + 4], "little")
            offset += 4
            end = offset + length
            if end > len(raw):
                raise ValueError("truncated string")
            result.append(raw[offset:end].decode("utf-8"))
            offset = end
    except UnicodeDecodeError as error:
        raise ValueError(f"{context}: string section is not UTF-8") from error
    if offset != len(raw):
        raise ValueError(f"{context}: string section has trailing data")
    return result


def _load_cluster_section(
    connection: sqlite3.Connection, section: str
) -> tuple[bytes, int]:
    row = connection.execute(
        "SELECT codec,records,raw_bytes,compressed_bytes,chunks "
        "FROM sections WHERE section=?",
        (section,),
    ).fetchone()
    if row is None or row["codec"] != _CLUSTER_SECTION_CODEC:
        raise ValueError(f"indexed clusters section {section!r} is missing")
    decompressor = zlib.decompressobj()
    output = bytearray()
    observed_chunks = 0
    observed_compressed = 0
    for chunk in connection.execute(
        "SELECT chunk_seq,payload FROM section_chunks "
        "WHERE section=? ORDER BY chunk_seq",
        (section,),
    ):
        if chunk["chunk_seq"] != observed_chunks:
            raise ValueError(f"indexed clusters section {section!r} is sparse")
        payload = bytes(chunk["payload"])
        observed_compressed += len(payload)
        output.extend(decompressor.decompress(payload))
        observed_chunks += 1
    output.extend(decompressor.flush())
    if (
        observed_chunks != row["chunks"]
        or observed_compressed != row["compressed_bytes"]
        or len(output) != row["raw_bytes"]
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise ValueError(f"indexed clusters section {section!r} is corrupt")
    return bytes(output), int(row["records"])




def _decompress_json(value: Any, context: str) -> Dict[str, Any]:
    if (
        not isinstance(value, dict)
        or value.get("codec") != _COMPRESSED_JSON_CODEC
        or isinstance(value.get("uncompressed_bytes"), bool)
        or not isinstance(value.get("uncompressed_bytes"), int)
        or value["uncompressed_bytes"] < 0
        or not isinstance(value.get("data"), str)
    ):
        raise ValueError(f"{context}: compressed payload is malformed")
    expected_size = value["uncompressed_bytes"]
    try:
        encoded = base64.b64decode(value["data"], validate=True)
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(encoded, expected_size + 1)
        if len(raw) > expected_size or decompressor.unconsumed_tail:
            raise ValueError("compressed length exceeds the declaration")
        raw += decompressor.flush(expected_size - len(raw) + 1)
        if (
            len(raw) != expected_size
            or not decompressor.eof
            or decompressor.unused_data
        ):
            raise ValueError("compressed length is inconsistent")
        result = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, zlib.error) as error:
        raise ValueError(f"{context}: compressed payload is invalid") from error
    if not isinstance(result, dict):
        raise ValueError(f"{context}: expected an object")
    return result




def _decompress_index_bytes(
    value: Any, context: str
) -> tuple[bytes, int, int]:
    if (
        not isinstance(value, dict)
        or value.get("codec") != _COMPRESSED_INDEX_CODEC
        or isinstance(value.get("count"), bool)
        or not isinstance(value.get("count"), int)
        or value["count"] < 0
        or value.get("width") not in {1, 2, 4}
        or not isinstance(value.get("data"), str)
    ):
        raise ValueError(f"{context}: compressed index is malformed")
    count = value["count"]
    width = value["width"]
    expected = count * width
    try:
        raw = zlib.decompress(
            base64.b64decode(value["data"], validate=True)
        )
    except (ValueError, zlib.error) as error:
        raise ValueError(f"{context}: compressed index is corrupt") from error
    if len(raw) != expected:
        raise ValueError(f"{context}: compressed index length disagrees")
    return raw, count, width


def _decompress_indices(value: Any, context: str) -> List[int]:
    raw, count, width = _decompress_index_bytes(value, context)
    return [
        int.from_bytes(raw[offset : offset + width], "little")
        for offset in range(0, count * width, width)
    ]






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






def expand_indexed_phase3_clusters(
    value: Mapping[str, Any], path: Path
) -> Dict[str, Any]:
    store_path, metadata = _read_cluster_store_metadata(path, value)
    connection = _open_cluster_store(store_path)
    try:
        def strings(name: str) -> List[str]:
            raw, records = _load_cluster_section(connection, name)
            return _decode_string_section(raw, records, f"{path}:{name}")

        def integers(name: str) -> List[int]:
            raw, records = _load_cluster_section(connection, name)
            return _decode_u32_section(raw, records, f"{path}:{name}")

        document_values = strings("document")
        if len(document_values) != 1:
            raise ValueError(f"{path}: indexed cluster document is invalid")
        document = json.loads(document_values[0])
        if (
            not isinstance(document, dict)
            or document.get("schema") != value["logical_schema"]
            or "clusters" in document
            or document.get("instances") != value["counts"]["instances"]
        ):
            raise ValueError(f"{path}: indexed cluster document is invalid")

        cluster_ids = strings("cluster_ids")
        member_counts = integers("member_counts")
        member_names = strings("member_names")
        resource_counts = integers("resource_counts")
        resource_names = strings("resource_names")
        resource_values = strings("resource_values")
        fixed_indices = integers("fixed_indices")
        fixed_values = strings("fixed_values")
        group_counts = integers("group_counts")
        group_values = strings("group_values")
        extra_values = strings("extra_values")
        eligible_nets = strings("eligible_nets")
        cluster_count = value["counts"]["clusters"]
        if (
            len(cluster_ids) != cluster_count
            or len(set(cluster_ids)) != cluster_count
            or len(member_counts) != cluster_count
            or len(resource_counts) != cluster_count
            or len(group_counts) != cluster_count
            or len(extra_values) != cluster_count
            or sum(member_counts) != len(member_names)
            or len(member_names) != value["counts"]["instances"]
            or len(set(member_names)) != len(member_names)
            or sum(resource_counts) != len(resource_names)
            or len(resource_names) != len(resource_values)
            or sum(group_counts) != len(group_values)
            or len(fixed_indices) != len(fixed_values)
            or fixed_indices != sorted(set(fixed_indices))
            or any(index >= cluster_count for index in fixed_indices)
            or len(eligible_nets) != value["counts"]["eligible_nets"]
            or len(set(eligible_nets)) != len(eligible_nets)
        ):
            raise ValueError(f"{path}: indexed cluster columns disagree")
        fixed_by_cluster = dict(zip(fixed_indices, fixed_values))
        clusters: List[Dict[str, Any]] = []
        member_offset = resource_offset = group_offset = 0
        for cluster_seq, cluster_id in enumerate(cluster_ids):
            next_member = member_offset + member_counts[cluster_seq]
            next_resource = resource_offset + resource_counts[cluster_seq]
            next_group = group_offset + group_counts[cluster_seq]
            names = resource_names[resource_offset:next_resource]
            if len(set(names)) != len(names):
                raise ValueError(f"{path}: indexed cluster resources repeat")
            groups = group_values[group_offset:next_group]
            if len(set(groups)) != len(groups):
                raise ValueError(f"{path}: indexed cluster groups repeat")
            extra = json.loads(extra_values[cluster_seq])
            if not isinstance(extra, dict):
                raise ValueError(f"{path}: indexed cluster extras are invalid")
            resources = {
                name: json.loads(encoded)
                for name, encoded in zip(
                    names, resource_values[resource_offset:next_resource]
                )
            }
            clusters.append(
                {
                    "id": cluster_id,
                    "instances": member_names[member_offset:next_member],
                    "resources": resources,
                    "fixed_fpga": fixed_by_cluster.get(cluster_seq),
                    "groups": groups,
                    **extra,
                }
            )
            member_offset = next_member
            resource_offset = next_resource
            group_offset = next_group
        if metadata["eligible_nets_present"]:
            policy = document.get("policy")
            if not isinstance(policy, dict):
                raise ValueError(f"{path}: indexed cluster policy is invalid")
            policy["eligible_combinational_cut_nets"] = eligible_nets
    except (IndexError, json.JSONDecodeError, sqlite3.DatabaseError, zlib.error) as error:
        raise ValueError(f"{path}: indexed clusters payload is corrupt") from error
    finally:
        connection.close()
    return {**document, "clusters": clusters}

def phase3_clusters_store_path(selector_path: Path) -> Path | None:
    """Resolve and validate the typed cluster owner named by a selector."""

    with selector_path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{selector_path}: expected a JSON object")
    schema = value.get("schema")
    if schema != INDEXED_CLUSTERS_SCHEMA:
        if isinstance(schema, str) and schema.startswith(
            "emuflow.phase3-clusters-storage/"
        ) and schema not in {PACKED_CLUSTERS_SCHEMA, COMPRESSED_CLUSTERS_SCHEMA}:
            raise ValueError(f"{selector_path}: unsupported clusters storage schema")
        return None
    store_path, _ = _read_cluster_store_metadata(selector_path, value)
    return store_path


def expand_compressed_phase3_clusters(value: Mapping[str, Any]) -> Dict[str, Any]:
    if value.get("schema") != COMPRESSED_CLUSTERS_SCHEMA:
        raise ValueError("not a compressed Phase 3 clusters document")
    result = _decompress_json(value.get("payload"), "packed clusters")
    clusters = result.get("clusters")
    if (
        not isinstance(clusters, list)
        or len(clusters) != value.get("cluster_count")
    ):
        raise ValueError("packed clusters count is inconsistent")
    return result


def _require_list(value: Any, context: str) -> List[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{context}: expected an array")
    return value




def _expand_rows(
    counts: Any,
    values: Any,
    context: str,
) -> List[List[Any]]:
    raw_counts = _require_list(counts, f"{context}.counts")
    raw_values = _require_list(values, f"{context}.values")
    if any(
        isinstance(count, bool) or not isinstance(count, int) or count < 0
        for count in raw_counts
    ):
        raise ValueError(f"{context}.counts: expected non-negative integers")
    if sum(raw_counts) != len(raw_values):
        raise ValueError(f"{context}: flattened value count is inconsistent")
    result: List[List[Any]] = []
    offset = 0
    for count in raw_counts:
        result.append(raw_values[offset : offset + count])
        offset += count
    return result




def expand_phase3_clusters(value: Mapping[str, Any]) -> Dict[str, Any]:
    if value.get("schema") != PACKED_CLUSTERS_SCHEMA:
        raise ValueError("not a packed Phase 3 clusters document")
    document = value.get("document")
    count = value.get("cluster_count")
    if (
        not isinstance(document, dict)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
    ):
        raise ValueError("packed clusters header is malformed")
    raw_ids = value.get("cluster_ids")
    if raw_ids is None:
        ids = [f"c{index:06d}" for index in range(count)]
    else:
        ids = _require_list(raw_ids, "packed clusters.cluster_ids")
        if len(ids) != count or not all(isinstance(item, str) for item in ids):
            raise ValueError("packed clusters.cluster_ids is malformed")
    instances = _expand_rows(
        value.get("instance_counts"), value.get("instances"), "packed instances"
    )
    resource_rows = _expand_rows(
        value.get("resource_counts"),
        value.get("resource_entries"),
        "packed resources",
    )
    groups = _expand_rows(
        value.get("group_counts"), value.get("groups"), "packed groups"
    )
    resource_names = _require_list(
        value.get("resource_names"), "packed clusters.resource_names"
    )
    if not all(isinstance(item, str) for item in resource_names):
        raise ValueError("packed clusters.resource_names is malformed")
    if len(instances) != count or len(resource_rows) != count or len(groups) != count:
        raise ValueError("packed clusters column lengths disagree")
    fixed_by_index: Dict[int, Any] = {}
    for item in _require_list(value.get("fixed"), "packed clusters.fixed"):
        if (
            not isinstance(item, list)
            or len(item) != 2
            or isinstance(item[0], bool)
            or not isinstance(item[0], int)
            or item[0] < 0
            or item[0] >= count
            or item[0] in fixed_by_index
        ):
            raise ValueError("packed clusters.fixed is malformed")
        fixed_by_index[item[0]] = item[1]
    raw_extra = value.get("extra")
    extra = [{} for _ in range(count)] if raw_extra is None else raw_extra
    if (
        not isinstance(extra, list)
        or len(extra) != count
        or not all(isinstance(item, dict) for item in extra)
    ):
        raise ValueError("packed clusters.extra is malformed")
    clusters: List[Dict[str, Any]] = []
    for index in range(count):
        resources: Dict[str, Any] = {}
        for entry in resource_rows[index]:
            if (
                not isinstance(entry, list)
                or len(entry) != 2
                or isinstance(entry[0], bool)
                or not isinstance(entry[0], int)
                or entry[0] < 0
                or entry[0] >= len(resource_names)
                or resource_names[entry[0]] in resources
            ):
                raise ValueError("packed resource entry is malformed")
            resources[resource_names[entry[0]]] = entry[1]
        if not all(isinstance(item, str) for item in instances[index]) or not all(
            isinstance(item, str) for item in groups[index]
        ):
            raise ValueError("packed cluster string columns are malformed")
        clusters.append(
            {
                "id": ids[index],
                "instances": instances[index],
                "resources": resources,
                "fixed_fpga": fixed_by_index.get(index),
                "groups": groups[index],
                **extra[index],
            }
        )
    return {**document, "clusters": clusters}




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
        from .phase3_assignment_index import iter_phase3_cut_records

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


def expand_phase3_assignment(
    value: Mapping[str, Any],
    path: Path,
    read_document: Callable[[Path], Dict[str, Any]],
) -> Dict[str, Any]:
    schema = value.get("schema")
    if schema not in {
        LEGACY_PACKED_ASSIGNMENT_SCHEMA,
        OLDER_PACKED_ASSIGNMENT_SCHEMA,
        PREVIOUS_PACKED_ASSIGNMENT_SCHEMA,
        PACKED_ASSIGNMENT_SCHEMA,
    }:
        raise ValueError("not a packed Phase 3 assignment document")
    document = value.get("document")
    count = value.get("cluster_count")
    part_ids = value.get("part_ids")
    raw_cluster_parts = value.get("cluster_parts")
    partitions = value.get("partitions")
    compressed_semantic_contract = value.get("semantic_contract")
    indexed_semantic_contract = value.get("semantic_contract_index")
    indexed_cut_nets = value.get("cut_nets_index")
    if schema == PACKED_ASSIGNMENT_SCHEMA:
        if not isinstance(indexed_cut_nets, Mapping) or "cut_nets" in document:
            raise ValueError("packed assignment cut storage is ambiguous")
    elif indexed_cut_nets is not None:
        raise ValueError("legacy packed assignment has a cut-record index")
    if (
        compressed_semantic_contract is not None
        and indexed_semantic_contract is not None
    ):
        raise ValueError("packed assignment semantic storage is ambiguous")
    if (
        not isinstance(document, dict)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or not isinstance(part_ids, list)
        or not all(isinstance(item, str) for item in part_ids)
        or len(set(part_ids)) != len(part_ids)
        or (
            schema == LEGACY_PACKED_ASSIGNMENT_SCHEMA
            and not isinstance(raw_cluster_parts, list)
        )
        or not isinstance(partitions, list)
        or not all(isinstance(item, dict) for item in partitions)
    ):
        raise ValueError("packed assignment header is malformed")
    cluster_parts = (
        raw_cluster_parts
        if isinstance(raw_cluster_parts, list)
        else _decompress_indices(raw_cluster_parts, "packed assignment")
    )
    if len(cluster_parts) != count:
        raise ValueError("packed assignment vector is malformed")
    semantic_contract = None
    if compressed_semantic_contract is not None:
        try:
            semantic_contract = _decompress_json(
                compressed_semantic_contract,
                "packed assignment semantic contract",
            )
        except ValueError as error:
            raise ValueError(
                "packed assignment semantic contract is corrupt"
            ) from error
    clusters_path = path.with_name("clusters.json")
    if not clusters_path.is_file():
        raise ValueError(
            f"{path}: packed assignment requires sibling clusters.json"
        )
    clusters = read_document(clusters_path).get("clusters")
    if not isinstance(clusters, list) or len(clusters) != count:
        raise ValueError("packed assignment cluster dependency is inconsistent")
    cluster_assignment: Dict[str, str] = {}
    instance_assignment: Dict[str, str] = {}
    clusters_by_part: Dict[str, List[str]] = {part: [] for part in part_ids}
    for index, cluster in enumerate(clusters):
        part_index = cluster_parts[index]
        if (
            isinstance(part_index, bool)
            or not isinstance(part_index, int)
            or part_index < 0
            or part_index >= len(part_ids)
            or not isinstance(cluster, dict)
            or not isinstance(cluster.get("id"), str)
            or not isinstance(cluster.get("instances"), list)
        ):
            raise ValueError("packed assignment vector is malformed")
        part = part_ids[part_index]
        cluster_id = cluster["id"]
        cluster_assignment[cluster_id] = part
        clusters_by_part[part].append(cluster_id)
        for instance in cluster["instances"]:
            if not isinstance(instance, str) or instance in instance_assignment:
                raise ValueError("packed assignment instance coverage is malformed")
            instance_assignment[instance] = part
    expanded_partitions = []
    for partition in partitions:
        fpga = partition.get("fpga")
        if fpga not in clusters_by_part:
            raise ValueError("packed assignment partition references unknown FPGA")
        expanded_partitions.append(
            {**partition, "clusters": sorted(clusters_by_part[fpga])}
        )
    if {partition["fpga"] for partition in partitions} != set(part_ids):
        raise ValueError("packed assignment partition coverage is inconsistent")
    result = {
        **document,
        "cluster_assignment": dict(sorted(cluster_assignment.items())),
        "instance_assignment": dict(sorted(instance_assignment.items())),
        "partitions": expanded_partitions,
    }
    if indexed_cut_nets is not None:
        artifact = indexed_cut_nets.get("artifact")
        if not isinstance(artifact, str) or Path(artifact).name != artifact:
            raise ValueError("packed assignment cut-record index is malformed")
        index_path = path.with_name(artifact)
        result["cut_nets"] = list(
            IndexedCutNetRecords(index_path, indexed_cut_nets)
        )
    if semantic_contract is not None:
        result["semantic_contract"] = semantic_contract
    if indexed_semantic_contract is not None:
        if (
            not isinstance(indexed_semantic_contract, dict)
            or indexed_semantic_contract.get("artifact")
            != "assignment.sqlite"
            or indexed_semantic_contract.get("schema")
            != "emuflow.static-exact-combinational-cut/v3"
            or not isinstance(indexed_semantic_contract.get("sha256"), str)
            or len(indexed_semantic_contract["sha256"]) != 64
            or indexed_semantic_contract["sha256"]
            != result.get("semantic_contract_sha256")
        ):
            raise ValueError("packed assignment semantic index is malformed")
        index_path = path.with_name(indexed_semantic_contract["artifact"])
        if not index_path.is_file():
            raise ValueError(
                f"{path}: indexed semantic contract requires sibling "
                f"{indexed_semantic_contract['artifact']}"
            )
        from .phase3_assignment_index import (
            read_phase3_exact_semantic_contract,
        )

        result["semantic_contract"] = read_phase3_exact_semantic_contract(
            index_path, result
        )
    return result


class IndexedInstanceAssignment(Mapping[str, str]):
    """Lazy instance-to-FPGA relation backed by assignment.sqlite."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        from .phase3_assignment_index import open_phase3_assignment_index

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


class PackedClusterAssignment(Mapping[str, str]):
    """Read-only cluster ownership without a graph-sized Python dictionary."""

    def __init__(self, value: Mapping[str, Any]):
        self.part_ids = value.get("part_ids")
        self.count = value.get("cluster_count")
        self.cluster_ids = value.get("cluster_ids")
        raw_parts = value.get("cluster_parts")
        if (
            not isinstance(self.part_ids, list)
            or not all(isinstance(item, str) for item in self.part_ids)
            or isinstance(self.count, bool)
            or not isinstance(self.count, int)
            or self.count < 0
            or (
                self.cluster_ids is not None
                and (
                    not isinstance(self.cluster_ids, list)
                    or len(self.cluster_ids) != self.count
                    or not all(
                        isinstance(item, str) for item in self.cluster_ids
                    )
                )
            )
        ):
            raise ValueError("packed cluster assignment header is malformed")
        if isinstance(raw_parts, list):
            if len(raw_parts) != self.count:
                raise ValueError("packed cluster assignment length disagrees")
            self.raw_parts = None
            self.width = 0
            self.legacy_parts = raw_parts
        else:
            raw, count, width = _decompress_index_bytes(
                raw_parts, "packed cluster assignment"
            )
            if count != self.count:
                raise ValueError("packed cluster assignment length disagrees")
            self.raw_parts = raw
            self.width = width
            self.legacy_parts = None
        self.id_index = (
            None
            if self.cluster_ids is None
            else {
                cluster_id: index
                for index, cluster_id in enumerate(self.cluster_ids)
            }
        )

    def _index(self, key: str) -> int | None:
        if self.id_index is not None:
            return self.id_index.get(key)
        if not isinstance(key, str) or not key.startswith("c"):
            return None
        try:
            index = int(key[1:])
        except ValueError:
            return None
        if index < 0 or index >= self.count or key != f"c{index:06d}":
            return None
        return index

    def _part(self, index: int) -> str:
        if self.legacy_parts is not None:
            part_index = self.legacy_parts[index]
        else:
            assert self.raw_parts is not None
            offset = index * self.width
            part_index = int.from_bytes(
                self.raw_parts[offset : offset + self.width], "little"
            )
        if (
            isinstance(part_index, bool)
            or not isinstance(part_index, int)
            or part_index < 0
            or part_index >= len(self.part_ids)
        ):
            raise ValueError("packed cluster assignment owner is invalid")
        return self.part_ids[part_index]

    def __getitem__(self, key: str) -> str:
        index = self._index(key)
        if index is None:
            raise KeyError(key)
        return self._part(index)

    def __iter__(self) -> Iterator[str]:
        if self.cluster_ids is not None:
            return iter(self.cluster_ids)
        return (f"c{index:06d}" for index in range(self.count))

    def __len__(self) -> int:
        return self.count


def read_phase3_assignment_view(
    path: Path,
    *,
    include_instance_assignment: bool = False,
    include_cluster_assignment: bool = False,
    include_semantic_contract: bool = True,
) -> Dict[str, Any]:
    """Read only the Phase 4--7 assignment contract.

    Production consumers need cut nets, partition summaries and Static Exact
    semantics.  Expanding the sibling clusters artifact merely to reconstruct
    millions of cluster and instance dictionaries is not part of that
    contract.  Callers that genuinely need instance lookup receive a lazy
    SQLite mapping instead.
    """

    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    if value.get("schema") not in {
        LEGACY_PACKED_ASSIGNMENT_SCHEMA,
        OLDER_PACKED_ASSIGNMENT_SCHEMA,
        PREVIOUS_PACKED_ASSIGNMENT_SCHEMA,
        PACKED_ASSIGNMENT_SCHEMA,
    }:
        if (
            not include_semantic_contract
            and isinstance(value.get("semantic_contract"), dict)
        ):
            result = dict(value)
            contract = result.pop("semantic_contract")
            result["semantic_contract_indexed"] = True
            result["semantic_contract_schema"] = contract.get("schema")
            return result
        return value
    document = value.get("document")
    partitions = value.get("partitions")
    if not isinstance(document, dict) or not isinstance(partitions, list):
        raise ValueError("packed assignment downstream view is malformed")
    result = {**document, "partitions": partitions}
    indexed_cut_nets = value.get("cut_nets_index")
    if value.get("schema") == PACKED_ASSIGNMENT_SCHEMA:
        if not isinstance(indexed_cut_nets, Mapping) or "cut_nets" in document:
            raise ValueError("packed assignment cut storage is ambiguous")
        artifact = indexed_cut_nets.get("artifact")
        if not isinstance(artifact, str) or Path(artifact).name != artifact:
            raise ValueError("packed assignment cut-record index is malformed")
        index_path = path.with_name(artifact)
        result["cut_nets"] = IndexedCutNetRecords(
            index_path, indexed_cut_nets
        )
        result["cut_records_indexed"] = True
    elif indexed_cut_nets is not None:
        raise ValueError("legacy packed assignment has a cut-record index")
    compressed = value.get("semantic_contract")
    indexed = value.get("semantic_contract_index")
    if compressed is not None and indexed is not None:
        raise ValueError("packed assignment semantic storage is ambiguous")
    enrich_indexed_contract = False
    if compressed is not None and include_semantic_contract:
        result["semantic_contract"] = _decompress_json(
            compressed,
            "packed assignment semantic contract",
        )
    elif compressed is not None:
        # Production packed assignments are emitted only for the generalized
        # structural Static Exact contract.  Phase 4 needs its identity and
        # the dependency fields already copied into ``cut_nets``; expanding
        # every logic segment is unnecessary until the scheduler consumes it.
        result["semantic_contract_indexed"] = True
        result["semantic_contract_schema"] = (
            "emuflow.static-exact-combinational-cut/v3"
        )
        enrich_indexed_contract = True
    elif indexed is not None:
        if (
            not isinstance(indexed, dict)
            or indexed.get("artifact") != "assignment.sqlite"
            or indexed.get("schema")
            != "emuflow.static-exact-combinational-cut/v3"
            or not isinstance(indexed.get("sha256"), str)
            or len(indexed["sha256"]) != 64
            or indexed["sha256"] != result.get("semantic_contract_sha256")
        ):
            raise ValueError("packed assignment semantic index is malformed")
        index_path = path.with_name(indexed["artifact"])
        if not index_path.is_file():
            raise ValueError(
                f"{path}: indexed semantic contract requires sibling "
                f"{indexed['artifact']}"
            )
        if include_semantic_contract:
            from .phase3_assignment_index import (
                read_phase3_exact_semantic_contract,
            )

            result["semantic_contract"] = read_phase3_exact_semantic_contract(
                index_path, result
            )
        else:
            result["semantic_contract_indexed"] = True
            result["semantic_contract_schema"] = indexed["schema"]
            enrich_indexed_contract = True
    if include_cluster_assignment:
        result["cluster_assignment"] = PackedClusterAssignment(value)
    if include_instance_assignment:
        index_path = path.with_name("assignment.sqlite")
        if not index_path.is_file():
            # Explicit compatibility fallback for old/small artifacts.  Every
            # production Phase 3 publication has the sidecar; legacy fixtures
            # may not.  Keep their prior behavior without making the large-
            # design path materialize the cluster graph.
            clusters_path = path.with_name("clusters.json")

            def read_clusters(candidate: Path) -> Dict[str, Any]:
                with candidate.open("r", encoding="utf-8") as stream:
                    clusters_value = json.load(stream)
                schema = clusters_value.get("schema")
                if schema == COMPRESSED_CLUSTERS_SCHEMA:
                    return expand_compressed_phase3_clusters(clusters_value)
                if schema == INDEXED_CLUSTERS_SCHEMA:
                    return expand_indexed_phase3_clusters(
                        clusters_value, candidate
                    )
                if schema == PACKED_CLUSTERS_SCHEMA:
                    return expand_phase3_clusters(clusters_value)
                return clusters_value

            return expand_phase3_assignment(value, path, read_clusters)
        result["instance_assignment"] = IndexedInstanceAssignment(index_path)
    if enrich_indexed_contract and indexed_cut_nets is None:
        # Drop the potentially gigabyte-scale base64 string before scanning
        # the indexed dependency relations.  The route view retains only the
        # cut records that Phase 4 actually consumes.
        compressed = None
        value = None
        index_path = path.with_name("assignment.sqlite")
        if index_path.is_file():
            _enrich_indexed_cut_dependencies(
                result,
                index_path,
                require_identity=indexed is not None,
            )
    return result


def _segment_number(segment_id: Any) -> int:
    if not isinstance(segment_id, str) or not segment_id.startswith("segment"):
        raise ValueError("assignment index segment identity is invalid")
    suffix = segment_id[7:]
    if not suffix.isdigit():
        raise ValueError("assignment index segment identity is invalid")
    return int(suffix)


def _enrich_indexed_cut_dependencies(
    assignment: Dict[str, Any],
    index_path: Path,
    *,
    require_identity: bool = False,
) -> None:
    """Reconstruct route-level dependency fields without semantic JSON.

    ``exact_cut_sources`` identifies every segment that feeds a cut TX.  A
    segment that also appears in ``exact_logic_segments`` is an RX-to-TX edge;
    joining those two relations reconstructs the cut dependency DAG.  Dense
    numeric segment IDs keep the join vector at four bytes per segment instead
    of a multi-gigabyte Python string dictionary.
    """

    cuts = assignment.get("cut_nets")
    if not isinstance(cuts, list):
        raise ValueError("packed assignment cut nets are invalid")
    net_ids = []
    net_index: Dict[str, int] = {}
    for index, cut in enumerate(cuts):
        if not isinstance(cut, dict) or not isinstance(cut.get("net"), str):
            raise ValueError("packed assignment cut net is invalid")
        net_id = cut["net"]
        if net_id in net_index:
            raise ValueError("packed assignment cut nets are not unique")
        net_index[net_id] = index
        net_ids.append(net_id)

    predecessors: List[List[int]] = [[] for _ in cuts]
    successors: List[List[int]] = [[] for _ in cuts]
    segment_sinks = array("i")
    from .phase3_assignment_index import open_phase3_assignment_index

    connection = open_phase3_assignment_index(index_path)
    try:
        metadata = {
            str(row[0]): json.loads(row[1])
            for row in connection.execute(
                "SELECT key,value FROM metadata WHERE key IN "
                "('semantic_contract_schema','semantic_contract_sha256',"
                "'semantic_contract_binary')"
            )
        }
        if require_identity:
            if (
                metadata.get("semantic_contract_schema")
                != assignment.get("semantic_contract_schema")
                or metadata.get("semantic_contract_sha256")
                != assignment.get("semantic_contract_sha256")
            ):
                raise ValueError(
                    "assignment index semantic contract identity disagrees"
                )
        binary = metadata.get("semantic_contract_binary")
        if isinstance(binary, Mapping):
            from .phase3_exact_binary import (
                iter_phase3_exact_node_sources,
                iter_phase3_exact_segments,
            )

            artifact = binary.get("artifact")
            if not isinstance(artifact, str) or Path(artifact).name != artifact:
                raise ValueError("assignment Static Exact binary path is invalid")
            binary_path = index_path.with_name(artifact)
            node_rows = iter_phase3_exact_node_sources(binary_path, binary)
            segment_rows = (
                (segment.get("source_cut_net"), segment["id"])
                for segment in iter_phase3_exact_segments(binary_path, binary)
                if isinstance(segment.get("source_cut_net"), str)
            )
        else:
            raise ValueError("assignment index lacks its Static Exact binary descriptor")

        exact_nodes = set()
        for net_id, segment_numbers in node_rows:
            exact_nodes.add(str(net_id))
            sink = net_index.get(str(net_id))
            if sink is None:
                raise ValueError("assignment index cut source is unknown")
            for number in segment_numbers:
                if number >= len(segment_sinks):
                    segment_sinks.extend(
                        array("i", [-1]) * (number + 1 - len(segment_sinks))
                    )
                if segment_sinks[number] != -1:
                    raise ValueError(
                        "assignment index source segment is not unique"
                    )
                segment_sinks[number] = sink
        if exact_nodes != set(net_ids):
            raise ValueError(
                "assignment index exact-cut coverage is inconsistent"
            )
        for source_net, segment_id in segment_rows:
            number = _segment_number(segment_id)
            if number >= len(segment_sinks):
                continue
            sink = segment_sinks[number]
            if sink < 0:
                continue
            source = net_index.get(str(source_net))
            if source is None:
                raise ValueError("assignment index dependency source is unknown")
            if source == sink:
                raise ValueError("assignment index dependency self-loop is invalid")
            predecessors[sink].append(source)
            successors[source].append(sink)
    finally:
        connection.close()

    indegree = array("I", (len(items) for items in predecessors))
    levels = array("I", [0]) * len(cuts)
    depths = array("I", [0]) * len(cuts)
    pending = deque(index for index, degree in enumerate(indegree) if degree == 0)
    visited = 0
    while pending:
        node = pending.popleft()
        visited += 1
        levels[node] = 1 + max(
            (levels[source] for source in predecessors[node]), default=0
        )
        depths[node] = max(
            (depths[source] for source in predecessors[node]), default=0
        ) + (1 if cuts[node].get("cut_class") == "combinational" else 0)
        for sink in successors[node]:
            indegree[sink] -= 1
            if indegree[sink] == 0:
                pending.append(sink)
    if visited != len(cuts):
        raise ValueError("assignment index exact-cut dependency graph has a cycle")

    for index, cut in enumerate(cuts):
        fields = {
            "dependency_level": int(levels[index]),
            "combinational_dependency_depth": int(depths[index]),
            "predecessor_cut_nets": sorted(
                net_ids[source] for source in predecessors[index]
            ),
        }
        for key, expected in fields.items():
            if key in cut and cut[key] != expected:
                raise ValueError(
                    f"packed assignment {key} disagrees with its index"
                )
            cut[key] = expected


def read_phase3_cut_timing_view(
    path: Path,
    *,
    include_instance_assignment: bool = False,
) -> Dict[str, Any]:
    """Read the Phase-3 relations needed by cut timing from SQLite.

    Static Exact production assignments contain a compressed semantic contract
    whose expanded Python representation can be tens of gigabytes.  Phase-4
    cut qualification and STA projection need only cut endpoints, segment IDs,
    and optional instance ownership; all of those relations are already sealed
    in ``assignment.sqlite``.  Legacy fixtures without that sidecar retain the
    ordinary JSON view.
    """

    index_path = path.with_name("assignment.sqlite")
    if not index_path.is_file():
        return read_phase3_assignment_view(
            path,
            include_instance_assignment=include_instance_assignment,
        )

    with path.open("r", encoding="utf-8") as stream:
        selector = json.load(stream)
    if (
        isinstance(selector, dict)
        and selector.get("schema") == PACKED_ASSIGNMENT_SCHEMA
    ):
        return read_phase3_assignment_view(
            path,
            include_instance_assignment=include_instance_assignment,
            include_semantic_contract=False,
        )

    from .phase3_assignment_index import open_phase3_assignment_index

    connection = open_phase3_assignment_index(index_path)
    connection.row_factory = sqlite3.Row
    try:
        metadata = {
            str(row["key"]): json.loads(row["value"])
            for row in connection.execute("SELECT key,value FROM metadata")
        }
        if metadata.get("schema") not in {
            "emuflow.phase3-assignment-sqlite/v3",
            "emuflow.phase3-assignment-sqlite/v4",
        }:
            raise ValueError("assignment index schema is invalid")
        parts = {
            int(row["part_index"]): str(row["fpga"])
            for row in connection.execute(
                "SELECT part_index,fpga FROM parts ORDER BY part_index"
            )
        }
        cut_records: Dict[str, Dict[str, Any]] = {}
        for row in connection.execute(
            "SELECT net_id,source_part_index,sink_part_index FROM cut_sinks "
            "ORDER BY net_id,sink_part_index"
        ):
            net_id = str(row["net_id"])
            source = parts.get(int(row["source_part_index"]))
            sink = parts.get(int(row["sink_part_index"]))
            if source is None or sink is None:
                raise ValueError("assignment index cut endpoint is invalid")
            record = cut_records.setdefault(
                net_id,
                {
                    "net": net_id,
                    "source_fpgas": [source],
                    "sink_fpgas": [],
                },
            )
            if record["source_fpgas"] != [source]:
                raise ValueError("assignment index cut source is inconsistent")
            record["sink_fpgas"].append(sink)

        result: Dict[str, Any] = {
            "schema": "emuflow.partition-assignment/v1",
            "design": metadata.get("design"),
            "platform": metadata.get("platform"),
            "provider": metadata.get("provider"),
            "seed": metadata.get("seed"),
            "constraints": metadata.get("constraints"),
            "cut_nets": [cut_records[net] for net in sorted(cut_records)],
        }
        exact_digest = metadata.get("semantic_contract_sha256")
        if exact_digest is not None:
            binary = metadata.get("semantic_contract_binary")
            if isinstance(binary, Mapping):
                from .phase3_exact_binary import iter_phase3_exact_node_sources

                artifact = binary.get("artifact")
                if not isinstance(artifact, str) or Path(artifact).name != artifact:
                    raise ValueError("assignment Static Exact binary path is invalid")
                exact_nets = [
                    net
                    for net, _segments in iter_phase3_exact_node_sources(
                        path.with_name(artifact), binary
                    )
                ]
            else:
                raise ValueError("assignment index lacks its Static Exact binary descriptor")
            if exact_nets != sorted(cut_records):
                raise ValueError(
                    "assignment index exact-cut coverage is inconsistent"
                )
            result["semantic_contract_sha256"] = exact_digest
    finally:
        connection.close()

    if include_instance_assignment:
        result["instance_assignment"] = IndexedInstanceAssignment(index_path)
    return result
