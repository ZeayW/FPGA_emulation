from __future__ import annotations

import base64
import json
import sqlite3
import threading
import zlib
from collections import OrderedDict
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping


PACKED_CLUSTERS_SCHEMA = "emuflow.phase3-clusters-storage/v1"
COMPRESSED_CLUSTERS_SCHEMA = "emuflow.phase3-clusters-storage/v2"
LEGACY_PACKED_ASSIGNMENT_SCHEMA = "emuflow.phase3-assignment-storage/v1"
PACKED_ASSIGNMENT_SCHEMA = "emuflow.phase3-assignment-storage/v2"
_COMPRESSED_JSON_CODEC = "zlib-level-1-base64-canonical-json"
_COMPRESSED_INDEX_CODEC = "zlib-level-1-base64-unsigned-index"


def _compress_json(value: Mapping[str, Any]) -> Dict[str, Any]:
    """Compress canonical JSON without first materializing encoded bytes."""

    compressor = zlib.compressobj(level=1)
    chunks: List[bytes] = []
    uncompressed_bytes = 0
    encoder = json.JSONEncoder(
        separators=(",", ":"),
        sort_keys=True,
    )
    for fragment in encoder.iterencode(value):
        encoded = fragment.encode("utf-8")
        uncompressed_bytes += len(encoded)
        chunk = compressor.compress(encoded)
        if chunk:
            chunks.append(chunk)
    tail = compressor.flush()
    if tail:
        chunks.append(tail)
    return {
        "codec": _COMPRESSED_JSON_CODEC,
        "uncompressed_bytes": uncompressed_bytes,
        "data": base64.b64encode(b"".join(chunks)).decode("ascii"),
    }


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


def _compress_indices(values: Iterator[int], maximum: int) -> Dict[str, Any]:
    width = 1 if maximum < 256 else (2 if maximum < 65536 else 4)
    compressor = zlib.compressobj(level=1)
    chunks: List[bytes] = []
    pending = bytearray()
    count = 0
    for item in values:
        if item < 0 or item > maximum:
            raise ValueError("packed assignment partition index is invalid")
        pending.extend(item.to_bytes(width, "little"))
        count += 1
        if len(pending) >= 1024 * 1024:
            chunk = compressor.compress(pending)
            if chunk:
                chunks.append(chunk)
            pending.clear()
    if pending:
        chunk = compressor.compress(pending)
        if chunk:
            chunks.append(chunk)
    tail = compressor.flush()
    if tail:
        chunks.append(tail)
    return {
        "codec": _COMPRESSED_INDEX_CODEC,
        "count": count,
        "width": width,
        "data": base64.b64encode(b"".join(chunks)).decode("ascii"),
    }


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


def compress_phase3_clusters(value: Mapping[str, Any]) -> Dict[str, Any]:
    """Persist production clusters without a second graph-sized Python tree."""

    clusters = value.get("clusters")
    if not isinstance(clusters, list):
        raise ValueError("clusters.clusters: expected an array")
    return {
        "schema": COMPRESSED_CLUSTERS_SCHEMA,
        "cluster_count": len(clusters),
        "payload": _compress_json(value),
    }


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


def _flatten_rows(rows: List[List[Any]]) -> tuple[List[int], List[Any]]:
    counts = [len(row) for row in rows]
    return counts, [item for row in rows for item in row]


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


