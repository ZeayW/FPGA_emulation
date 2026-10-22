from __future__ import annotations

import fnmatch
import hashlib
import math
from array import array
from collections import defaultdict, deque
from collections.abc import Set as AbstractSet
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from .errors import ValidationError
from .io import read_json
from .ir import EmuIR
from .partition_limits import strict_balance_limits
from .platform import FpgaNode, Platform
from .resources import RESOURCE_FIELDS, ResourceVector
from .combinational_cut import (
    STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
    STATIC_EXACT_CANDIDATE_POLICIES,
    STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH,
    _build_combinational_cut_candidate_index,
)


CLUSTERS_SCHEMA = "emuflow.clusters/v1"
PARTITION_ASSIGNMENT_SCHEMA = "emuflow.partition-assignment/v1"
PARTITION_CONSTRAINTS_SCHEMA = "emuflow.partition-constraints/v1"
DEFAULT_BALANCE_DIMENSIONS = ("cells", "lut", "ff")
TRANSPORTED_CUT_CLASSES = {"register_output", "register_input"}
LEGAL_CUT_CLASSES = {*TRANSPORTED_CUT_CLASSES, "primary_input"}
REPLICATED_NET_CLASSES = {"clock", "reset", "primary_input"}
CUT_MODE_SEQUENTIAL_ONLY = "sequential-only"
CUT_MODE_STATIC_EXACT = "static-exact-combinational"
HARD_MACRO_RESOURCES = {
    "bram",
    "dsp",
    "carry",
    "bram18k",
    "uram288",
    "dsp48",
    "carry8",
}
FPGA_SELECTION_POLICIES = {"all", "explicit", "minimum-capacity"}


class _UnionFind:
    def __init__(self, size: int):
        if size < 0 or size >= 2**32:
            raise ValidationError(
                "partition union-find exceeds the uint32 identity domain"
            )
        # Phase 3 used to retain two Python-int lists alongside the instance
        # dictionaries and name index.  Four-byte parents and one-byte ranks
        # preserve the same union-by-rank policy while keeping the storage
        # proportional to the native integer identity domain.
        self.parent = array("I", range(size))
        self.rank = array("B", [0]) * size

    def find(self, item: int) -> int:
        parent = self.parent[item]
        while parent != self.parent[parent]:
            parent = self.parent[parent]
        while item != parent:
            next_item = self.parent[item]
            self.parent[item] = parent
            item = next_item
        return parent

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1


class _CompactClusterRecords(list):
    """Lazy v1 cluster records backed by packed ordinal columns.

    The public Phase 3 contract deliberately remains a ``list`` so existing
    providers and the canonical JSON encoder keep the same behavior.  The
    base list is empty, however: production retains cell names once in a UTF-8
    pool, cluster membership as CSR ordinals, resource totals in uint32
    columns, and only the sparse fixed/group constraints.  Iteration expands
    one record at a time.
    """

    def __init__(
        self,
        *,
        name_bytes: bytearray,
        name_offsets: array,
        member_offsets: array,
        member_ordinals: array,
        resource_columns: Tuple[array, ...],
        fixed_by_cluster: Mapping[int, str],
        groups_by_cluster: Mapping[int, Sequence[str]],
        source_cluster_indices: array,
    ) -> None:
        super().__init__()
        cluster_count = len(member_offsets) - 1
        if (
            cluster_count < 0
            or len(name_offsets) != len(source_cluster_indices) + 1
            or len(member_ordinals) != len(source_cluster_indices)
            or len(resource_columns) != len(RESOURCE_FIELDS)
            or any(len(column) != cluster_count for column in resource_columns)
            or not member_offsets
            or member_offsets[0] != 0
            or member_offsets[-1] != len(member_ordinals)
        ):
            raise ValidationError("compact partition cluster columns are invalid")
        self._name_bytes = name_bytes
        self._name_offsets = name_offsets
        self._member_offsets = member_offsets
        self._member_ordinals = member_ordinals
        self._resource_columns = resource_columns
        self._fixed_by_cluster = dict(fixed_by_cluster)
        self._groups_by_cluster = {
            int(index): tuple(groups)
            for index, groups in groups_by_cluster.items()
        }
        self.source_cluster_indices = source_cluster_indices

    def __len__(self) -> int:
        return len(self._member_offsets) - 1

    def _name(self, ordinal: int) -> str:
        begin = self._name_offsets[ordinal]
        end = self._name_offsets[ordinal + 1]
        return self._name_bytes[begin:end].decode("utf-8")

    def _record(self, index: int) -> Dict[str, Any]:
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError("compact cluster index out of range")
        begin = self._member_offsets[index]
        end = self._member_offsets[index + 1]
        return {
            "id": f"c{index:06d}",
            "instances": [
                self._name(self._member_ordinals[offset])
                for offset in range(begin, end)
            ],
            "resources": {
                field: column[index]
                for field, column in zip(
                    RESOURCE_FIELDS, self._resource_columns
                )
                if column[index]
            },
            "fixed_fpga": self._fixed_by_cluster.get(index),
            "groups": list(self._groups_by_cluster.get(index, ())),
        }

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for index in range(len(self)):
            yield self._record(index)

    def cluster_member_count(self, index: int) -> int:
        if index < 0 or index >= len(self):
            raise IndexError("compact cluster index out of range")
        return self._member_offsets[index + 1] - self._member_offsets[index]

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, slice):
            return [self._record(item) for item in range(*index.indices(len(self)))]
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("compact cluster indices must be integers or slices")
        return self._record(index)

    def __contains__(self, item: object) -> bool:
        return any(record == item for record in self)

    def __reversed__(self) -> Iterator[Dict[str, Any]]:
        for index in range(len(self) - 1, -1, -1):
            yield self._record(index)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (list, tuple)):
            return list(self) == list(other)
        return False

    def __ne__(self, other: object) -> bool:
        return not self == other

    def copy(self) -> List[Dict[str, Any]]:
        return list(self)

    def __copy__(self) -> List[Dict[str, Any]]:
        return list(self)

    def __deepcopy__(self, memo: Dict[int, Any]) -> List[Dict[str, Any]]:
        import copy

        result = copy.deepcopy(list(self), memo)
        memo[id(self)] = result
        return result

    @property
    def packed_payload_bytes(self) -> int:
        columns = (
            self._name_offsets,
            self._member_offsets,
            self._member_ordinals,
            self.source_cluster_indices,
            *self._resource_columns,
        )
        return len(self._name_bytes) + sum(
            len(column) * column.itemsize for column in columns
        )

    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("compact cluster records are immutable")

    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable
    __delitem__ = _immutable
    __setitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable


class _CompactInstanceAssignment(Mapping[str, str]):
    """Bounded-memory instance assignment over compact EmuIR ordinals."""

    def __init__(
        self,
        path: Path,
        clusters: _CompactClusterRecords,
        cluster_assignment: Mapping[str, str],
    ) -> None:
        self.path = path
        self.part_ids = tuple(sorted(set(cluster_assignment.values())))
        part_index = {part: index for index, part in enumerate(self.part_ids)}
        self._cluster_parts = array("I")
        self._cluster_member_counts = array("I")
        for index in range(len(clusters)):
            cluster_id = f"c{index:06d}"
            member_count = clusters.cluster_member_count(index)
            if (
                cluster_id not in cluster_assignment
                or member_count >= 2**32
            ):
                raise ValidationError(
                    "compact partition cluster ownership is invalid"
                )
            self._cluster_parts.append(
                part_index[cluster_assignment[cluster_id]]
            )
            self._cluster_member_counts.append(member_count)
        self._source_clusters = clusters.source_cluster_indices
        if sum(self._cluster_member_counts) != len(self._source_clusters):
            raise ValidationError(
                "compact partition instance coverage is invalid"
            )
        if self._source_clusters and max(self._source_clusters) >= len(clusters):
            raise ValidationError(
                "compact partition source ownership is invalid"
            )

    def __len__(self) -> int:
        return len(self._source_clusters)

    def __iter__(self) -> Iterator[str]:
        from .emuir_storage import open_emuir_sqlite_read_only

        connection = open_emuir_sqlite_read_only(self.path)
        try:
            for row in connection.execute(
                "SELECT id FROM cell_records ORDER BY id"
            ):
                yield row["id"]
        finally:
            connection.close()

    def __getitem__(self, identity: str) -> str:
        from .emuir_storage import open_emuir_sqlite_read_only

        connection = open_emuir_sqlite_read_only(self.path)
        try:
            row = connection.execute(
                "SELECT seq FROM cell_records WHERE id=?", (identity,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError(identity)
        return self.part_for_source_seq(row["seq"])

    def values(self) -> Iterator[str]:
        for source_seq in range(len(self)):
            yield self.part_for_source_seq(source_seq)

    def items(self) -> Iterator[Tuple[str, str]]:
        for _, identity, part in self.iter_source_assignments(order_by_id=True):
            yield identity, part

    def part_for_source_seq(self, source_seq: int) -> str:
        if (
            isinstance(source_seq, bool)
            or not isinstance(source_seq, int)
            or source_seq < 0
            or source_seq >= len(self._source_clusters)
        ):
            raise ValidationError("compact assignment source sequence is invalid")
        cluster_index = self._source_clusters[source_seq]
        return self.part_ids[self._cluster_parts[cluster_index]]

    def cluster_part(self, cluster_index: int) -> str:
        return self.part_ids[self._cluster_parts[cluster_index]]

    def cluster_member_count(self, cluster_index: int) -> int:
        return self._cluster_member_counts[cluster_index]

    def iter_source_assignments(
        self, *, order_by_id: bool = False
    ) -> Iterator[Tuple[int, str, str]]:
        from .emuir_storage import open_emuir_sqlite_read_only

        connection = open_emuir_sqlite_read_only(self.path)
        try:
            order = "id" if order_by_id else "seq"
            for row in connection.execute(
                f"SELECT seq,id FROM cell_records ORDER BY {order}"
            ):
                source_seq = row["seq"]
                yield source_seq, row["id"], self.part_for_source_seq(source_seq)
        finally:
            connection.close()

    def canonical_sha256(self) -> str:
        """Hash the exact legacy pretty JSON without materializing it."""

        import json

        digest = hashlib.sha256()
        digest.update(b'{\n  "instance_assignment": {')
        first = True
        encoder = json.JSONEncoder(sort_keys=True)
        for _, identity, part in self.iter_source_assignments(order_by_id=True):
            digest.update(b"\n" if first else b",\n")
            first = False
            digest.update(b"    ")
            digest.update(encoder.encode(identity).encode("utf-8"))
            digest.update(b": ")
            digest.update(encoder.encode(part).encode("utf-8"))
        if first:
            digest.update(b"}")
        else:
            digest.update(b"\n  }")
        digest.update(b"\n}\n")
        return digest.hexdigest()


class _PackedClusterAssignment(Mapping[str, str]):
    """Canonical ``cN -> FPGA`` ownership in one fixed-width part column."""

    def __init__(
        self,
        part_ids: Sequence[str],
        part_indices: Iterable[int],
    ) -> None:
        self.part_ids = tuple(part_ids)
        if (
            not self.part_ids
            or len(set(self.part_ids)) != len(self.part_ids)
            or any(not isinstance(part, str) or not part for part in self.part_ids)
        ):
            raise ValidationError("packed cluster assignment parts are invalid")
        self.part_indices = array("I", part_indices)
        if self.part_indices and max(self.part_indices) >= len(self.part_ids):
            raise ValidationError(
                "packed cluster assignment part index is invalid"
            )

    @staticmethod
    def identity(index: int) -> str:
        return f"c{index:06d}"

    def _index(self, identity: str) -> int:
        if (
            not isinstance(identity, str)
            or not identity.startswith("c")
            or not identity[1:].isdigit()
        ):
            raise KeyError(identity)
        index = int(identity[1:])
        if (
            index < 0
            or index >= len(self.part_indices)
            or self.identity(index) != identity
        ):
            raise KeyError(identity)
        return index

    def __len__(self) -> int:
        return len(self.part_indices)

    def __iter__(self) -> Iterator[str]:
        for index in range(len(self)):
            yield self.identity(index)

    def __getitem__(self, identity: str) -> str:
        return self.part_ids[self.part_indices[self._index(identity)]]

    def part_for_index(self, index: int) -> str:
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or index >= len(self)
        ):
            raise IndexError("packed cluster index out of range")
        return self.part_ids[self.part_indices[index]]

    def values(self) -> Iterator[str]:
        for part_index in self.part_indices:
            yield self.part_ids[part_index]

    def items(self) -> Iterator[Tuple[str, str]]:
        for index, part_index in enumerate(self.part_indices):
            yield self.identity(index), self.part_ids[part_index]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _PackedClusterAssignment):
            if self.part_ids == other.part_ids:
                return self.part_indices == other.part_indices
        if not isinstance(other, Mapping) or len(self) != len(other):
            return False
        missing = object()
        return all(
            other.get(identity, missing) == part
            for identity, part in self.items()
        )

    def __ne__(self, other: object) -> bool:
        return not self == other

    @property
    def packed_payload_bytes(self) -> int:
        return len(self.part_indices) * self.part_indices.itemsize


def _instance_ids_on_net(net: Mapping[str, Any]) -> List[str]:
    return sorted(
        {
            endpoint["instance"]
            for collection in ("drivers", "sinks")
            for endpoint in net[collection]
            if endpoint["instance"] is not None
        }
    )


def _sum_resources(
    instance_ids: Iterable[str],
    instances: Mapping[str, Mapping[str, Any]],
) -> ResourceVector:
    return ResourceVector.sum(
        ResourceVector.from_mapping(instances[instance_id]["resources"])
        for instance_id in instance_ids
    )


