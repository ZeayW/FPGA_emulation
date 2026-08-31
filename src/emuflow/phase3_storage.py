from __future__ import annotations

import base64
import json
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping


PACKED_CLUSTERS_SCHEMA = "emuflow.phase3-clusters-storage/v1"
PACKED_ASSIGNMENT_SCHEMA = "emuflow.phase3-assignment-storage/v1"
_COMPRESSED_JSON_CODEC = "zlib-level-1-base64-canonical-json"


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
    cluster_ids = [cluster.get("id") for cluster in clusters]
    if (
        not all(isinstance(item, str) for item in cluster_ids)
        or set(cluster_ids) != set(cluster_assignment)
    ):
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
    for cluster in clusters:
        part = cluster_assignment[cluster["id"]]
        for instance in cluster.get("instances", []):
            if instance_assignment.get(instance) != part:
                raise ValueError("assignment cannot be packed: cluster is split")
    expected_instances = {
        instance for cluster in clusters for instance in cluster.get("instances", [])
    }
    if set(instance_assignment) != expected_instances:
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
        encoded = json.dumps(
            semantic_contract,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        compressed_semantic_contract = {
            "codec": _COMPRESSED_JSON_CODEC,
            "uncompressed_bytes": len(encoded),
            "data": base64.b64encode(zlib.compress(encoded, level=1)).decode(
                "ascii"
            ),
        }
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
        "cluster_count": len(cluster_ids),
        "part_ids": part_ids,
        "cluster_parts": [
            part_index[cluster_assignment[cluster_id]] for cluster_id in cluster_ids
        ],
        "partitions": packed_partitions,
        "semantic_contract": compressed_semantic_contract,
    }


def expand_phase3_assignment(
    value: Mapping[str, Any],
    path: Path,
    read_document: Callable[[Path], Dict[str, Any]],
) -> Dict[str, Any]:
    if value.get("schema") != PACKED_ASSIGNMENT_SCHEMA:
        raise ValueError("not a packed Phase 3 assignment document")
    document = value.get("document")
    count = value.get("cluster_count")
    part_ids = value.get("part_ids")
    cluster_parts = value.get("cluster_parts")
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
        or not isinstance(cluster_parts, list)
        or len(cluster_parts) != count
        or not isinstance(partitions, list)
        or not all(isinstance(item, dict) for item in partitions)
    ):
        raise ValueError("packed assignment header is malformed")
    semantic_contract = None
    if compressed_semantic_contract is not None:
        if (
            not isinstance(compressed_semantic_contract, dict)
            or compressed_semantic_contract.get("codec")
            != _COMPRESSED_JSON_CODEC
            or isinstance(
                compressed_semantic_contract.get("uncompressed_bytes"), bool
            )
            or not isinstance(
                compressed_semantic_contract.get("uncompressed_bytes"), int
            )
            or compressed_semantic_contract["uncompressed_bytes"] < 0
            or not isinstance(compressed_semantic_contract.get("data"), str)
        ):
            raise ValueError("packed assignment semantic contract is malformed")
        expected_size = compressed_semantic_contract["uncompressed_bytes"]
        try:
            encoded = base64.b64decode(
                compressed_semantic_contract["data"], validate=True
            )
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
            semantic_contract = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, zlib.error, json.JSONDecodeError) as error:
            raise ValueError(
                "packed assignment semantic contract is corrupt"
            ) from error
        if not isinstance(semantic_contract, dict):
            raise ValueError("packed assignment semantic contract is not an object")
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