def pack_phase3_clusters(value: Mapping[str, Any]) -> Dict[str, Any]:
    """Pack the repeated per-cluster JSON fields without losing information."""

    clusters = value.get("clusters")
    if not isinstance(clusters, list):
        raise ValueError("clusters.clusters: expected an array")
    required = {"id", "instances", "resources", "fixed_fpga", "groups"}
    ids: List[str] = []
    instance_rows: List[List[str]] = []
    resource_rows: List[List[List[Any]]] = []
    fixed: List[List[Any]] = []
    group_rows: List[List[str]] = []
    extra_rows: List[Dict[str, Any]] = []
    resource_names = sorted(
        {
            name
            for cluster in clusters
            if isinstance(cluster, dict)
            and isinstance(cluster.get("resources"), dict)
            for name in cluster["resources"]
        }
    )
    resource_index = {name: index for index, name in enumerate(resource_names)}
    for index, cluster in enumerate(clusters):
        if not isinstance(cluster, dict) or not required <= set(cluster):
            raise ValueError(f"clusters[{index}]: unsupported cluster record")
        cluster_id = cluster["id"]
        instances = cluster["instances"]
        resources = cluster["resources"]
        groups = cluster["groups"]
        if (
            not isinstance(cluster_id, str)
            or not isinstance(instances, list)
            or not all(isinstance(item, str) for item in instances)
            or not isinstance(resources, dict)
            or not isinstance(groups, list)
            or not all(isinstance(item, str) for item in groups)
        ):
            raise ValueError(f"clusters[{index}]: malformed cluster record")
        ids.append(cluster_id)
        instance_rows.append(list(instances))
        resource_rows.append(
            [[resource_index[name], resources[name]] for name in sorted(resources)]
        )
        if cluster["fixed_fpga"] is not None:
            fixed.append([index, cluster["fixed_fpga"]])
        group_rows.append(list(groups))
        extra_rows.append(
            {key: item for key, item in cluster.items() if key not in required}
        )
    sequential_ids = ids == [f"c{index:06d}" for index in range(len(ids))]
    instance_counts, instances = _flatten_rows(instance_rows)
    resource_counts, resource_entries = _flatten_rows(resource_rows)
    group_counts, groups = _flatten_rows(group_rows)
    document = {key: item for key, item in value.items() if key != "clusters"}
    return {
        "schema": PACKED_CLUSTERS_SCHEMA,
        "document": document,
        "cluster_count": len(clusters),
        "cluster_ids": None if sequential_ids else ids,
        "instance_counts": instance_counts,
        "instances": instances,
        "resource_names": resource_names,
        "resource_counts": resource_counts,
        "resource_entries": resource_entries,
        "fixed": fixed,
        "group_counts": group_counts,
        "groups": groups,
        "extra": extra_rows if any(extra_rows) else None,
    }


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


def pack_phase3_assignment(
    value: Mapping[str, Any],
    clusters_artifact: Mapping[str, Any],
) -> Dict[str, Any]:
    """Store only the irreducible cluster-to-FPGA vector for Phase 3."""

    clusters = clusters_artifact.get("clusters")
    cluster_assignment = value.get("cluster_assignment")
    instance_assignment = value.get("instance_assignment")
    partitions = value.get("partitions")
    if (
        not isinstance(clusters, list)
        or not isinstance(cluster_assignment, dict)
        or not isinstance(instance_assignment, dict)
        or not isinstance(partitions, list)
    ):
        raise ValueError("assignment cannot be packed: required mappings are absent")
    if len(clusters) != len(cluster_assignment):
        raise ValueError("assignment cannot be packed: cluster coverage differs")
    partition_ids = [
        partition.get("fpga") if isinstance(partition, dict) else None
        for partition in partitions
    ]
    if (
        not all(isinstance(item, str) for item in partition_ids)
        or len(set(partition_ids)) != len(partition_ids)
        or not set(cluster_assignment.values()) <= set(partition_ids)
    ):
        raise ValueError("assignment cannot be packed: invalid FPGA identifier")
    part_ids = sorted(partition_ids)
    part_index = {part: index for index, part in enumerate(part_ids)}
    observed_instances = 0
    sequential_cluster_ids = all(
        cluster.get("id") == f"c{index:06d}"
        for index, cluster in enumerate(clusters)
    )
    stored_cluster_ids = (
        None
        if sequential_cluster_ids
        else [cluster.get("id") for cluster in clusters]
    )

    def cluster_part_indices() -> Iterator[int]:
        nonlocal observed_instances
        for cluster in clusters:
            cluster_id = cluster.get("id")
            if (
                not isinstance(cluster_id, str)
                or cluster_id not in cluster_assignment
            ):
                raise ValueError(
                    "assignment cannot be packed: cluster coverage differs"
                )
            part = cluster_assignment[cluster_id]
            for instance in cluster.get("instances", []):
                if instance_assignment.get(instance) != part:
                    raise ValueError(
                        "assignment cannot be packed: cluster is split"
                    )
                observed_instances += 1
            yield part_index[part]

    cluster_parts = _compress_indices(
        cluster_part_indices(), max(0, len(part_ids) - 1)
    )
    if len(instance_assignment) != observed_instances:
        raise ValueError("assignment cannot be packed: instance coverage differs")
    packed_partitions = []
    for partition in partitions:
        if not isinstance(partition, dict):
            raise ValueError("assignment cannot be packed: malformed partition")
        packed_partitions.append(
            {key: item for key, item in partition.items() if key != "clusters"}
        )
    semantic_contract = value.get("semantic_contract")
    compressed_semantic_contract = None
    if semantic_contract is not None:
        compressed_semantic_contract = _compress_json(semantic_contract)
    document = {
        key: item
        for key, item in value.items()
        if key
        not in {
            "cluster_assignment",
            "instance_assignment",
            "partitions",
            "semantic_contract",
        }
    }
    return {
        "schema": PACKED_ASSIGNMENT_SCHEMA,
        "document": document,
        "cluster_count": len(clusters),
        "cluster_ids": stored_cluster_ids,
        "part_ids": part_ids,
        "cluster_parts": cluster_parts,
        "partitions": packed_partitions,
        "semantic_contract": compressed_semantic_contract,
    }