def _compact_partition_ir_path(ir: Any) -> Optional[Path]:
    """Return the authoritative compact store for a production Phase 3 IR."""

    # Keep the reference in-memory EmuIR independent of the production SQLite
    # representation and do not trust an arbitrary object with a path field.
    from .partition_ir import CompactPartitionIR

    return ir.path if isinstance(ir, CompactPartitionIR) else None


def _ordered_partition_instance_ids(ir: Any) -> List[str]:
    """Return instance identities without decoding compact cell payloads."""

    path = _compact_partition_ir_path(ir)
    if path is None:
        return sorted(instance["id"] for instance in ir.value["instances"])

    from .emuir_storage import open_emuir_sqlite_read_only

    expected = len(ir.value["instances"])
    connection = open_emuir_sqlite_read_only(path)
    try:
        instance_ids = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM cell_records ORDER BY id"
            )
        ]
    finally:
        connection.close()
    if len(instance_ids) != expected:
        raise ValidationError("compact EmuIR cell identity set is incomplete")
    return instance_ids


def _build_clusters_from_compact_ir(
    ir: Any,
    constraints: Mapping[str, Any],
    released_combinational_nets: AbstractSet[str],
) -> List[Dict[str, Any]]:
    """Build canonical clusters from integer SQLite records.

    The compatibility implementation expands every cell and endpoint into
    dictionaries, then retains an instance dictionary, a name-to-index map,
    grouped member lists, and the final cluster payload at the same time.  A
    compact production EmuIR already owns dense cell ordinals.  This path maps
    those ordinals to the canonical lexical order once, performs connectivity
    union on integer endpoint cursors, and constructs each final record during
    a second ordered resource scan.  The emitted v1 schema and ordering remain
    byte-for-byte compatible with the reference implementation.
    """

    from .emuir_storage import _decode, open_emuir_sqlite_read_only

    path = _compact_partition_ir_path(ir)
    if path is None:
        raise ValidationError("compact cluster construction requires compact EmuIR")
    instance_count = len(ir.value["instances"])
    if instance_count >= 2**32:
        raise ValidationError(
            "partition instance count exceeds the uint32 identity domain"
        )
    missing = 2**32 - 1
    seq_to_ordinal = array("I", [missing]) * instance_count
    connection = open_emuir_sqlite_read_only(path)
    try:
        # Python's Unicode lexical order and SQLite BINARY UTF-8 order agree.
        # Preserve the former build_clusters ordering while retaining only the
        # four-byte original-seq -> lexical-ordinal permutation.
        cells = connection.execute(
            "SELECT seq FROM cell_records ORDER BY id"
        )
        cell_count = 0
        for ordinal, row in enumerate(cells):
            seq = row["seq"]
            if (
                isinstance(seq, bool)
                or not isinstance(seq, int)
                or seq < 0
                or seq >= instance_count
                or seq_to_ordinal[seq] != missing
            ):
                raise ValidationError("compact EmuIR cell sequence is invalid")
            seq_to_ordinal[seq] = ordinal
            cell_count += 1
        if cell_count != instance_count or missing in seq_to_ordinal:
            raise ValidationError("compact EmuIR cell sequence is incomplete")

        union_find = _UnionFind(instance_count)

        def instance_ordinal(instance_id: str) -> int:
            row = connection.execute(
                "SELECT seq FROM cell_records WHERE id=?", (instance_id,)
            ).fetchone()
            if row is None:
                raise ValidationError(
                    f"partition constraint references absent instance {instance_id!r}"
                )
            seq = row["seq"]
            if seq < 0 or seq >= instance_count:
                raise ValidationError("compact EmuIR cell sequence is invalid")
            ordinal = seq_to_ordinal[seq]
            if ordinal == missing:
                raise ValidationError("compact EmuIR cell sequence is incomplete")
            return ordinal

        group_ordinals: List[array] = []
        for group in constraints["groups"]:
            ordinals = array(
                "I", (instance_ordinal(item) for item in group["instances"])
            )
            group_ordinals.append(ordinals)
            first = ordinals[0]
            for ordinal in ordinals[1:]:
                union_find.union(first, ordinal)

        endpoint_rows = iter(
            connection.execute(
                """SELECT net_seq,role,instance_seq
                     FROM endpoint_records
                    ORDER BY net_seq,role,ordinal"""
            )
        )
        endpoint = next(endpoint_rows, None)
        net_count = 0
        for net in connection.execute(
            "SELECT seq,id,cut_class FROM net_records ORDER BY seq"
        ):
            net_seq = net["seq"]
            if net_seq != net_count:
                raise ValidationError("compact EmuIR net sequence is invalid")
            net_count += 1
            if endpoint is not None and endpoint["net_seq"] < net_seq:
                raise ValidationError("compact EmuIR has orphaned endpoints")
            members: Set[int] = set()
            while endpoint is not None and endpoint["net_seq"] == net_seq:
                if endpoint["role"] not in (0, 1):
                    raise ValidationError("compact EmuIR endpoint role is invalid")
                instance_seq = endpoint["instance_seq"]
                if instance_seq is not None:
                    if instance_seq < 0 or instance_seq >= instance_count:
                        raise ValidationError(
                            "compact EmuIR endpoint owner is invalid"
                        )
                    ordinal = seq_to_ordinal[instance_seq]
                    if ordinal == missing:
                        raise ValidationError(
                            "compact EmuIR endpoint owner is invalid"
                        )
                    members.add(ordinal)
                endpoint = next(endpoint_rows, None)
            if len(members) < 2:
                continue
            ordinal_membership = getattr(
                released_combinational_nets, "contains_ordinal", None
            )
            released = (
                ordinal_membership(net_seq)
                if ordinal_membership is not None
                else net["id"] in released_combinational_nets
            )
            if (
                net["cut_class"]
                not in LEGAL_CUT_CLASSES | REPLICATED_NET_CLASSES
                and not released
            ):
                member_iter = iter(members)
                first = next(member_iter)
                for ordinal in member_iter:
                    union_find.union(first, ordinal)
        if net_count != len(ir.value["nets"]):
            raise ValidationError("compact EmuIR net sequence is incomplete")
        if endpoint is not None:
            raise ValidationError("compact EmuIR has orphaned endpoints")

        # Build packed authoritative columns in lexical cell order.  The first
        # member of each component therefore establishes the same cluster
        # ordering as sorting all grouped member lists.  Unlike the former
        # implementation, this does not retain one dictionary, list and set of
        # Python strings for every cluster.
        root_to_cluster = array("I", [missing]) * instance_count
        lexical_clusters = array("I")
        member_counts = array("I")
        first_member_ordinals = array("I")
        name_bytes = bytearray()
        name_offsets = array("Q", [0])
        resource_columns = tuple(array("I") for _ in RESOURCE_FIELDS)
        resources_by_payload: Dict[int, ResourceVector] = {}
        cells = connection.execute(
            """SELECT id,resources_seq
                 FROM cell_records
                ORDER BY id"""
        )
        resource_cell_count = 0
        for ordinal, row in enumerate(cells):
            if ordinal >= instance_count:
                raise ValidationError(
                    "compact EmuIR resource cell sequence is invalid"
                )
            resource_cell_count += 1
            instance_id = row["id"]
            root = union_find.find(ordinal)
            cluster_index = root_to_cluster[root]
            if cluster_index == missing:
                cluster_index = len(member_counts)
                root_to_cluster[root] = cluster_index
                member_counts.append(0)
                first_member_ordinals.append(ordinal)
                for column in resource_columns:
                    column.append(0)
            lexical_clusters.append(cluster_index)
            member_counts[cluster_index] += 1
            encoded_name = instance_id.encode("utf-8")
            name_bytes.extend(encoded_name)
            name_offsets.append(len(name_bytes))
            payload_seq = row["resources_seq"]
            if payload_seq is None:
                raise ValidationError(
                    f"instance {instance_id!r} resource payload is missing"
                )
            resources = resources_by_payload.get(payload_seq)
            if resources is None:
                payload = connection.execute(
                    "SELECT value FROM payloads WHERE seq=?", (payload_seq,)
                ).fetchone()
                if payload is None or payload["value"] is None:
                    raise ValidationError(
                        f"instance {instance_id!r} resource payload is missing"
                    )
                resources_value = _decode(payload["value"], "resources")
                if not isinstance(resources_value, Mapping):
                    raise ValidationError(
                        f"instance {instance_id!r} resources: expected an object"
                    )
                resources = ResourceVector.from_mapping(
                    resources_value,
                    f"instance {instance_id!r} resources",
                )
                resources_by_payload[payload_seq] = resources
            for field_index, field in enumerate(RESOURCE_FIELDS):
                amount = getattr(resources, field)
                if amount:
                    total = resource_columns[field_index][cluster_index] + amount
                    if total >= 2**32:
                        raise ValidationError(
                            "compact partition cluster resource exceeds uint32"
                        )
                    resource_columns[field_index][cluster_index] = total
        if resource_cell_count != instance_count:
            raise ValidationError(
                "compact EmuIR resource cell sequence is incomplete"
            )

        fixed_by_cluster: Dict[int, Set[str]] = defaultdict(set)
        for item in constraints["fixed"]:
            ordinal = instance_ordinal(item["instance"])
            cluster_index = root_to_cluster[union_find.find(ordinal)]
            fixed_by_cluster[cluster_index].add(item["fpga"])
        for cluster_index, fixed_fpgas in fixed_by_cluster.items():
            if len(fixed_fpgas) > 1:
                member_ordinal = first_member_ordinals[cluster_index]
                begin = name_offsets[member_ordinal]
                end = name_offsets[member_ordinal + 1]
                first_member = name_bytes[begin:end].decode("utf-8")
                raise ValidationError(
                    f"cluster containing {first_member!r} has conflicting fixed "
                    f"FPGA constraints {sorted(fixed_fpgas)}"
                )

        groups_by_cluster: Dict[int, Set[str]] = defaultdict(set)
        for group, ordinals in zip(constraints["groups"], group_ordinals):
            for ordinal in ordinals:
                groups_by_cluster[
                    root_to_cluster[union_find.find(ordinal)]
                ].add(group["id"])
        member_offsets = array("I", [0])
        member_total = 0
        for count in member_counts:
            member_total += count
            member_offsets.append(member_total)
        if member_total != instance_count:
            raise ValidationError("compact partition cluster membership is incomplete")
        next_member_offsets = array("I", member_offsets[:-1])
        member_ordinals = array("I", [0]) * instance_count
        for ordinal, cluster_index in enumerate(lexical_clusters):
            offset = next_member_offsets[cluster_index]
            member_ordinals[offset] = ordinal
            next_member_offsets[cluster_index] = offset + 1
        source_cluster_indices = array(
            "I",
            (
                root_to_cluster[union_find.find(seq_to_ordinal[source_seq])]
                for source_seq in range(instance_count)
            ),
        )
        return _CompactClusterRecords(
            name_bytes=name_bytes,
            name_offsets=name_offsets,
            member_offsets=member_offsets,
            member_ordinals=member_ordinals,
            resource_columns=resource_columns,
            fixed_by_cluster={
                index: next(iter(values))
                for index, values in fixed_by_cluster.items()
            },
            groups_by_cluster={
                index: sorted(values)
                for index, values in groups_by_cluster.items()
            },
            source_cluster_indices=source_cluster_indices,
        )
    finally:
        connection.close()


def _expand_instance_patterns(
    patterns: Sequence[str],
    instance_ids: Sequence[str],
    context: str,
) -> List[str]:
    matches: Set[str] = set()
    for pattern_index, pattern in enumerate(patterns):
        if not isinstance(pattern, str) or not pattern:
            raise ValidationError(
                f"{context}[{pattern_index}]: expected a non-empty string"
            )
        pattern_matches = [
            instance_id
            for instance_id in instance_ids
            if fnmatch.fnmatchcase(instance_id, pattern)
        ]
        if not pattern_matches:
            raise ValidationError(
                f"{context}[{pattern_index}]: pattern {pattern!r} matched no instances"
            )
        matches.update(pattern_matches)
    return sorted(matches)


