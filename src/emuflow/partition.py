from __future__ import annotations

import hashlib
import math
from array import array
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from .errors import ValidationError
from .partition_limits import planning_resource_capacities, strict_balance_limits
from .platform import FpgaNode, Platform
from .resources import RESOURCE_FIELDS


CLUSTERS_SCHEMA = "emuflow.clusters/v1"
PARTITION_ASSIGNMENT_SCHEMA = "emuflow.partition-assignment/v1"
PARTITION_CONSTRAINTS_SCHEMA = "emuflow.partition-constraints/v1"
# Balance overall logic while dedicated resources remain capacity-constrained.
DEFAULT_BALANCE_DIMENSIONS: Tuple[str, ...] = ("cells", "lut", "ff")
TRANSPORTED_CUT_CLASSES = {"register_output", "register_input"}
LEGAL_CUT_CLASSES = {*TRANSPORTED_CUT_CLASSES, "primary_input"}
REPLICATED_NET_CLASSES = {"clock", "reset", "primary_input"}
CUT_MODE_SEQUENTIAL_ONLY = "sequential-only"
CUT_MODE_STATIC_EXACT = "static-exact-combinational"
DEFAULT_PARTITION_CUT_MODE = CUT_MODE_STATIC_EXACT
DEFAULT_PATRON_ALGORITHM_VERSION = 14
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
        return bytes(self._name_bytes[begin:end]).decode("utf-8")

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

    def cluster_id_at(self, index: int) -> str:
        if index < 0 or index >= len(self):
            raise IndexError("compact cluster index out of range")
        return f"c{index:06d}"

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


class _StaticExactCutNetRecords(list):
    """Final cut records projected from compact cuts and typed cut nodes."""

    def __init__(
        self,
        cuts: Sequence[Mapping[str, Any]],
        nodes: Sequence[Mapping[str, Any]],
    ) -> None:
        super().__init__()
        if len(cuts) != len(nodes):
            raise ValidationError("Static Exact contract cut coverage disagrees")
        for index in range(len(cuts)):
            if cuts.net_at(index) != nodes[index].get("net"):
                raise ValidationError("Static Exact contract cut order disagrees")
        self._cuts = cuts
        self._nodes = nodes

    def __len__(self) -> int:
        return len(self._cuts)

    def _record(self, index: int) -> Dict[str, Any]:
        cut = self._cuts[index]
        cut.pop("transport_round", None)
        node = self._nodes[index]
        cut.update(
            {
                "dependency_level": node["dependency_level"],
                "combinational_dependency_depth": node[
                    "combinational_dependency_depth"
                ],
                "predecessor_cut_nets": node["predecessor_cut_nets"],
            }
        )
        return cut

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for index in range(len(self)):
            yield self._record(index)

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, slice):
            return [self._record(item) for item in range(*index.indices(len(self)))]
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("Static Exact cut index must be an integer")
        resolved = index if index >= 0 else len(self) + index
        if resolved < 0 or resolved >= len(self):
            raise IndexError("Static Exact cut index out of range")
        return self._record(resolved)

    def __contains__(self, item: object) -> bool:
        return any(record == item for record in self)

    def __reversed__(self) -> Iterator[Dict[str, Any]]:
        for index in range(len(self) - 1, -1, -1):
            yield self._record(index)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Sequence):
            return False
        return len(self) == len(other) and all(
            left == right for left, right in zip(self, other)
        )

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
        return self._cuts.packed_payload_bytes

    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("Static Exact cut records are immutable")

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


class _TranslatedPartColumn(Sequence[int]):
    """Read-only coordinate view; a board-sized translation owns no C column."""
    itemsize = 4
    readonly = True

    def __init__(self, source, translation):
        self.source = source
        self.translation = translation

    def __len__(self):
        return len(self.source)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return array('I', (self[i] for i in range(*index.indices(len(self)))))
        return self.translation[self.source[index]]