def expand_phase3_assignment(
    value: Mapping[str, Any],
    path: Path,
    read_document: Callable[[Path], Dict[str, Any]],
) -> Dict[str, Any]:
    schema = value.get("schema")
    if schema not in {
        LEGACY_PACKED_ASSIGNMENT_SCHEMA,
        PACKED_ASSIGNMENT_SCHEMA,
    }:
        raise ValueError("not a packed Phase 3 assignment document")
    document = value.get("document")
    count = value.get("cluster_count")
    part_ids = value.get("part_ids")
    raw_cluster_parts = value.get("cluster_parts")
    partitions = value.get("partitions")
    compressed_semantic_contract = value.get("semantic_contract")
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
    if semantic_contract is not None:
        result["semantic_contract"] = semantic_contract
    return result


class IndexedInstanceAssignment(Mapping[str, str]):
    """Lazy instance-to-FPGA relation backed by assignment.sqlite."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.connection = sqlite3.connect(
            f"file:{self.path}?mode=ro&immutable=1",
            uri=True,
            check_same_thread=False,
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
        PACKED_ASSIGNMENT_SCHEMA,
    }:
        return value
    document = value.get("document")
    partitions = value.get("partitions")
    if not isinstance(document, dict) or not isinstance(partitions, list):
        raise ValueError("packed assignment downstream view is malformed")
    result = {**document, "partitions": partitions}
    compressed = value.get("semantic_contract")
    if compressed is not None:
        result["semantic_contract"] = _decompress_json(
            compressed,
            "packed assignment semantic contract",
        )
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
                if schema == PACKED_CLUSTERS_SCHEMA:
                    return expand_phase3_clusters(clusters_value)
                return clusters_value

            return expand_phase3_assignment(value, path, read_clusters)
        result["instance_assignment"] = IndexedInstanceAssignment(index_path)
    return result


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

    connection = sqlite3.connect(
        f"file:{index_path.resolve()}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        metadata = {
            str(row["key"]): json.loads(row["value"])
            for row in connection.execute("SELECT key,value FROM metadata")
        }
        if metadata.get("schema") != "emuflow.phase3-assignment-sqlite/v1":
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
            source_segments: Dict[str, List[str]] = {}
            all_source_segment_ids = set()
            for row in connection.execute(
                "SELECT net_id,segment_id FROM exact_cut_sources "
                "ORDER BY net_id,ordinal"
            ):
                net_id = str(row["net_id"])
                segment_id = str(row["segment_id"])
                source_segments.setdefault(net_id, []).append(segment_id)
                all_source_segment_ids.add(segment_id)
            capture_segments: Dict[str, List[str]] = {}
            for row in connection.execute(
                "SELECT source_cut_net,segment_id FROM exact_logic_segments "
                "ORDER BY source_cut_net,segment_id"
            ):
                segment_id = str(row["segment_id"])
                if segment_id not in all_source_segment_ids:
                    capture_segments.setdefault(
                        str(row["source_cut_net"]), []
                    ).append(segment_id)
            exact_nets = [
                str(row["net_id"])
                for row in connection.execute(
                    "SELECT net_id FROM exact_cut_nodes ORDER BY net_id"
                )
            ]
            if exact_nets != sorted(cut_records):
                raise ValueError(
                    "assignment index exact-cut coverage is inconsistent"
                )
            result["semantic_contract_sha256"] = exact_digest
            result["semantic_contract_index"] = {
                "cut_nodes": [
                    {
                        "net": net_id,
                        "source_segment_ids": source_segments.get(net_id, []),
                        "capture_segment_ids": capture_segments.get(net_id, []),
                    }
                    for net_id in exact_nets
                ]
            }
    finally:
        connection.close()

    if include_instance_assignment:
        result["instance_assignment"] = IndexedInstanceAssignment(index_path)
    return result