def normalize_partition_constraints(
    value: Optional[Mapping[str, Any]],
    ir: EmuIR,
    platform: Platform,
    min_used_fpgas: Optional[int] = None,
    balance_tolerance: Optional[float] = None,
) -> Dict[str, Any]:
    raw: Mapping[str, Any] = value or {}
    if raw and raw.get("schema") != PARTITION_CONSTRAINTS_SCHEMA:
        raise ValidationError(
            "constraints.schema: expected "
            f"{PARTITION_CONSTRAINTS_SCHEMA!r}, got {raw.get('schema')!r}"
        )

    # Most production runs have no instance-level group or fixed constraints.
    # Do not build and then destroy a million-entry Python list plus set in
    # that common case.  On compact EmuIR, the uncommon constrained path reads
    # only the indexed identity column instead of decoding resource payloads.
    instance_ids: Optional[List[str]] = None
    instance_set: Optional[Set[str]] = None

    def instance_catalog() -> Tuple[List[str], Set[str]]:
        nonlocal instance_ids, instance_set
        if instance_ids is None:
            instance_ids = _ordered_partition_instance_ids(ir)
            instance_set = set(instance_ids)
        assert instance_set is not None
        return instance_ids, instance_set

    fpga_ids = {fpga.id for fpga in platform.fpgas}

    raw_groups = raw.get("groups", [])
    if not isinstance(raw_groups, list):
        raise ValidationError("constraints.groups: expected an array")
    groups: List[Dict[str, Any]] = []
    group_ids: Set[str] = set()
    for index, item in enumerate(raw_groups):
        if not isinstance(item, dict):
            raise ValidationError(f"constraints.groups[{index}]: expected an object")
        group_id = item.get("id")
        if not isinstance(group_id, str) or not group_id:
            raise ValidationError(
                f"constraints.groups[{index}].id: expected a non-empty string"
            )
        if group_id in group_ids:
            raise ValidationError(
                f"constraints.groups[{index}].id: duplicate {group_id!r}"
            )
        group_ids.add(group_id)
        raw_instances = item.get("instances", [])
        raw_patterns = item.get("patterns", [])
        if not isinstance(raw_instances, list) or not all(
            isinstance(instance_id, str) for instance_id in raw_instances
        ):
            raise ValidationError(
                f"constraints.groups[{index}].instances: expected strings"
            )
        if not isinstance(raw_patterns, list):
            raise ValidationError(
                f"constraints.groups[{index}].patterns: expected an array"
            )
        all_instance_ids, known_instance_ids = instance_catalog()
        unknown = sorted(set(raw_instances) - known_instance_ids)
        if unknown:
            raise ValidationError(
                f"constraints.groups[{index}].instances: unknown instances {unknown}"
            )
        expanded = set(raw_instances)
        expanded.update(
            _expand_instance_patterns(
                raw_patterns,
                all_instance_ids,
                f"constraints.groups[{index}].patterns",
            )
        )
        if not expanded:
            raise ValidationError(
                f"constraints.groups[{index}]: expected at least one instance"
            )
        groups.append({"id": group_id, "instances": sorted(expanded)})

    raw_fixed = raw.get("fixed", [])
    if not isinstance(raw_fixed, list):
        raise ValidationError("constraints.fixed: expected an array")
    fixed_by_instance: Dict[str, str] = {}
    fixed: List[Dict[str, str]] = []
    for index, item in enumerate(raw_fixed):
        if not isinstance(item, dict):
            raise ValidationError(f"constraints.fixed[{index}]: expected an object")
        fpga_id = item.get("fpga")
        if fpga_id not in fpga_ids:
            raise ValidationError(
                f"constraints.fixed[{index}].fpga: unknown FPGA {fpga_id!r}"
            )
        raw_instances = item.get("instances", [])
        if "instance" in item:
            raw_instances = list(raw_instances) + [item.get("instance")]
        raw_patterns = item.get("patterns", [])
        if not isinstance(raw_instances, list) or not all(
            isinstance(instance_id, str) for instance_id in raw_instances
        ):
            raise ValidationError(
                f"constraints.fixed[{index}].instances: expected strings"
            )
        if not isinstance(raw_patterns, list):
            raise ValidationError(
                f"constraints.fixed[{index}].patterns: expected an array"
            )
        all_instance_ids, known_instance_ids = instance_catalog()
        unknown = sorted(set(raw_instances) - known_instance_ids)
        if unknown:
            raise ValidationError(
                f"constraints.fixed[{index}].instances: unknown instances {unknown}"
            )
        expanded = set(raw_instances)
        expanded.update(
            _expand_instance_patterns(
                raw_patterns,
                all_instance_ids,
                f"constraints.fixed[{index}].patterns",
            )
        )
        if not expanded:
            raise ValidationError(
                f"constraints.fixed[{index}]: expected at least one instance"
            )
        for instance_id in sorted(expanded):
            previous = fixed_by_instance.get(instance_id)
            if previous is not None and previous != fpga_id:
                raise ValidationError(
                    f"constraints.fixed: instance {instance_id!r} is fixed to "
                    f"both {previous!r} and {fpga_id!r}"
                )
            fixed_by_instance[instance_id] = fpga_id
            fixed.append({"instance": instance_id, "fpga": fpga_id})

    requested_min_used = (
        min_used_fpgas
        if min_used_fpgas is not None
        else raw.get("min_used_fpgas")
    )
    minimum_selection_count = (
        requested_min_used
        if (
            not isinstance(requested_min_used, bool)
            and isinstance(requested_min_used, int)
            and requested_min_used > 0
        )
        else 1
    )
    selection_policy = raw.get("fpga_selection_policy", "all")
    if selection_policy not in FPGA_SELECTION_POLICIES:
        raise ValidationError(
            "constraints.fpga_selection_policy: expected one of "
            f"{sorted(FPGA_SELECTION_POLICIES)}"
        )

    platform_order = {
        fpga.id: index for index, fpga in enumerate(platform.fpgas)
    }
    directed_neighbors: Dict[str, Set[str]] = {
        fpga.id: set() for fpga in platform.fpgas
    }
    direct_capacity: Dict[Tuple[str, str], int] = defaultdict(int)
    direct_latency: Dict[Tuple[str, str], int] = {}
    for link in platform.links:
        left, right = link.endpoints
        directions = [(left, right)]
        if link.direction in {"full_duplex", "half_duplex"}:
            directions.append((right, left))
        for source, sink in directions:
            directed_neighbors[source].add(sink)
            key = (source, sink)
            direct_capacity[key] += (
                link.transport_bits_per_cycle_per_direction
            )
            direct_latency[key] = min(
                direct_latency.get(key, link.latency_cycles),
                link.latency_cycles,
            )

    boundary_egress_capacity = {
        fpga.id: sum(
            direct_capacity.get((fpga.id, sink.id), 0)
            for sink in platform.fpgas
            if sink.id != fpga.id
        )
        for fpga in platform.fpgas
    }
    boundary_ingress_capacity = {
        fpga.id: sum(
            direct_capacity.get((source.id, fpga.id), 0)
            for source in platform.fpgas
            if source.id != fpga.id
        )
        for fpga in platform.fpgas
    }

    def topology_score(candidate: Sequence[FpgaNode]) -> Tuple[Any, ...]:
        """Rank capacity-equivalent active subsets by communication quality.

        Inactive FPGAs remain legal transit nodes, so shortest paths are
        measured on the complete platform.  Directly connected, high-capacity
        subsets nevertheless win deterministic ties.  This prevents a
        homogeneous platform from blindly selecting the first N FPGAs even
        when those FPGAs communicate only through avoidable transit nodes.
        """

        candidate_ids = tuple(fpga.id for fpga in candidate)
        distances: Dict[str, Dict[str, int]] = {}
        for source in candidate_ids:
            by_sink = {source: 0}
            pending = deque([source])
            while pending:
                current = pending.popleft()
                for sink in sorted(
                    directed_neighbors[current], key=platform_order.get
                ):
                    if sink in by_sink:
                        continue
                    by_sink[sink] = by_sink[current] + 1
                    pending.append(sink)
            distances[source] = by_sink

        ordered_pairs = [
            (source, sink)
            for source in candidate_ids
            for sink in candidate_ids
            if source != sink
        ]
        unreachable = sum(
            sink not in distances[source] for source, sink in ordered_pairs
        )
        hops = [
            distances[source][sink]
            for source, sink in ordered_pairs
            if sink in distances[source]
        ]
        maximum_hops = max(hops, default=0)
        total_hops = sum(hops)
        aggregate_direct_capacity = sum(
            direct_capacity.get((source, sink), 0)
            for source, sink in ordered_pairs
        )
        aggregate_direct_latency = sum(
            direct_latency.get((source, sink), 0)
            for source, sink in ordered_pairs
            if (source, sink) in direct_latency
        )
        # Inactive devices remain legal relays, but every transported bit must
        # first leave its source FPGA and finally enter its sink FPGA.  A
        # direct-pair-only score therefore misses the exact single-node cuts
        # that bound a routable partition on an asymmetric platform.  Prefer
        # subsets whose weakest active endpoint has the most ingress/egress
        # headroom before considering direct-pair bandwidth.  This is a
        # topology property only: it does not inspect a partition, timing
        # schedule, or application-specific communication demand.
        endpoint_boundaries = [
            capacity
            for fpga_id in candidate_ids
            for capacity in (
                boundary_egress_capacity[fpga_id],
                boundary_ingress_capacity[fpga_id],
            )
        ]
        minimum_endpoint_boundary = min(endpoint_boundaries, default=0)
        aggregate_endpoint_boundary = sum(endpoint_boundaries)
        return (
            unreachable,
            maximum_hops,
            total_hops,
            -minimum_endpoint_boundary,
            -aggregate_endpoint_boundary,
            -aggregate_direct_capacity,
            aggregate_direct_latency,
            tuple(platform_order[item] for item in candidate_ids),
        )

    def best_capacity_subset(
        count: int,
        required_fixed: Set[str],
        total_resources: Mapping[str, int],
    ) -> Optional[Tuple[FpgaNode, ...]]:
        feasible = []
        for candidate in combinations(platform.fpgas, count):
            candidate_ids = {fpga.id for fpga in candidate}
            if not required_fixed <= candidate_ids:
                continue
            if not all(
                total_resources.get(field, 0)
                <= sum(
                    fpga.effective_capacity.get(field, 0)
                    for fpga in candidate
                )
                for field in RESOURCE_FIELDS
            ):
                continue
            feasible.append(candidate)
        if not feasible:
            return None
        return min(feasible, key=topology_score)

    def best_homogeneous_subset(
        count: int, required_fixed: Set[str]
    ) -> Tuple[FpgaNode, ...]:
        """Select a topology-aware homogeneous subset without N-choose-K work."""

        by_id = {fpga.id: fpga for fpga in platform.fpgas}
        if required_fixed:
            seeds = [set(required_fixed)]
        else:
            seeds = [{fpga.id} for fpga in platform.fpgas]
        completed = []
        for selected in seeds:
            while len(selected) < count:
                remaining = [
                    fpga
                    for fpga in platform.fpgas
                    if fpga.id not in selected
                ]
                best = min(
                    remaining,
                    key=lambda fpga: topology_score(
                        tuple(
                            by_id[fpga_id]
                            for fpga_id in sorted(
                                {*selected, fpga.id},
                                key=platform_order.get,
                            )
                        )
                    ),
                )
                selected.add(best.id)
            completed.append(
                tuple(
                    by_id[fpga_id]
                    for fpga_id in sorted(
                        selected, key=platform_order.get
                    )
                )
            )
        return min(completed, key=topology_score)

    def select_minimum_capacity_fpgas() -> List[str]:
        total_resources = ResourceVector.sum(
            ResourceVector.from_mapping(instance["resources"])
            for instance in ir.value["instances"]
        ).to_dict()
        required_fixed = set(fixed_by_instance.values())
        capacity_vectors = [
            tuple(
                fpga.effective_capacity.get(field, 0)
                for field in RESOURCE_FIELDS
            )
            for fpga in platform.fpgas
        ]
        if len(set(capacity_vectors)) == 1:
            per_fpga = dict(zip(RESOURCE_FIELDS, capacity_vectors[0]))
            required_count = max(
                (
                    math.ceil(total_resources.get(field, 0) / capacity)
                    if capacity > 0
                    else (
                        0
                        if total_resources.get(field, 0) == 0
                        else math.inf
                    )
                )
                for field, capacity in per_fpga.items()
            )
            count = max(
                minimum_selection_count,
                len(required_fixed),
                int(required_count) if math.isfinite(required_count) else 0,
            )
            if not math.isfinite(required_count) or count > len(platform.fpgas):
                raise ValidationError(
                    "constraints.fpga_selection_policy: design demand exceeds "
                    "the effective capacity of every supported active subset"
                )
            selected = best_homogeneous_subset(count, required_fixed)
            selected_set = {fpga.id for fpga in selected}
            return [
                fpga.id for fpga in platform.fpgas if fpga.id in selected_set
            ]
        for count in range(
            max(minimum_selection_count, len(required_fixed)),
            len(platform.fpgas) + 1,
        ):
            candidate = best_capacity_subset(
                count, required_fixed, total_resources
            )
            if candidate is not None:
                return [fpga.id for fpga in candidate]
        raise ValidationError(
            "constraints.fpga_selection_policy: design demand exceeds the "
            "effective capacity of every supported active subset"
        )

    raw_active = raw.get("active_fpgas")
    if raw_active is not None:
        if (
            not isinstance(raw_active, list)
            or not raw_active
            or not all(isinstance(item, str) and item for item in raw_active)
            or len(set(raw_active)) != len(raw_active)
        ):
            raise ValidationError(
                "constraints.active_fpgas: expected unique FPGA ids"
            )
        unknown_active = sorted(set(raw_active) - fpga_ids)
        if unknown_active:
            raise ValidationError(
                "constraints.active_fpgas: unknown FPGAs "
                f"{unknown_active}"
            )
        active_set = set(raw_active)
        active_fpgas = [
            fpga.id for fpga in platform.fpgas if fpga.id in active_set
        ]
        if selection_policy == "all" and active_set != fpga_ids:
            selection_policy = "explicit"
        elif selection_policy not in {
            "all",
            "explicit",
            "minimum-capacity",
        }:
            raise ValidationError(
                "constraints.active_fpgas has an invalid selection policy"
            )
        if (
            selection_policy == "minimum-capacity"
            and active_fpgas != select_minimum_capacity_fpgas()
        ):
            raise ValidationError(
                "constraints.active_fpgas disagrees with the sealed "
                "minimum-capacity selection"
            )
    elif selection_policy == "explicit":
        raise ValidationError(
            "constraints.fpga_selection_policy='explicit' requires active_fpgas"
        )
    elif selection_policy == "minimum-capacity":
        active_fpgas = select_minimum_capacity_fpgas()
    else:
        active_fpgas = [fpga.id for fpga in platform.fpgas]

    inactive_fixed = sorted(set(fixed_by_instance.values()) - set(active_fpgas))
    if inactive_fixed:
        raise ValidationError(
            "constraints.fixed references inactive FPGAs "
            f"{inactive_fixed}"
        )

    raw_min_used = (
        requested_min_used
        if requested_min_used is not None
        else len(active_fpgas)
    )
    if (
        isinstance(raw_min_used, bool)
        or not isinstance(raw_min_used, int)
        or raw_min_used <= 0
        or raw_min_used > len(active_fpgas)
    ):
        raise ValidationError(
            "constraints.min_used_fpgas: expected an integer between 1 and "
            f"{len(active_fpgas)} active FPGAs"
        )

    raw_tolerance = raw.get("balance_tolerance", 0.10)
    if balance_tolerance is not None:
        raw_tolerance = balance_tolerance
    if (
        isinstance(raw_tolerance, bool)
        or not isinstance(raw_tolerance, (int, float))
        or float(raw_tolerance) < 0.0
    ):
        raise ValidationError(
            "constraints.balance_tolerance: expected a non-negative number"
        )
    raw_dimension_tolerances = raw.get("balance_tolerance_by_dimension", {})
    if not isinstance(raw_dimension_tolerances, dict):
        raise ValidationError(
            "constraints.balance_tolerance_by_dimension: expected an object"
        )
    valid_dimensions = {"cells", *RESOURCE_FIELDS}
    unknown_dimensions = sorted(
        set(raw_dimension_tolerances) - valid_dimensions
    )
    if unknown_dimensions:
        raise ValidationError(
            "constraints.balance_tolerance_by_dimension: unknown dimensions "
            f"{unknown_dimensions}"
        )
    dimension_tolerances: Dict[str, float] = {}
    for dimension, value in raw_dimension_tolerances.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or float(value) < 0.0
        ):
            raise ValidationError(
                "constraints.balance_tolerance_by_dimension."
                f"{dimension}: expected a non-negative number"
            )
        dimension_tolerances[dimension] = float(value)

    raw_balance_dimensions = raw.get(
        "balance_dimensions", list(DEFAULT_BALANCE_DIMENSIONS)
    )
    if not isinstance(raw_balance_dimensions, list):
        raise ValidationError(
            "constraints.balance_dimensions: expected an array"
        )
    if any(
        not isinstance(dimension, str)
        for dimension in raw_balance_dimensions
    ):
        raise ValidationError(
            "constraints.balance_dimensions: expected dimension names"
        )
    unknown_balance_dimensions = sorted(
        set(raw_balance_dimensions) - valid_dimensions
    )
    if unknown_balance_dimensions:
        raise ValidationError(
            "constraints.balance_dimensions: unknown dimensions "
            f"{unknown_balance_dimensions}"
        )
    if len(set(raw_balance_dimensions)) != len(raw_balance_dimensions):
        raise ValidationError(
            "constraints.balance_dimensions: duplicate dimensions"
        )

    return {
        "schema": PARTITION_CONSTRAINTS_SCHEMA,
        "groups": groups,
        "fixed": sorted(
            fixed, key=lambda item: (item["instance"], item["fpga"])
        ),
        "fpga_selection_policy": selection_policy,
        "active_fpgas": active_fpgas,
        "min_used_fpgas": raw_min_used,
        "balance_tolerance": float(raw_tolerance),
        "balance_tolerance_by_dimension": dict(
            sorted(dimension_tolerances.items())
        ),
        "balance_dimensions": sorted(raw_balance_dimensions),
    }