class _CompactInstanceAssignment(Mapping[str, str]):
    """Bounded-memory instance assignment over compact EmuIR ordinals."""

    def __init__(
        self,
        path: Path,
        clusters: _CompactClusterRecords,
        cluster_assignment: Mapping[str, str],
        *,
        _known_used_parts=None,
    ) -> None:
        self.path = path
        self._native_publication = getattr(clusters, "implementation_id", None) == "native-primary-clusters-v1"
        self._native_primary_artifact = getattr(clusters, "native_artifact", None)
        if self._native_publication:
            if self._native_primary_artifact is None:
                raise ValidationError("native assignment primary ownership is missing")
            self._native_primary_generation = clusters.native_generation
            self._native_source_generation = clusters.native_source_generation
        _validate_cluster_assignment_coverage(clusters, cluster_assignment)
        self._cluster_member_offsets = clusters._member_offsets
        if isinstance(cluster_assignment, _PackedClusterAssignment):
            used = (set(cluster_assignment.part_indices) if _known_used_parts is None else
                    {index for index, part in enumerate(cluster_assignment.part_ids) if part in _known_used_parts})
            if any(index >= len(cluster_assignment.part_ids) for index in used):
                raise ValidationError("compact partition cluster ownership is invalid")
            self.part_ids = tuple(sorted(cluster_assignment.part_ids[index] for index in used))
            if self.part_ids == cluster_assignment.part_ids:
                self._cluster_parts = cluster_assignment.part_indices
            else:
                lookup = {part: index for index, part in enumerate(self.part_ids)}
                translation = {index: lookup[cluster_assignment.part_ids[index]] for index in used}
                self._cluster_parts = (_TranslatedPartColumn(cluster_assignment.part_indices, translation)
                    if _known_used_parts is not None else
                    array("I", (translation[index] for index in cluster_assignment.part_indices)))
        else:
            self.part_ids = tuple(sorted(set(cluster_assignment.values())))
            part_index = {part: index for index, part in enumerate(self.part_ids)}
            self._cluster_parts = array("I", (part_index[cluster_assignment[f"c{i:06d}"]] for i in range(len(clusters))))
        self._source_clusters = clusters.source_cluster_indices
        self._original_cluster_assignment = cluster_assignment
        self._original_cluster_parts = self._cluster_parts
        self._original_part_ids = self.part_ids
        if (self._cluster_member_offsets[0] != 0 or
                self._cluster_member_offsets[-1] != len(self._source_clusters)):
            raise ValidationError("compact partition instance coverage is invalid")
        if (getattr(clusters, "implementation_id", None) != "native-primary-clusters-v1" and
                any(not 0 < clusters.cluster_member_count(i) < 2**32 for i in range(len(clusters)))):
            raise ValidationError("compact partition cluster ownership is invalid")
        # The native primary owner already verified source coverage. Reference
        # columns still require their independent ordinal bound check here.
        if (getattr(clusters, "implementation_id", None) != "native-primary-clusters-v1" and
                self._source_clusters and max(self._source_clusters) >= len(clusters)):
            raise ValidationError("compact partition source ownership is invalid")

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
        return self._cluster_member_offsets[cluster_index + 1] - self._cluster_member_offsets[cluster_index]

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
                if self.part_indices is other.part_indices:
                    return True
                return len(self) == len(other) and all(
                    left == right
                    for left, right in zip(self.part_indices, other.part_indices)
                )
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


def _validate_cluster_assignment_coverage(clusters, assignment):
    """Check identities without decoding compact membership/resource records."""
    compact = isinstance(clusters, _CompactClusterRecords)
    if len(assignment) == len(clusters):
        if compact and isinstance(assignment, _PackedClusterAssignment):
            return  # Canonical identities are intrinsic to this typed mapping.
        identities = (f"c{i:06d}" for i in range(len(clusters))) if compact else (c["id"] for c in clusters)
        if all(identity in assignment for identity in identities):
            return
    expected = ({f"c{i:06d}" for i in range(len(clusters))} if compact
                else {cluster["id"] for cluster in clusters})
    actual = set(assignment)
    raise ValidationError("cluster assignment exact coverage failed; "
                          f"missing={sorted(expected-actual)[:8]}, extra={sorted(actual-expected)[:8]}")