def active_partition_fpgas(
    platform: Platform, constraints: Mapping[str, Any]
) -> List[FpgaNode]:
    """Return assignment targets while retaining the full routing fabric."""

    active_ids = constraints.get("active_fpgas")
    if active_ids is None:
        active_ids = [fpga.id for fpga in platform.fpgas]
    active_set = set(active_ids)
    result = [fpga for fpga in platform.fpgas if fpga.id in active_set]
    if len(result) != len(active_set) or not result:
        raise ValidationError("partition constraints contain invalid active FPGAs")
    return result


def load_partition_constraints(
    path: Optional[Path],
    ir: EmuIR,
    platform: Platform,
    min_used_fpgas: Optional[int] = None,
    balance_tolerance: Optional[float] = None,
) -> Dict[str, Any]:
    value = read_json(path) if path is not None else None
    return normalize_partition_constraints(
        value,
        ir,
        platform,
        min_used_fpgas=min_used_fpgas,
        balance_tolerance=balance_tolerance,
    )


def build_clusters(
    ir: EmuIR,
    constraints: Mapping[str, Any],
    cut_mode: str = CUT_MODE_SEQUENTIAL_ONLY,
    max_cross_fpga_dependency_depth: int = (
        STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH
    ),
    static_exact_candidate_policy: str = STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    *,
    _stream_eligible_policy_names: bool = False,
) -> Dict[str, Any]:
    if not isinstance(_stream_eligible_policy_names, bool):
        raise ValidationError("stream eligible policy names flag is invalid")
    if cut_mode not in {CUT_MODE_SEQUENTIAL_ONLY, CUT_MODE_STATIC_EXACT}:
        raise ValidationError(
            "partition cut mode must be 'sequential-only' or "
            "'static-exact-combinational'"
        )
    released_combinational_nets: AbstractSet[str] = set()
    candidate_index = None
    if cut_mode == CUT_MODE_STATIC_EXACT:
        if static_exact_candidate_policy not in STATIC_EXACT_CANDIDATE_POLICIES:
            raise ValidationError("unknown static exact candidate policy")
        if (
            isinstance(max_cross_fpga_dependency_depth, bool)
            or not isinstance(max_cross_fpga_dependency_depth, int)
            or max_cross_fpga_dependency_depth < 0
        ):
            raise ValidationError(
                "max_cross_fpga_dependency_depth must be non-negative "
                "(zero disables the policy limit)"
            )
        candidate_index = _build_combinational_cut_candidate_index(
            ir,
            include_dependency_levels=False,
            include_source_identity=False,
        )
        # Candidate depth is not intrinsic to a net.  A net deep in the
        # potential-cut DAG can still be the only transported boundary in its
        # cone and therefore have actual dependency depth one.  Release every
        # structurally legal candidate and reconstruct actual depth only after
        # the provider supplies an assignment.
        released_combinational_nets = candidate_index["eligible_ids"]
    compact_ir_path = _compact_partition_ir_path(ir)
    if compact_ir_path is not None:
        clusters = _build_clusters_from_compact_ir(
            ir,
            constraints,
            released_combinational_nets,
        )
        instance_count = len(ir.value["instances"])
        result = {
            "schema": CLUSTERS_SCHEMA,
            "design": ir.value["design"]["name"],
            "clusters": clusters,
            "instances": instance_count,
            "policy": {
                "legal_cut_classes": sorted(LEGAL_CUT_CLASSES),
                "replicated_net_classes": sorted(REPLICATED_NET_CLASSES),
                "hard_macro_resources": sorted(HARD_MACRO_RESOURCES),
                "hard_macro_granularity": "instance",
            },
        }
        if cut_mode == CUT_MODE_STATIC_EXACT:
            result["policy"].update(
                {
                    "cut_mode": cut_mode,
                    "max_cross_fpga_dependency_depth": (
                        max_cross_fpga_dependency_depth
                    ),
                    "eligible_combinational_cut_nets": (
                        released_combinational_nets.streaming_ordered_names()
                        if _stream_eligible_policy_names
                        and hasattr(
                            released_combinational_nets,
                            "streaming_ordered_names",
                        )
                        else released_combinational_nets.ordered_names()
                        if hasattr(
                            released_combinational_nets, "ordered_names"
                        )
                        else sorted(released_combinational_nets)
                    ),
                    "transported_cut_classes": sorted(
                        {*TRANSPORTED_CUT_CLASSES, "combinational"}
                    ),
                    "qualification": "structural-partition-legality",
                }
            )
            if (
                static_exact_candidate_policy
                == STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
            ):
                result["policy"]["candidate_selection_policy"] = (
                    static_exact_candidate_policy
                )
        return result
    instances = {
        instance["id"]: instance for instance in ir.value["instances"]
    }
    instance_ids = sorted(instances)
    index_by_id = {
        instance_id: index for index, instance_id in enumerate(instance_ids)
    }
    union_find = _UnionFind(len(instance_ids))

    for group in constraints["groups"]:
        members = group["instances"]
        for member in members[1:]:
            union_find.union(index_by_id[members[0]], index_by_id[member])

    for net in ir.value["nets"]:
        members = _instance_ids_on_net(net)
        if len(members) < 2:
            continue
        if (
            net["cut_class"] not in LEGAL_CUT_CLASSES | REPLICATED_NET_CLASSES
            and net["id"] not in released_combinational_nets
        ):
            for member in members[1:]:
                union_find.union(index_by_id[members[0]], index_by_id[member])
    members_by_root: Dict[int, List[str]] = defaultdict(list)
    for instance_id in instance_ids:
        members_by_root[union_find.find(index_by_id[instance_id])].append(
            instance_id
        )

    fixed_by_instance = {
        item["instance"]: item["fpga"] for item in constraints["fixed"]
    }
    groups_by_instance: Dict[str, List[str]] = defaultdict(list)
    for group in constraints["groups"]:
        for instance_id in group["instances"]:
            groups_by_instance[instance_id].append(group["id"])

    raw_clusters = sorted(
        (sorted(members) for members in members_by_root.values()),
        key=lambda members: members[0],
    )
    clusters: List[Dict[str, Any]] = []
    for index, members in enumerate(raw_clusters):
        fixed_fpgas = {
            fixed_by_instance[member]
            for member in members
            if member in fixed_by_instance
        }
        if len(fixed_fpgas) > 1:
            raise ValidationError(
                f"cluster containing {members[0]!r} has conflicting fixed FPGA "
                f"constraints {sorted(fixed_fpgas)}"
            )
        clusters.append(
            {
                "id": f"c{index:06d}",
                "instances": members,
                "resources": _sum_resources(members, instances).to_dict(
                    include_zeros=False
                ),
                "fixed_fpga": next(iter(fixed_fpgas), None),
                "groups": sorted(
                    {
                        group_id
                        for member in members
                        for group_id in groups_by_instance.get(member, [])
                    }
                ),
            }
        )

    result = {
        "schema": CLUSTERS_SCHEMA,
        "design": ir.value["design"]["name"],
        "clusters": clusters,
        "instances": len(instance_ids),
        "policy": {
            "legal_cut_classes": sorted(LEGAL_CUT_CLASSES),
            "replicated_net_classes": sorted(REPLICATED_NET_CLASSES),
            "hard_macro_resources": sorted(HARD_MACRO_RESOURCES),
            "hard_macro_granularity": "instance",
        },
    }
    if cut_mode == CUT_MODE_STATIC_EXACT:
        assert candidate_index is not None
        result["policy"].update(
            {
                "cut_mode": cut_mode,
                "max_cross_fpga_dependency_depth": (
                    max_cross_fpga_dependency_depth
                ),
                "eligible_combinational_cut_nets": (
                    released_combinational_nets.streaming_ordered_names()
                    if _stream_eligible_policy_names
                    and hasattr(
                        released_combinational_nets,
                        "streaming_ordered_names",
                    )
                    else released_combinational_nets.ordered_names()
                    if hasattr(released_combinational_nets, "ordered_names")
                    else sorted(released_combinational_nets)
                ),
                "transported_cut_classes": sorted(
                    {*TRANSPORTED_CUT_CLASSES, "combinational"}
                ),
                "qualification": "structural-partition-legality",
            }
        )
        if static_exact_candidate_policy == STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2:
            result["policy"]["candidate_selection_policy"] = (
                static_exact_candidate_policy
            )
    return result


def transported_cut_classes_for_clusters(
    clusters_artifact: Mapping[str, Any],
) -> Set[str]:
    raw = clusters_artifact.get("policy", {}).get("transported_cut_classes")
    if raw is None:
        return set(TRANSPORTED_CUT_CLASSES)
    if not isinstance(raw, list) or not all(
        isinstance(item, str) for item in raw
    ):
        raise ValidationError(
            "clusters.policy.transported_cut_classes: expected strings"
        )
    result = set(raw)
    allowed = {*TRANSPORTED_CUT_CLASSES, "combinational"}
    if not set(TRANSPORTED_CUT_CLASSES).issubset(result) or not result <= allowed:
        raise ValidationError(
            "clusters.policy.transported_cut_classes is not a supported policy"
        )
    return result


def _cluster_adjacency(
    ir: EmuIR,
    cluster_by_instance: Mapping[str, str],
    transported_cut_classes: Set[str],
) -> Dict[str, Dict[str, int]]:
    adjacency: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for net in ir.value["nets"]:
        if net["cut_class"] not in transported_cut_classes:
            continue
        driver_clusters = {
            cluster_by_instance[endpoint["instance"]]
            for endpoint in net["drivers"]
            if endpoint["instance"] is not None
        }
        sink_clusters = {
            cluster_by_instance[endpoint["instance"]]
            for endpoint in net["sinks"]
            if endpoint["instance"] is not None
        }
        for driver_cluster in driver_clusters:
            for sink_cluster in sink_clusters:
                if driver_cluster == sink_cluster:
                    continue
                adjacency[driver_cluster][sink_cluster] += 1
                adjacency[sink_cluster][driver_cluster] += 1
    return adjacency


def _cluster_transport_arcs(
    ir: EmuIR,
    cluster_by_instance: Mapping[str, str],
    transported_cut_classes: Set[str],
) -> Dict[str, Set[Tuple[str, bool]]]:
    """Return neighbor arcs and whether the keyed cluster drives the arc."""
    arcs: Dict[str, Set[Tuple[str, bool]]] = defaultdict(set)
    for net in ir.value["nets"]:
        if net["cut_class"] not in transported_cut_classes:
            continue
        driver_clusters = {
            cluster_by_instance[endpoint["instance"]]
            for endpoint in net["drivers"]
            if endpoint["instance"] is not None
        }
        sink_clusters = {
            cluster_by_instance[endpoint["instance"]]
            for endpoint in net["sinks"]
            if endpoint["instance"] is not None
        }
        for driver_cluster in driver_clusters:
            for sink_cluster in sink_clusters:
                if driver_cluster == sink_cluster:
                    continue
                arcs[driver_cluster].add((sink_cluster, True))
                arcs[sink_cluster].add((driver_cluster, False))
    return arcs


def _resource_add(
    left: Mapping[str, int], right: Mapping[str, int]
) -> Dict[str, int]:
    return {
        field: left.get(field, 0) + right.get(field, 0)
        for field in RESOURCE_FIELDS
    }


def _fits(resources: Mapping[str, int], capacity: Mapping[str, int]) -> bool:
    return all(resources.get(field, 0) <= limit for field, limit in capacity.items())


def _seeded_tie(seed: int, cluster_id: str) -> str:
    return hashlib.sha256(f"{seed}:{cluster_id}".encode("utf-8")).hexdigest()


def _partition_hop_distances(
    platform: Platform,
    route_constraints: Mapping[str, Any],
) -> Dict[str, Dict[str, Optional[int]]]:
    # Import locally because routing owns the canonical directed BoardDB graph
    # and imports the partition artifact schema at module initialization.
    from .routing import (
        SYSTEM_ROUTE_CONSTRAINTS_SCHEMA,
        build_directed_graph,
        normalize_route_constraints,
    )

    raw_constraints = dict(route_constraints)
    raw_constraints.setdefault("schema", SYSTEM_ROUTE_CONSTRAINTS_SCHEMA)
    normalized = normalize_route_constraints(raw_constraints, platform)
    adjacency, _, _ = build_directed_graph(platform, normalized)
    result: Dict[str, Dict[str, Optional[int]]] = {}
    for source in sorted(adjacency):
        distance = {source: 0}
        queue = deque([source])
        while queue:
            node = queue.popleft()
            for arc in adjacency[node]:
                neighbor = arc["to"]
                if neighbor in distance:
                    continue
                distance[neighbor] = distance[node] + 1
                queue.append(neighbor)
        result[source] = {
            sink: distance.get(sink) for sink in sorted(adjacency)
        }
    return result


def assign_clusters(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    seed: int = 0,
    route_constraints: Optional[Mapping[str, Any]] = None,
    *,
    _include_semantic_contract: bool = True,
) -> Dict[str, Any]:
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValidationError("partition seed: expected a non-negative integer")

    clusters = {
        cluster["id"]: cluster for cluster in clusters_artifact["clusters"]
    }
    if len(clusters) < constraints["min_used_fpgas"]:
        raise ValidationError(
            f"partitioning has {len(clusters)} atomic clusters but "
            f"{constraints['min_used_fpgas']} FPGAs must be used"
        )
    cluster_by_instance = {
        instance_id: cluster_id
        for cluster_id, cluster in clusters.items()
        for instance_id in cluster["instances"]
    }
    transported_cut_classes = transported_cut_classes_for_clusters(
        clusters_artifact
    )
    adjacency = _cluster_adjacency(
        ir, cluster_by_instance, transported_cut_classes
    )
    transport_arcs = _cluster_transport_arcs(
        ir, cluster_by_instance, transported_cut_classes
    )
    hop_limit = (
        route_constraints.get("max_route_hops")
        if route_constraints is not None
        else None
    )
    hop_distances = (
        _partition_hop_distances(platform, route_constraints)
        if hop_limit is not None
        else None
    )
    partition_fpgas = active_partition_fpgas(platform, constraints)
    fpga_ids = [fpga.id for fpga in partition_fpgas]
    effective_capacity = {
        fpga.id: fpga.effective_capacity for fpga in partition_fpgas
    }
    loads = {
        fpga_id: {field: 0 for field in RESOURCE_FIELDS}
        for fpga_id in fpga_ids
    }

    total_resources = ResourceVector.sum(
        ResourceVector.from_mapping(cluster["resources"])
        for cluster in clusters.values()
    ).to_dict()
    soft_caps: Dict[str, Dict[str, int]] = {
        fpga_id: {} for fpga_id in fpga_ids
    }
    tolerance = constraints["balance_tolerance"]
    dimension_tolerances = constraints.get(
        "balance_tolerance_by_dimension", {}
    )
    for field in RESOURCE_FIELDS:
        total = total_resources[field]
        capacities = {
            fpga_id: effective_capacity[fpga_id].get(field, 0)
            for fpga_id in fpga_ids
        }
        capacity_total = sum(capacities.values())
        if total == 0 or capacity_total == 0:
            continue
        for fpga_id in fpga_ids:
            proportional = total * capacities[fpga_id] / capacity_total
            field_tolerance = dimension_tolerances.get(field, tolerance)
            soft_caps[fpga_id][field] = min(
                capacities[fpga_id],
                math.floor(
                    proportional * (1.0 + field_tolerance) + 1e-9
                ),
            )

    def dominant_size(cluster: Mapping[str, Any]) -> float:
        resources = cluster["resources"]
        ratios = []
        for fpga_id in fpga_ids:
            capacity = effective_capacity[fpga_id]
            ratios.extend(
                resources.get(field, 0) / capacity[field]
                for field in capacity
                if capacity[field] > 0
            )
        return max(ratios, default=0.0)

    order = sorted(
        clusters,
        key=lambda cluster_id: (
            -sum(adjacency.get(cluster_id, {}).values())
            if hop_limit is not None
            else 0,
            -dominant_size(clusters[cluster_id]),
            -len(clusters[cluster_id]["instances"]),
            _seeded_tie(seed, cluster_id),
            cluster_id,
        ),
    )
    assignment: Dict[str, str] = {}

    def hop_legal(cluster_id: str, fpga_id: str) -> bool:
        if hop_limit is None or hop_distances is None:
            return True
        for neighbor, cluster_is_driver in transport_arcs.get(
            cluster_id, set()
        ):
            if neighbor not in assignment:
                continue
            if cluster_is_driver:
                distance = hop_distances[fpga_id][assignment[neighbor]]
            else:
                distance = hop_distances[assignment[neighbor]][fpga_id]
            if distance is None or distance > hop_limit:
                return False
        return True

    def place(cluster_id: str, fpga_id: str) -> None:
        cluster_resources = ResourceVector.from_mapping(
            clusters[cluster_id]["resources"]
        ).to_dict()
        projected = _resource_add(loads[fpga_id], cluster_resources)
        if not _fits(projected, effective_capacity[fpga_id]):
            raise ValidationError(
                f"cluster {cluster_id!r} does not fit FPGA {fpga_id!r}"
            )
        assignment[cluster_id] = fpga_id
        loads[fpga_id] = projected

    for cluster_id in order:
        fixed_fpga = clusters[cluster_id]["fixed_fpga"]
        if fixed_fpga is not None:
            place(cluster_id, fixed_fpga)

    populated = {fpga_id for fpga_id in assignment.values()}
    for fpga_id in fpga_ids:
        if len(populated) >= constraints["min_used_fpgas"]:
            break
        if fpga_id in populated:
            continue
        candidate = next(
            (
                cluster_id
                for cluster_id in order
                if cluster_id not in assignment
                and hop_legal(cluster_id, fpga_id)
                and _fits(
                    _resource_add(
                        loads[fpga_id],
                        ResourceVector.from_mapping(
                            clusters[cluster_id]["resources"]
                        ).to_dict(),
                    ),
                    effective_capacity[fpga_id],
                )
            ),
            None,
        )
        if candidate is None and hop_limit is not None:
            candidate = next(
                (
                    cluster_id
                    for cluster_id in order
                    if cluster_id not in assignment
                    and _fits(
                        _resource_add(
                            loads[fpga_id],
                            ResourceVector.from_mapping(
                                clusters[cluster_id]["resources"]
                            ).to_dict(),
                        ),
                        effective_capacity[fpga_id],
                    )
                ),
                None,
            )
        if candidate is None:
            raise ValidationError(
                f"cannot populate required FPGA {fpga_id!r} within capacity"
            )
        place(candidate, fpga_id)
        populated.add(fpga_id)

    for cluster_id in order:
        if cluster_id in assignment:
            continue
        resources = ResourceVector.from_mapping(
            clusters[cluster_id]["resources"]
        ).to_dict()
        capacity_candidates = [
            fpga_id
            for fpga_id in fpga_ids
            if _fits(
                _resource_add(loads[fpga_id], resources),
                effective_capacity[fpga_id],
            )
        ]
        if not capacity_candidates:
            raise ValidationError(
                f"cluster {cluster_id!r} cannot fit any FPGA; resources "
                f"{clusters[cluster_id]['resources']}"
            )
        topology_candidates = [
            fpga_id
            for fpga_id in capacity_candidates
            if hop_legal(cluster_id, fpga_id)
        ]
        actual_candidates = topology_candidates or capacity_candidates
        balanced_candidates = [
            fpga_id
            for fpga_id in actual_candidates
            if _fits(
                _resource_add(loads[fpga_id], resources),
                soft_caps[fpga_id],
            )
        ]
        candidates = balanced_candidates or actual_candidates

        def score(fpga_id: str) -> Tuple[float, float, float, str]:
            cut_cost = sum(
                weight
                for neighbor, weight in adjacency.get(cluster_id, {}).items()
                if neighbor in assignment and assignment[neighbor] != fpga_id
            )
            projected = _resource_add(loads[fpga_id], resources)
            ratios = [
                projected[field] / effective_capacity[fpga_id][field]
                for field in effective_capacity[fpga_id]
                if effective_capacity[fpga_id][field] > 0
            ]
            return (
                float(cut_cost),
                max(ratios, default=0.0),
                sum(ratios),
                fpga_id,
            )

        place(cluster_id, min(candidates, key=score))

    return build_partition_assignment(
        ir,
        platform,
        clusters_artifact,
        constraints,
        assignment,
        provider="deterministic-multiresource-greedy-v1",
        seed=seed,
        _include_semantic_contract=_include_semantic_contract,
    )