def _cluster_assignment_summary(clusters, assignment, fpga_ids):
    """Independent primary-column reduction; reference records remain explicit."""
    _validate_cluster_assignment_coverage(clusters, assignment)
    if getattr(clusters, "implementation_id", None) == "native-primary-clusters-v1":
        from .phase3_assignment_native import reduce_native_partition_loads
        return reduce_native_partition_loads(clusters, assignment, fpga_ids)
    dimensions = ("cells", *RESOURCE_FIELDS)
    totals = dict.fromkeys(dimensions, 0)
    loads = {part: dict.fromkeys(dimensions, 0) for part in fpga_ids}
    counts = dict.fromkeys(fpga_ids, 0)
    for index in range(len(clusters)):
        if isinstance(clusters, _CompactClusterRecords):
            identity = f"c{index:06d}"
            values = (clusters.cluster_member_count(index),
                      *(column[index] for column in clusters._resource_columns))
        else:
            cluster = clusters[index]
            identity = cluster["id"]
            values = (len(cluster["instances"]), *(cluster["resources"].get(field, 0) for field in RESOURCE_FIELDS))
        part = assignment[identity]
        if part not in loads:
            raise ValidationError(f"balance assignment references unknown FPGA {part!r}")
        counts[part] += 1
        for dimension, value in zip(dimensions, values):
            totals[dimension] += value
            loads[part][dimension] += value
    return totals, loads, counts


def summarize_balance_observation(
    loads: Mapping[str, Mapping[str, int]],
    strict_dimensions: Iterable[str],
) -> Dict[str, Any]:
    """Describe the load distribution without imposing another upper bound."""
    fields = tuple(next(iter(loads.values()), {}))
    totals = {field: sum(row[field] for row in loads.values()) for field in fields}
    hard_dimensions = sorted(strict_dimensions)
    return {
        "diagnostic_only": not hard_dimensions,
        "hard_constraint_dimensions": hard_dimensions,
        "design_share_by_fpga": {
            fpga: {field: row[field] / totals[field] if totals[field] else 0.0
                   for field in fields}
            for fpga, row in loads.items()
        },
        "max_to_mean_by_dimension": {
            field: (len(loads) * max(row[field] for row in loads.values()) / totals[field]
                    if totals[field] else 0.0)
            for field in fields
        },
    }


def validate_cluster_assignment_balance(
    platform: Platform,
    clusters: Sequence[Mapping[str, Any]],
    cluster_assignment: Mapping[str, str],
    requested_tolerance: float,
    requested_tolerance_by_dimension: Optional[Mapping[str, float]] = None,
    active_fpgas: Optional[Sequence[str]] = None,
    requested_balance_dimensions: Optional[Sequence[str]] = (),
    resource_reservations_by_fpga: Optional[
        Mapping[str, Mapping[str, int]]
    ] = None,
) -> Dict[str, Any]:
    """Check capacities and opt-in share limits; explicit None is legacy all-fields."""
    partition_fpgas = _balance_partition_fpgas(platform, active_fpgas)
    fpga_ids = [fpga.id for fpga in partition_fpgas]
    if len(cluster_assignment) != len(clusters):
        raise ValidationError("balance assignment cluster coverage changed")

    if isinstance(clusters, _CompactClusterRecords):
        totals_all, loads_all, _counts = _cluster_assignment_summary(clusters, cluster_assignment, fpga_ids)
        resource_present = {field: bool(totals_all[field]) for field in RESOURCE_FIELDS}
    else:
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

    return _validate_balance_loads(partition_fpgas, totals_all, loads_all, resource_present,
                                  requested_tolerance, requested_tolerance_by_dimension,
                                  requested_balance_dimensions, resource_reservations_by_fpga)


def _balance_partition_fpgas(platform, active_fpgas):
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
    return partition_fpgas


def _validate_balance_loads(partition_fpgas, totals_all, loads_all, resource_present,
                            requested_tolerance, requested_tolerance_by_dimension,
                            requested_balance_dimensions, resource_reservations_by_fpga):
    """Apply independent capacity/share equations to primary-derived facts."""
    fpga_ids = [fpga.id for fpga in partition_fpgas]
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
        resource_reservations_by_fpga,
    )
    planning_capacity = planning_resource_capacities(
        partition_fpgas, resource_reservations_by_fpga
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
                    else planning_capacity[fpga.id][dimension]
                )
                for dimension in dimensions
            }
            for fpga in partition_fpgas
        },
        "physical_capacity_limits": {
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
        "resource_reservations_by_fpga": {
            fpga.id: dict(
                sorted((resource_reservations_by_fpga or {}).get(fpga.id, {}).items())
            )
            for fpga in partition_fpgas
        },
        "balance_observation": summarize_balance_observation(loads, strict_dimensions),
        "balance_loads": loads,
        "balance_allowed_loads": allowed_loads,
        "max_balance_limit_ratio": maximum_ratio,
        "balance_violations": 0,
        "strict_balance_violations": 0,
        "capacity_violations": 0,
    }