def build_partition_assignment(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    cluster_assignment: Mapping[str, str],
    provider: str,
    seed: int,
    provider_metadata: Optional[Mapping[str, Any]] = None,
    *,
    _include_semantic_contract: bool = True,
    _stream_semantic_records: bool = False,
) -> Dict[str, Any]:
    """Build the common Phase 3 assignment artifact for any provider."""

    cluster_records = clusters_artifact["clusters"]
    if not isinstance(cluster_records, list):
        raise ValidationError("partition clusters are incomplete")
    if len(cluster_assignment) != len(cluster_records) or any(
        cluster["id"] not in cluster_assignment for cluster in cluster_records
    ):
        expected = {cluster["id"] for cluster in cluster_records}
        actual = set(cluster_assignment)
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValidationError(
            "cluster assignment exact coverage failed; "
            f"missing={missing[:8]}, extra={extra[:8]}"
        )
    fpga_ids = {
        fpga.id for fpga in active_partition_fpgas(platform, constraints)
    }
    unknown_fpgas = sorted(set(cluster_assignment.values()) - fpga_ids)
    if unknown_fpgas:
        raise ValidationError(
            f"cluster assignment references unknown FPGAs {unknown_fpgas}"
        )

    compact_path = _compact_partition_ir_path(ir)
    compact_assignment = (
        _stream_semantic_records
        and compact_path is not None
        and isinstance(cluster_records, _CompactClusterRecords)
    )
    instance_assignment: Mapping[str, str]
    if compact_assignment:
        instance_assignment = _CompactInstanceAssignment(
            compact_path, cluster_records, cluster_assignment
        )
    else:
        instance_assignment = {
            instance_id: cluster_assignment[cluster["id"]]
            for cluster in cluster_records
            for instance_id in cluster["instances"]
        }
    cut_policy = clusters_artifact.get("policy", {})
    build_exact_contract = (
        cut_policy.get("cut_mode") == CUT_MODE_STATIC_EXACT
        and _include_semantic_contract
    )
    if build_exact_contract:
        cut_nets, cut_metrics, cut_net_ordinals = (
            _compute_cut_nets_with_ordinals(ir, instance_assignment)
        )
    else:
        cut_nets, cut_metrics = compute_cut_nets(ir, instance_assignment)
        cut_net_ordinals = None
    semantic_contract = None
    if build_exact_contract:
        from .combinational_cut import (
            build_static_exact_semantic_contract,
            semantic_contract_sha256,
        )

        semantic_contract = build_static_exact_semantic_contract(
            ir,
            platform.to_dict(),
            instance_assignment,
            cut_nets,
            max_dependency_depth=cut_policy[
                "max_cross_fpga_dependency_depth"
            ],
            candidate_selection_policy=cut_policy.get(
                "candidate_selection_policy",
                STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
            ),
            stream_records=_stream_semantic_records,
            cut_net_ordinals=cut_net_ordinals,
        )
        contract_nodes = semantic_contract["cut_nodes"]
        if len(contract_nodes) != len(cut_nets):
            raise ValidationError(
                "Static Exact contract cut coverage disagrees"
            )
        for cut, node in zip(cut_nets, contract_nodes):
            if cut["net"] != node["net"]:
                raise ValidationError(
                    "Static Exact contract cut order disagrees"
                )
            cut.update(
                {
                    "dependency_level": node["dependency_level"],
                    "combinational_dependency_depth": node[
                        "combinational_dependency_depth"
                    ],
                    "predecessor_cut_nets": node[
                        "predecessor_cut_nets"
                    ],
                }
            )
        cut_metrics.update(
            {
                "combinational_cut_nets": semantic_contract["metrics"][
                    "combinational_cut_nets"
                ],
                "maximum_combinational_dependency_depth": (
                    semantic_contract["metrics"][
                        "maximum_combinational_dependency_depth"
                    ]
                ),
            }
        )
        _remove_static_exact_global_rounds(cut_nets, cut_metrics)
    partition_state = {
        fpga.id: {
            "clusters": [],
            "instance_count": 0,
            "resources": {field: 0 for field in RESOURCE_FIELDS},
        }
        for fpga in platform.fpgas
    }
    for cluster in cluster_records:
        cluster_id = cluster["id"]
        assigned_fpga = cluster_assignment[cluster_id]
        state = partition_state[assigned_fpga]
        state["clusters"].append(cluster_id)
        state["instance_count"] += len(cluster["instances"])
        vector = ResourceVector.from_mapping(cluster["resources"])
        for field in RESOURCE_FIELDS:
            state["resources"][field] += getattr(vector, field)

    partition_records = []
    for fpga in platform.fpgas:
        state = partition_state[fpga.id]
        assigned_cluster_ids = state["clusters"]
        instance_count = state["instance_count"]
        resources = {
            field: amount
            for field, amount in state["resources"].items()
            if amount
        }
        if not ResourceVector.from_mapping(resources).fits_capacity(
            fpga.effective_capacity
        ):
            raise ValidationError(
                f"provider assignment exceeds effective capacity of {fpga.id!r}"
            )
        utilization = {
            field: resources.get(field, 0) / fpga.effective_capacity[field]
            for field in fpga.effective_capacity
            if fpga.effective_capacity[field] > 0
        }
        partition_records.append(
            {
                "fpga": fpga.id,
                "clusters": assigned_cluster_ids,
                "cluster_count": len(assigned_cluster_ids),
                "instance_count": instance_count,
                "resources": resources,
                "effective_capacity": dict(
                    sorted(fpga.effective_capacity.items())
                ),
                "utilization": dict(sorted(utilization.items())),
            }
        )

    cluster_assignment_is_canonical = (
        isinstance(cluster_assignment, (dict, _PackedClusterAssignment))
        and all(
            cluster_id == cluster["id"]
            for cluster_id, cluster in zip(
                cluster_assignment, cluster_records
            )
        )
    )
    stored_cluster_assignment = (
        cluster_assignment
        if cluster_assignment_is_canonical
        else dict(sorted(cluster_assignment.items()))
    )
    result: Dict[str, Any] = {
        "schema": PARTITION_ASSIGNMENT_SCHEMA,
        "design": ir.value["design"]["name"],
        "platform": platform.name,
        "provider": provider,
        "seed": seed,
        "constraints": dict(constraints),
        "cluster_assignment": stored_cluster_assignment,
        "instance_assignment": (
            instance_assignment
            if compact_assignment
            else dict(sorted(instance_assignment.items()))
        ),
        "partitions": partition_records,
        "cut_nets": cut_nets,
        "metrics": {
            "instances": len(instance_assignment),
            "clusters": len(cluster_records),
            "used_fpgas": sum(
                1 for record in partition_records if record["instance_count"]
            ),
            **cut_metrics,
        },
    }
    if provider_metadata is not None:
        result["provider_metadata"] = dict(provider_metadata)
    if semantic_contract is not None:
        result["semantic_contract"] = semantic_contract
        result["semantic_contract_sha256"] = semantic_contract_sha256(
            semantic_contract
        )
    return result


def validate_partition_artifacts_online(
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    assignment_artifact: Mapping[str, Any],
) -> Dict[str, Any]:
    """Check the final Phase-3 assignment contract without optimizer replay.

    Cluster construction and the Static Exact structural contract are validated
    by their owning code. This pass checks only assignment coverage, fixed and
    group constraints, capacity, balance, and the internally materialized
    cluster mapping.  It consumes the already-resident objects once.
    """

    if clusters_artifact.get("schema") != CLUSTERS_SCHEMA:
        raise ValidationError("invalid online clusters schema")
    if assignment_artifact.get("schema") != PARTITION_ASSIGNMENT_SCHEMA:
        raise ValidationError("invalid online assignment schema")
    if assignment_artifact.get("replication") is not None:
        raise ValidationError(
            "online assignment validation does not support replication"
        )
    raw_clusters = clusters_artifact.get("clusters")
    raw_assignment = assignment_artifact.get("instance_assignment")
    cluster_assignment = assignment_artifact.get("cluster_assignment")
    metrics = assignment_artifact.get("metrics")
    constraints = assignment_artifact.get("constraints")
    if (
        not isinstance(raw_clusters, list)
        or not isinstance(raw_assignment, Mapping)
        or not isinstance(cluster_assignment, Mapping)
        or not isinstance(metrics, dict)
        or not isinstance(constraints, dict)
    ):
        raise ValidationError("incomplete online partition artifact")

    fpga_by_id = {fpga.id: fpga for fpga in platform.fpgas}
    compact_assignment = isinstance(raw_assignment, _CompactInstanceAssignment)
    cluster_ids = set()
    instance_ids = set()
    resource_totals = {
        fpga_id: {field: 0 for field in RESOURCE_FIELDS}
        for fpga_id in fpga_by_id
    }
    expected_cluster_assignment = {}
    for index, cluster in enumerate(raw_clusters):
        if not isinstance(cluster, dict):
            raise ValidationError(f"clusters[{index}] is not an object")
        cluster_id = cluster.get("id")
        members = cluster.get("instances")
        resources = cluster.get("resources")
        if (
            not isinstance(cluster_id, str)
            or not cluster_id
            or (
                cluster_id != f"c{index:06d}"
                if compact_assignment
                else cluster_id in cluster_ids
            )
            or not isinstance(members, list)
            or not members
            or not all(isinstance(member, str) for member in members)
            or not isinstance(resources, dict)
        ):
            raise ValidationError(f"clusters[{index}] is malformed")
        if compact_assignment:
            if raw_assignment.cluster_member_count(index) != len(members):
                raise ValidationError(
                    "online compact cluster membership disagrees"
                )
            part = raw_assignment.cluster_part(index)
        else:
            duplicate_members = instance_ids & set(members)
            if duplicate_members:
                raise ValidationError(
                    "online clusters contain duplicate instances"
                )
            cluster_ids.add(cluster_id)
            instance_ids.update(members)
            assigned_parts = {raw_assignment.get(member) for member in members}
            if len(assigned_parts) != 1:
                raise ValidationError("online assignment splits a cluster")
            part = next(iter(assigned_parts))
        if part not in fpga_by_id:
            raise ValidationError("online assignment references an unknown FPGA")
        if cluster_assignment.get(cluster_id) != part:
            raise ValidationError(
                "online cluster assignment disagrees with instances"
            )
        if not compact_assignment:
            expected_cluster_assignment[cluster_id] = part
        vector = ResourceVector.from_mapping(resources)
        for field in RESOURCE_FIELDS:
            resource_totals[part][field] += getattr(vector, field)

    if compact_assignment:
        if (
            len(raw_assignment) != clusters_artifact.get("instances")
            or len(cluster_assignment) != len(raw_clusters)
        ):
            raise ValidationError(
                "online compact assignment coverage disagrees"
            )
    else:
        if set(raw_assignment) != instance_ids:
            raise ValidationError(
                "online assignment does not exactly cover instances"
            )
        if set(cluster_assignment) != cluster_ids:
            raise ValidationError(
                "online assignment does not exactly cover clusters"
            )
        if cluster_assignment != expected_cluster_assignment:
            raise ValidationError(
                "online cluster assignment disagrees with instances"
            )
    for fixed in constraints.get("fixed", []):
        if raw_assignment.get(fixed.get("instance")) != fixed.get("fpga"):
            raise ValidationError("online assignment violates a fixed instance")
    for group in constraints.get("groups", []):
        parts = {
            raw_assignment.get(instance)
            for instance in group.get("instances", [])
        }
        if len(parts) != 1 or next(iter(parts), None) not in fpga_by_id:
            raise ValidationError("online assignment splits a fixed group")
    resources_by_fpga = {
        fpga_id: ResourceVector.from_mapping(totals)
        for fpga_id, totals in resource_totals.items()
    }
    for fpga_id, resources in resources_by_fpga.items():
        if not resources.fits_capacity(fpga_by_id[fpga_id].effective_capacity):
            raise ValidationError("online assignment exceeds FPGA capacity")
    used_fpgas = len(set(cluster_assignment.values()))
    if used_fpgas < constraints.get("min_used_fpgas", 1):
        raise ValidationError("online assignment uses too few FPGAs")
    active_fpgas = constraints.get("active_fpgas")
    if active_fpgas is not None and not set(cluster_assignment.values()) <= set(
        active_fpgas
    ):
        raise ValidationError("online assignment uses an inactive FPGA")

    balance = validate_cluster_assignment_balance(
        platform,
        raw_clusters,
        cluster_assignment,
        constraints["balance_tolerance"],
        constraints.get("balance_tolerance_by_dimension", {}),
        constraints.get("active_fpgas"),
        constraints.get("balance_dimensions"),
    )
    result = {
        "status": "pass",
        "instances": len(raw_assignment),
        "clusters": len(raw_clusters),
        "used_fpgas": used_fpgas,
        "illegal_cuts": 0,
        **{
            key: value
            for key, value in metrics.items()
            if key not in {"instances", "clusters", "used_fpgas"}
        },
        **balance,
        "resources_by_fpga": {
            fpga_id: resources.to_dict(include_zeros=False)
            for fpga_id, resources in resources_by_fpga.items()
        },
    }
    semantic_contract = assignment_artifact.get("semantic_contract")
    if semantic_contract is not None:
        result.update(
            {
                "cut_mode": CUT_MODE_STATIC_EXACT,
                "qualification": "structural-partition-legality",
                "semantic_contract": {
                    "status": "pass",
                    **semantic_contract["metrics"],
                },
            }
        )
    return result


def compute_cut_nets(
    ir: EmuIR,
    instance_assignment: Mapping[str, str],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    cut_nets, metrics, _ = _compute_cut_nets_with_ordinals(
        ir, instance_assignment, retain_ordinals=False
    )
    return cut_nets, metrics


def _compute_cut_nets_with_ordinals(
    ir: EmuIR,
    instance_assignment: Mapping[str, str],
    *,
    retain_ordinals: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], array | None]:
    """Compute cuts and retain their authoritative EmuIR net coordinates.

    The public artifact deliberately continues to contain names only. The
    private ordinal column lives just long enough for Static Exact native input
    construction, where it avoids rebuilding a cut-name hash table to recover
    coordinates that this traversal already knew.
    """

    compact_path = _compact_partition_ir_path(ir)
    if compact_path is not None and isinstance(
        instance_assignment, _CompactInstanceAssignment
    ):
        return _compute_compact_cut_nets_with_ordinals(
            ir,
            instance_assignment,
            retain_ordinals=retain_ordinals,
        )

    cut_nets: List[Dict[str, Any]] = []
    cut_net_ordinals = array("I") if retain_ordinals else None
    replicated_primary_inputs = 0
    global_nets = 0
    cut_sink_endpoints = 0
    for net_ordinal, net in enumerate(ir.value["nets"]):
        driver_fpgas = sorted(
            {
                instance_assignment[endpoint["instance"]]
                for endpoint in net["drivers"]
                if endpoint["instance"] is not None
            }
        )
        sink_endpoints_by_fpga: Dict[str, int] = defaultdict(int)
        for endpoint in net["sinks"]:
            if endpoint["instance"] is not None:
                sink_endpoints_by_fpga[
                    instance_assignment[endpoint["instance"]]
                ] += 1
        sink_fpgas = sorted(sink_endpoints_by_fpga)
        instance_fpgas = set(driver_fpgas) | set(sink_fpgas)

        if net["cut_class"] in {"clock", "reset"}:
            if len(instance_fpgas) > 1:
                global_nets += 1
            continue
        # An undriven wire has no value-producing FPGA.  The splitter keeps
        # such a wire open in every partition that consumes it, preserving
        # the original four-state ``x`` semantics.  Treating its sinks as a
        # remote cut would invent a transport source and later make the exact
        # semantic contract demand a nonexistent source FPGA.
        if not driver_fpgas and net["cut_class"] == "undriven":
            continue
        if not driver_fpgas and net["cut_class"] == "primary_input":
            if len(sink_fpgas) > 1:
                replicated_primary_inputs += 1
            continue

        remote_sink_fpgas = sorted(
            fpga_id for fpga_id in sink_fpgas if fpga_id not in driver_fpgas
        )
        if not remote_sink_fpgas:
            continue
        remote_endpoints = sum(
            sink_endpoints_by_fpga[fpga_id] for fpga_id in remote_sink_fpgas
        )
        cut_sink_endpoints += remote_endpoints
        cut = {
            "net": net["id"],
            "cut_class": net["cut_class"],
            "source_fpgas": driver_fpgas,
            "sink_fpgas": remote_sink_fpgas,
            "sink_endpoints": remote_endpoints,
        }
        if net["cut_class"] == "register_input":
            # Round 0 distributes stable FF Q values. Round 1 starts only
            # after every round-0 arrival plus one fabric settle cycle, so a
            # D/CE source can safely depend on any remote FF without copying
            # an enormous per-net transitive dependency list.
            cut["transport_round"] = 1
        cut_nets.append(cut)
        if cut_net_ordinals is not None:
            cut_net_ordinals.append(net_ordinal)

    metrics = {
        "cut_nets": len(cut_nets),
        "cut_sink_endpoints": cut_sink_endpoints,
        "replicated_primary_inputs": replicated_primary_inputs,
        "global_nets": global_nets,
    }
    register_input_cuts = sum(
        cut["cut_class"] == "register_input" for cut in cut_nets
    )
    if register_input_cuts:
        metrics.update(
            {
                "register_input_cut_nets": register_input_cuts,
                "transport_rounds": 2,
                "round_barriers": 1,
            }
        )
    if any(
        cut_nets[index - 1]["net"] >= cut_nets[index]["net"]
        for index in range(1, len(cut_nets))
    ):
        order = sorted(
            range(len(cut_nets)), key=lambda index: cut_nets[index]["net"]
        )
        cut_nets = [cut_nets[index] for index in order]
        if cut_net_ordinals is not None:
            cut_net_ordinals = array(
                "I", (cut_net_ordinals[index] for index in order)
            )
    return cut_nets, metrics, cut_net_ordinals


def _compute_compact_cut_nets_with_ordinals(
    ir: Any,
    instance_assignment: _CompactInstanceAssignment,
    *,
    retain_ordinals: bool,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], array | None]:
    """Compute cuts from integer endpoint owners without instance-name maps."""

    from .emuir_storage import open_emuir_sqlite_read_only

    connection = open_emuir_sqlite_read_only(instance_assignment.path)
    cut_nets: List[Dict[str, Any]] = []
    cut_net_ordinals = array("I") if retain_ordinals else None
    replicated_primary_inputs = 0
    global_nets = 0
    cut_sink_endpoints = 0
    try:
        endpoint_rows = iter(
            connection.execute(
                """SELECT net_seq,role,instance_seq
                     FROM endpoint_records
                    ORDER BY net_seq,role,ordinal"""
            )
        )
        endpoint = next(endpoint_rows, None)
        observed_nets = 0
        for net in connection.execute(
            "SELECT seq,id,cut_class FROM net_records ORDER BY seq"
        ):
            net_seq = net["seq"]
            if net_seq != observed_nets:
                raise ValidationError("compact EmuIR net sequence is invalid")
            observed_nets += 1
            driver_parts: Set[str] = set()
            sink_counts: Dict[str, int] = defaultdict(int)
            while endpoint is not None and endpoint["net_seq"] == net_seq:
                role = endpoint["role"]
                source_seq = endpoint["instance_seq"]
                if role not in (0, 1):
                    raise ValidationError("compact EmuIR endpoint role is invalid")
                if source_seq is not None:
                    part = instance_assignment.part_for_source_seq(source_seq)
                    if role == 0:
                        driver_parts.add(part)
                    else:
                        sink_counts[part] += 1
                endpoint = next(endpoint_rows, None)
            if endpoint is not None and endpoint["net_seq"] < net_seq:
                raise ValidationError("compact EmuIR has orphaned endpoints")
            driver_fpgas = sorted(driver_parts)
            sink_fpgas = sorted(sink_counts)
            instance_fpgas = driver_parts | set(sink_counts)
            cut_class = net["cut_class"]
            if cut_class in {"clock", "reset"}:
                if len(instance_fpgas) > 1:
                    global_nets += 1
                continue
            if not driver_fpgas and cut_class == "undriven":
                continue
            if not driver_fpgas and cut_class == "primary_input":
                if len(sink_fpgas) > 1:
                    replicated_primary_inputs += 1
                continue
            remote_sink_fpgas = [
                part for part in sink_fpgas if part not in driver_parts
            ]
            if not remote_sink_fpgas:
                continue
            remote_endpoints = sum(
                sink_counts[part] for part in remote_sink_fpgas
            )
            cut_sink_endpoints += remote_endpoints
            cut = {
                "net": net["id"],
                "cut_class": cut_class,
                "source_fpgas": driver_fpgas,
                "sink_fpgas": remote_sink_fpgas,
                "sink_endpoints": remote_endpoints,
            }
            if cut_class == "register_input":
                cut["transport_round"] = 1
            cut_nets.append(cut)
            if cut_net_ordinals is not None:
                cut_net_ordinals.append(net_seq)
        if observed_nets != len(ir.value["nets"]) or endpoint is not None:
            raise ValidationError("compact EmuIR endpoint coverage is invalid")
    finally:
        connection.close()

    metrics = {
        "cut_nets": len(cut_nets),
        "cut_sink_endpoints": cut_sink_endpoints,
        "replicated_primary_inputs": replicated_primary_inputs,
        "global_nets": global_nets,
    }
    register_input_cuts = sum(
        cut["cut_class"] == "register_input" for cut in cut_nets
    )
    if register_input_cuts:
        metrics.update(
            {
                "register_input_cut_nets": register_input_cuts,
                "transport_rounds": 2,
                "round_barriers": 1,
            }
        )
    if any(
        cut_nets[index - 1]["net"] >= cut_nets[index]["net"]
        for index in range(1, len(cut_nets))
    ):
        order = sorted(range(len(cut_nets)), key=lambda index: cut_nets[index]["net"])
        cut_nets = [cut_nets[index] for index in order]
        if cut_net_ordinals is not None:
            cut_net_ordinals = array(
                "I", (cut_net_ordinals[index] for index in order)
            )
    return cut_nets, metrics, cut_net_ordinals


def _remove_static_exact_global_rounds(
    cut_nets: Sequence[Dict[str, Any]],
    metrics: Dict[str, Any],
) -> None:
    """Make the Static Exact dependency DAG the sole ordering contract.

    ``compute_cut_nets`` also serves the registered-boundary flow, where a
    two-round barrier is part of that flow's conservative transport model.
    Static Exact has an explicit, path-local dependency DAG instead.  Carrying
    the legacy round marker into its assignment would make downstream
    equivalence and scheduling reintroduce an unrelated global barrier.
    """

    for cut in cut_nets:
        cut.pop("transport_round", None)
    metrics.pop("transport_rounds", None)
    metrics.pop("round_barriers", None)
    metrics["transport_ordering_policy"] = "path-local-dependency-dag-v1"


def validate_cluster_assignment_balance(
    platform: Platform,
    clusters: Sequence[Mapping[str, Any]],
    cluster_assignment: Mapping[str, str],
    requested_tolerance: float,
    requested_tolerance_by_dimension: Optional[Mapping[str, float]] = None,
    active_fpgas: Optional[Sequence[str]] = None,
    requested_balance_dimensions: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    active_set = (
        set(active_fpgas)
        if active_fpgas is not None
        else {fpga.id for fpga in platform.fpgas}
    )
    partition_fpgas = [
        fpga for fpga in platform.fpgas if fpga.id in active_set
    ]
    if len(partition_fpgas) != len(active_set) or not partition_fpgas:
        raise ValidationError("balance active FPGA set is invalid")
    fpga_ids = [fpga.id for fpga in partition_fpgas]
    if len(cluster_assignment) != len(clusters):
        raise ValidationError("balance assignment cluster coverage changed")

    # Accumulate totals and assigned loads while each cluster record is live.
    # The former implementation retained a nested ``weights`` dictionary for
    # every cluster, then walked that graph a second time through the
    # assignment.  NVDLA has more than a million clusters, so this independent
    # validator duplicated the optimizer's largest string-keyed structure
    # after TritonPart had already finished.  All balance equations are sums;
    # no per-cluster weight is needed after its owning partition has been
    # checked.
    all_dimensions = ("cells", *RESOURCE_FIELDS)
    totals_all = {dimension: 0 for dimension in all_dimensions}
    loads_all = {
        fpga_id: {dimension: 0 for dimension in all_dimensions}
        for fpga_id in fpga_ids
    }
    resource_present = {field: False for field in RESOURCE_FIELDS}
    missing = object()
    for cluster in clusters:
        cluster_id = cluster["id"]
        fpga_id = cluster_assignment.get(cluster_id, missing)
        if fpga_id is missing:
            raise ValidationError(
                "balance assignment cluster coverage changed"
            )
        if fpga_id not in loads_all:
            raise ValidationError(
                f"balance assignment references unknown FPGA {fpga_id!r}"
            )
        cells = len(cluster["instances"])
        totals_all["cells"] += cells
        loads_all[fpga_id]["cells"] += cells
        resources = cluster["resources"]
        for field in RESOURCE_FIELDS:
            value = resources.get(field, 0)
            resource_present[field] = resource_present[field] or bool(value)
            totals_all[field] += value
            loads_all[fpga_id][field] += value

    dimensions = ["cells"]
    dimensions.extend(
        field
        for field in RESOURCE_FIELDS
        if resource_present[field]
        and all(
            fpga.effective_capacity.get(field, 0) > 0
            for fpga in partition_fpgas
        )
    )
    totals = {dimension: totals_all[dimension] for dimension in dimensions}
    tolerance_overrides = requested_tolerance_by_dimension or {}
    requested_percent_by_dimension = {
        dimension: float(tolerance_overrides.get(dimension, requested_tolerance)) * 100.0
        for dimension in dimensions
    }
    # Constraints are a contract, including when clusters are indivisible.
    # Infeasible assignments fail instead of changing the caller's tolerance.
    effective_percent_by_dimension = dict(requested_percent_by_dimension)
    strict_dimensions = (
        set(dimensions)
        if requested_balance_dimensions is None
        else set(requested_balance_dimensions) & set(dimensions)
    )
    limits = strict_balance_limits(
        partition_fpgas,
        totals,
        requested_tolerance,
        tolerance_overrides,
        strict_dimensions,
    )
    loads = {
        fpga_id: {
            dimension: loads_all[fpga_id][dimension]
            for dimension in dimensions
        }
        for fpga_id in fpga_ids
    }

    violations = []
    maximum_ratio = 0.0
    allowed_loads = {
        fpga_id: {dimension: 0.0 for dimension in dimensions}
        for fpga_id in fpga_ids
    }
    for fpga_id in fpga_ids:
        for dimension in dimensions:
            total = totals[dimension]
            if not total:
                continue
            actual_share = loads[fpga_id][dimension] / total
            allowed = limits[fpga_id][dimension]
            allowed_share = allowed / total
            allowed_loads[fpga_id][dimension] = allowed
            ratio = loads[fpga_id][dimension] / allowed if allowed else (
                math.inf if loads[fpga_id][dimension] else 0.0
            )
            maximum_ratio = max(maximum_ratio, ratio)
            if loads[fpga_id][dimension] > allowed:
                violations.append(
                    {
                        "fpga": fpga_id,
                        "dimension": dimension,
                        "bound": "upper",
                        "load": loads[fpga_id][dimension],
                        "total": total,
                        "actual_share": actual_share,
                        "allowed_share": allowed_share,
                        "constraint": (
                            "balance"
                            if dimension in strict_dimensions
                            else "capacity"
                        ),
                    }
                )
    if violations:
        raise ValidationError(
            "assignment violates effective multi-resource balance/capacity: "
            f"{violations[:8]}"
        )
    return {
        "requested_balance_percent": requested_tolerance * 100.0,
        "effective_balance_percent": max(
            effective_percent_by_dimension.values(), default=0.0
        ),
        "requested_balance_percent_by_dimension": (
            requested_percent_by_dimension
        ),
        "effective_balance_percent_by_dimension": (
            effective_percent_by_dimension
        ),
        "balance_auto_relaxed": False,
        "balance_dimensions": dimensions,
        "strict_balance_dimensions": sorted(strict_dimensions),
        "capacity_only_dimensions": sorted(
            set(dimensions) - strict_dimensions
        ),
        "constraint_mode_by_dimension": {
            dimension: (
                "balance" if dimension in strict_dimensions else "capacity"
            )
            for dimension in dimensions
        },
        "enforced_balance_percent_by_dimension": {
            dimension: requested_percent_by_dimension[dimension]
            for dimension in dimensions
            if dimension in strict_dimensions
        },
        "capacity_limits": {
            fpga.id: {
                dimension: (
                    totals[dimension]
                    if dimension == "cells"
                    else fpga.effective_capacity[dimension]
                )
                for dimension in dimensions
            }
            for fpga in partition_fpgas
        },
        "balance_loads": loads,
        "balance_allowed_loads": allowed_loads,
        "max_balance_limit_ratio": maximum_ratio,
        "balance_violations": 0,
        "strict_balance_violations": 0,
        "capacity_violations": 0,
    }


def validate_partition_artifacts(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    assignment_artifact: Mapping[str, Any],
) -> Dict[str, Any]:
    if clusters_artifact.get("schema") != CLUSTERS_SCHEMA:
        raise ValidationError(
            f"clusters.schema: expected {CLUSTERS_SCHEMA!r}, "
            f"got {clusters_artifact.get('schema')!r}"
        )
    if assignment_artifact.get("schema") != PARTITION_ASSIGNMENT_SCHEMA:
        raise ValidationError(
            f"assignment.schema: expected {PARTITION_ASSIGNMENT_SCHEMA!r}, "
            f"got {assignment_artifact.get('schema')!r}"
        )

    cut_policy = clusters_artifact.get("policy", {})
    cut_mode = cut_policy.get("cut_mode", CUT_MODE_SEQUENTIAL_ONLY)
    if cut_mode not in {CUT_MODE_SEQUENTIAL_ONLY, CUT_MODE_STATIC_EXACT}:
        raise ValidationError(f"clusters.policy.cut_mode: unknown {cut_mode!r}")
    if cut_mode == CUT_MODE_SEQUENTIAL_ONLY and any(
        key in cut_policy
        for key in (
            "eligible_combinational_cut_nets",
            "semantic_contract",
            "max_cross_fpga_dependency_depth",
        )
    ):
        raise ValidationError(
            "sequential-only cluster policy contains exact-cut fields"
        )

    instance_ids = {instance["id"] for instance in ir.value["instances"]}
    instances = {
        instance["id"]: instance for instance in ir.value["instances"]
    }
    fpga_by_id = {fpga.id: fpga for fpga in platform.fpgas}
    raw_assignment = assignment_artifact.get("instance_assignment")
    if not isinstance(raw_assignment, dict):
        raise ValidationError("assignment.instance_assignment: expected an object")
    assigned_ids = set(raw_assignment)
    missing = sorted(instance_ids - assigned_ids)
    extra = sorted(assigned_ids - instance_ids)
    if missing or extra:
        raise ValidationError(
            "assignment.instance_assignment: exact coverage failed; "
            f"missing={missing[:8]}, extra={extra[:8]}"
        )
    unknown_fpgas = sorted(set(raw_assignment.values()) - set(fpga_by_id))
    if unknown_fpgas:
        raise ValidationError(
            f"assignment.instance_assignment: unknown FPGAs {unknown_fpgas}"
        )

    raw_clusters = clusters_artifact.get("clusters")
    if not isinstance(raw_clusters, list):
        raise ValidationError("clusters.clusters: expected an array")
    cluster_ids: Set[str] = set()
    cluster_members: Set[str] = set()
    for index, cluster in enumerate(raw_clusters):
        if not isinstance(cluster, dict):
            raise ValidationError(f"clusters[{index}]: expected an object")
        cluster_id = cluster.get("id")
        members = cluster.get("instances")
        if not isinstance(cluster_id, str) or not cluster_id:
            raise ValidationError(
                f"clusters[{index}].id: expected a non-empty string"
            )
        if cluster_id in cluster_ids:
            raise ValidationError(f"clusters[{index}].id: duplicate {cluster_id!r}")
        cluster_ids.add(cluster_id)
        if not isinstance(members, list) or not all(
            isinstance(member, str) for member in members
        ):
            raise ValidationError(
                f"clusters[{index}].instances: expected an array of strings"
            )
        duplicate_members = cluster_members & set(members)
        if duplicate_members:
            raise ValidationError(
                f"clusters[{index}].instances: duplicate coverage "
                f"{sorted(duplicate_members)[:8]}"
            )
        cluster_members.update(members)
        assigned_fpgas = {raw_assignment[member] for member in members}
        if len(assigned_fpgas) != 1:
            raise ValidationError(
                f"cluster {cluster_id!r} spans FPGAs {sorted(assigned_fpgas)}"
            )
        expected_resources = _sum_resources(members, instances).to_dict(
            include_zeros=False
        )
        if cluster.get("resources") != expected_resources:
            raise ValidationError(
                f"cluster {cluster_id!r} resource summary does not match EmuIR"
            )
    if cluster_members != instance_ids:
        raise ValidationError(
            "clusters: exact instance coverage failed; "
            f"missing={sorted(instance_ids - cluster_members)[:8]}, "
            f"extra={sorted(cluster_members - instance_ids)[:8]}"
        )

    constraints = normalize_partition_constraints(
        assignment_artifact.get("constraints"),
        ir,
        platform,
    )
    inactive_assignment = sorted(
        set(raw_assignment.values()) - set(constraints["active_fpgas"])
    )
    if inactive_assignment:
        raise ValidationError(
            "assignment uses inactive FPGAs "
            f"{inactive_assignment}"
        )
    if cut_mode == CUT_MODE_STATIC_EXACT:
        required_policy = {
            "max_cross_fpga_dependency_depth",
            "eligible_combinational_cut_nets",
            "transported_cut_classes",
            "qualification",
        }
        missing_policy = sorted(required_policy - set(cut_policy))
        if missing_policy:
            raise ValidationError(
                "static exact cluster policy is incomplete: "
                f"{missing_policy}"
            )
        expected_clusters = build_clusters(
            ir,
            constraints,
            cut_mode=CUT_MODE_STATIC_EXACT,
            max_cross_fpga_dependency_depth=cut_policy[
                "max_cross_fpga_dependency_depth"
            ],
            static_exact_candidate_policy=cut_policy.get(
                "candidate_selection_policy",
                STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
            ),
        )
        if clusters_artifact != expected_clusters:
            raise ValidationError(
                "static exact clusters do not match independent reconstruction"
            )
    for group in constraints["groups"]:
        assigned_fpgas = {
            raw_assignment[instance_id] for instance_id in group["instances"]
        }
        if len(assigned_fpgas) != 1:
            raise ValidationError(
                f"group {group['id']!r} spans FPGAs {sorted(assigned_fpgas)}"
            )
    for fixed in constraints["fixed"]:
        actual = raw_assignment[fixed["instance"]]
        if actual != fixed["fpga"]:
            raise ValidationError(
                f"fixed instance {fixed['instance']!r}: expected "
                f"{fixed['fpga']!r}, got {actual!r}"
            )

    primary_resources_by_fpga = {
        fpga_id: ResourceVector.sum(
            ResourceVector.from_mapping(instances[instance_id]["resources"])
            for instance_id, assigned_fpga in raw_assignment.items()
            if assigned_fpga == fpga_id
        )
        for fpga_id in fpga_by_id
    }
    replication_validation = None
    if assignment_artifact.get("replication") is not None:
        if cut_mode == CUT_MODE_STATIC_EXACT:
            raise ValidationError(
                "replication is not qualified with static exact cuts"
            )
        from .replication import validate_replication_artifact

        replication_validation = validate_replication_artifact(
            ir,
            platform,
            clusters_artifact,
            assignment_artifact,
        )
        resources_by_fpga = replication_validation["resources_by_fpga"]
    else:
        resources_by_fpga = primary_resources_by_fpga
    for fpga_id, resources in resources_by_fpga.items():
        if not resources.fits_capacity(fpga_by_id[fpga_id].effective_capacity):
            raise ValidationError(
                f"FPGA {fpga_id!r} exceeds effective capacity: "
                f"{resources.to_dict(include_zeros=False)}"
            )
    used_fpgas = sum(
        1
        for fpga_id in fpga_by_id
        if any(assigned_fpga == fpga_id for assigned_fpga in raw_assignment.values())
    )
    if used_fpgas < constraints["min_used_fpgas"]:
        raise ValidationError(
            f"assignment uses {used_fpgas} FPGAs; "
            f"{constraints['min_used_fpgas']} required"
        )
    raw_cluster_assignment = assignment_artifact.get("cluster_assignment")
    if not isinstance(raw_cluster_assignment, dict):
        raise ValidationError("assignment.cluster_assignment: expected an object")
    if set(raw_cluster_assignment) != cluster_ids:
        raise ValidationError(
            "assignment.cluster_assignment: exact cluster coverage failed"
        )
    expected_cluster_assignment = {
        cluster["id"]: raw_assignment[cluster["instances"][0]]
        for cluster in raw_clusters
    }
    if raw_cluster_assignment != expected_cluster_assignment:
        raise ValidationError(
            "assignment.cluster_assignment does not match instance assignment"
        )
    balance_validation = validate_cluster_assignment_balance(
        platform,
        raw_clusters,
        raw_cluster_assignment,
        constraints["balance_tolerance"],
        constraints.get("balance_tolerance_by_dimension", {}),
        constraints.get("active_fpgas"),
        constraints.get("balance_dimensions"),
    )

    legal_cut_classes = set(LEGAL_CUT_CLASSES)
    eligible_combinational_nets: Set[str] = set()
    if cut_mode == CUT_MODE_STATIC_EXACT:
        transported = transported_cut_classes_for_clusters(clusters_artifact)
        if transported != {*TRANSPORTED_CUT_CLASSES, "combinational"}:
            raise ValidationError(
                "static exact mode requires combinational transport"
            )
        eligible_combinational_nets = set(
            cut_policy["eligible_combinational_cut_nets"]
        )
        legal_cut_classes.add("combinational")
    illegal_cuts: List[str] = []
    for net in ir.value["nets"]:
        fpga_ids = {
            raw_assignment[instance_id]
            for instance_id in _instance_ids_on_net(net)
        }
        if (
            len(fpga_ids) > 1
            and net["cut_class"]
            not in legal_cut_classes | REPLICATED_NET_CLASSES
        ):
            illegal_cuts.append(net["id"])
        if (
            len(fpga_ids) > 1
            and net["cut_class"] == "combinational"
            and net["id"] not in eligible_combinational_nets
        ):
            illegal_cuts.append(net["id"])
    if illegal_cuts:
        raise ValidationError(
            "assignment contains forbidden combinational cuts: "
            f"{illegal_cuts[:8]}"
        )

    if replication_validation is None and cut_mode == CUT_MODE_STATIC_EXACT:
        expected_cuts, expected_metrics, expected_cut_net_ordinals = (
            _compute_cut_nets_with_ordinals(ir, raw_assignment)
        )
    elif replication_validation is None:
        expected_cuts, expected_metrics = compute_cut_nets(ir, raw_assignment)
        expected_cut_net_ordinals = None
    else:
        expected_cuts = replication_validation["cut_nets"]
        expected_metrics = replication_validation["metrics"]
        expected_cut_net_ordinals = None
    expected_contract = None
    if cut_mode == CUT_MODE_STATIC_EXACT:
        from .combinational_cut import (
            build_static_exact_semantic_contract,
            semantic_contract_sha256,
        )

        expected_contract = build_static_exact_semantic_contract(
            ir,
            platform.to_dict(),
            raw_assignment,
            expected_cuts,
            max_dependency_depth=cut_policy[
                "max_cross_fpga_dependency_depth"
            ],
            candidate_selection_policy=cut_policy.get(
                "candidate_selection_policy",
                STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
            ),
            cut_net_ordinals=expected_cut_net_ordinals,
        )
        contract_nodes = expected_contract["cut_nodes"]
        if len(contract_nodes) != len(expected_cuts):
            raise ValidationError(
                "Static Exact contract cut coverage disagrees"
            )
        for cut, node in zip(expected_cuts, contract_nodes):
            if cut["net"] != node["net"]:
                raise ValidationError(
                    "Static Exact contract cut order disagrees"
                )
            cut.update(
                {
                    "dependency_level": node["dependency_level"],
                    "combinational_dependency_depth": node[
                        "combinational_dependency_depth"
                    ],
                    "predecessor_cut_nets": node[
                        "predecessor_cut_nets"
                    ],
                }
            )
        expected_metrics.update(
            {
                "combinational_cut_nets": expected_contract["metrics"][
                    "combinational_cut_nets"
                ],
                "maximum_combinational_dependency_depth": (
                    expected_contract["metrics"][
                        "maximum_combinational_dependency_depth"
                    ]
                ),
            }
        )
        _remove_static_exact_global_rounds(expected_cuts, expected_metrics)
        if assignment_artifact.get("semantic_contract") != expected_contract:
            raise ValidationError(
                "assignment.semantic_contract does not match independent "
                "reconstruction"
            )
        if assignment_artifact.get("semantic_contract_sha256") != (
            semantic_contract_sha256(expected_contract)
        ):
            raise ValidationError(
                "assignment.semantic_contract_sha256 does not match "
                "independent reconstruction"
            )
    elif any(
        key in assignment_artifact
        for key in ("semantic_contract", "semantic_contract_sha256")
    ):
        raise ValidationError(
            "sequential-only assignment may not contain a semantic contract"
        )
    if assignment_artifact.get("cut_nets") != expected_cuts:
        raise ValidationError("assignment.cut_nets does not match recomputed cuts")
    metrics = assignment_artifact.get("metrics")
    if not isinstance(metrics, dict):
        raise ValidationError("assignment.metrics: expected an object")
    for key, expected in expected_metrics.items():
        if metrics.get(key) != expected:
            raise ValidationError(
                f"assignment.metrics.{key}: expected {expected}, "
                f"got {metrics.get(key)!r}"
            )

    result = {
        "status": "pass",
        "instances": len(instance_ids),
        "clusters": len(raw_clusters),
        "used_fpgas": used_fpgas,
        "illegal_cuts": 0,
        **expected_metrics,
        **balance_validation,
        "resources_by_fpga": {
            fpga_id: resources.to_dict(include_zeros=False)
            for fpga_id, resources in resources_by_fpga.items()
        },
    }
    if expected_contract is not None:
        result.update(
            {
                "cut_mode": CUT_MODE_STATIC_EXACT,
                "qualification": "structural-partition-legality",
                "semantic_contract": {
                    "status": "pass",
                    **expected_contract["metrics"],
                },
            }
        )
    if replication_validation is not None:
        replication_metrics = replication_validation["artifact"]["metrics"]
        for key, expected in replication_metrics.items():
            if metrics.get(key) != expected:
                raise ValidationError(
                    f"assignment.metrics.{key}: expected {expected}, "
                    f"got {metrics.get(key)!r}"
                )
        result["replication"] = {
            "status": "pass",
            **replication_metrics,
        }
        result["primary_resources_by_fpga"] = {
            fpga_id: resources.to_dict(include_zeros=False)
            for fpga_id, resources in primary_resources_by_fpga.items()
        }
    return result
