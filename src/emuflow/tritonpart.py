from __future__ import annotations

import hashlib
import heapq
import math
import shutil
import subprocess
from array import array
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path
from typing import (
    AbstractSet,
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from .errors import EmuFlowError, ValidationError
from .io import read_json, write_json
from .ir import EmuIR
from .native_tools import resolve_native_executable
from .partition import (
    _CompactClusterRecords,
    _compact_partition_ir_path,
    active_partition_fpgas,
    build_partition_assignment,
    transported_cut_classes_for_clusters,
    validate_cluster_assignment_balance,
)
from .partition_limits import strict_balance_limits, validate_indivisible_limits
from .phase3_runtime import phase3_runtime_call, phase3_runtime_observe
from .platform import Platform
from .resources import RESOURCE_FIELDS
from .sta import (
    PARTITION_NET_WEIGHTS_INDEX_SCHEMA,
    load_partition_net_weights_indexed,
)


TRITONPART_INPUT_SCHEMA = "emuflow.tritonpart-input/v1"
PARTITION_NET_WEIGHTS_SCHEMA = "emuflow.partition-net-weights/v1"
TRITONPART_PROVIDER = "tritonpart-openroad-hypergraph-v1"
TRITONPART_OBJECTIVE_NET_CUT = "net-cut-v1"


class _CompactClusterOrder(list):
    """Canonical cluster identities without one Python string per vertex."""

    def __init__(self, count: int) -> None:
        super().__init__()
        self.count = count

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[str]:
        for index in range(self.count):
            yield f"c{index:06d}"

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(self.count))]
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("compact cluster index must be an integer")
        if index < 0:
            index += self.count
        if index < 0 or index >= self.count:
            raise IndexError("compact cluster index out of range")
        return f"c{index:06d}"


class _CompactVertexWeights(list):
    """Lazy TritonPart vertex rows over the authoritative cluster columns."""

    def __init__(
        self,
        clusters: _CompactClusterRecords,
        resource_fields: Sequence[str],
    ) -> None:
        super().__init__()
        self.clusters = clusters
        field_index = {
            field: index for index, field in enumerate(RESOURCE_FIELDS)
        }
        self.columns = tuple(
            clusters._resource_columns[field_index[field]]
            for field in resource_fields
        )

    def __len__(self) -> int:
        return len(self.clusters)

    def _row(self, index: int) -> List[int]:
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError("compact vertex index out of range")
        return [
            self.clusters.cluster_member_count(index),
            *(column[index] for column in self.columns),
        ]

    def __iter__(self) -> Iterator[List[int]]:
        for index in range(len(self)):
            yield self._row(index)

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, slice):
            return [self._row(item) for item in range(*index.indices(len(self)))]
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("compact vertex index must be an integer")
        return self._row(index)

    def totals(self) -> List[int]:
        return [
            len(self.clusters.source_cluster_indices),
            *(sum(column) for column in self.columns),
        ]


class _PackedHyperedgeRecords(list):
    """Packed legal hyperedges expanded one record at a time."""

    _CUT_CLASSES = (
        "register_output",
        "register_input",
        "combinational",
    )

    def __init__(self) -> None:
        super().__init__()
        self.name_bytes = bytearray()
        self.name_offsets = array("Q", [0])
        self.member_offsets = array("Q", [0])
        self.members = array("I")
        self.weights = array("d")
        self.cut_classes = array("B")

    def append_record(
        self,
        net_id: str,
        cut_class: str,
        weight: float,
        cluster_indices: Sequence[int],
    ) -> None:
        try:
            cut_code = self._CUT_CLASSES.index(cut_class)
        except ValueError as error:
            raise ValidationError(
                f"unsupported compact hyperedge class {cut_class!r}"
            ) from error
        encoded = net_id.encode("utf-8")
        self.name_bytes.extend(encoded)
        self.name_offsets.append(len(self.name_bytes))
        self.members.extend(cluster_indices)
        self.member_offsets.append(len(self.members))
        self.weights.append(float(weight))
        self.cut_classes.append(cut_code)

    def __len__(self) -> int:
        return len(self.weights)

    def _record(self, index: int) -> Dict[str, Any]:
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError("packed hyperedge index out of range")
        name_begin = self.name_offsets[index]
        name_end = self.name_offsets[index + 1]
        member_begin = self.member_offsets[index]
        member_end = self.member_offsets[index + 1]
        cluster_indices = self.members[member_begin:member_end]
        return {
            "net": self.name_bytes[name_begin:name_end].decode("utf-8"),
            "cut_class": self._CUT_CLASSES[self.cut_classes[index]],
            "objective_component": "net-cut",
            "weight": self.weights[index],
            "clusters": [
                f"c{cluster_index:06d}" for cluster_index in cluster_indices
            ],
            "vertices": [cluster_index + 1 for cluster_index in cluster_indices],
        }

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for index in range(len(self)):
            yield self._record(index)

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, slice):
            return [self._record(item) for item in range(*index.indices(len(self)))]
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("packed hyperedge index must be an integer")
        return self._record(index)

    @property
    def payload_bytes(self) -> int:
        columns = (
            self.name_offsets,
            self.member_offsets,
            self.members,
            self.weights,
            self.cut_classes,
        )
        return len(self.name_bytes) + sum(
            len(column) * column.itemsize for column in columns
        )


def load_partition_net_weights(path: Optional[Path]) -> Dict[str, float]:
    if path is None:
        return {}
    value = read_json(path)
    if value.get("schema") == PARTITION_NET_WEIGHTS_INDEX_SCHEMA:
        return load_partition_net_weights_indexed(path)
    if value.get("schema") != PARTITION_NET_WEIGHTS_SCHEMA:
        raise ValidationError(
            "net weights schema: expected "
            f"{PARTITION_NET_WEIGHTS_SCHEMA!r}, got {value.get('schema')!r}"
        )
    raw_weights = value.get("weights")
    if not isinstance(raw_weights, dict):
        raise ValidationError("net weights.weights: expected an object")
    weights: Dict[str, float] = {}
    for net_id, raw_weight in raw_weights.items():
        if not isinstance(net_id, str) or not net_id:
            raise ValidationError(
                "net weights.weights: keys must be non-empty strings"
            )
        if (
            isinstance(raw_weight, bool)
            or not isinstance(raw_weight, (int, float))
            or not math.isfinite(float(raw_weight))
            or float(raw_weight) <= 0.0
        ):
            raise ValidationError(
                f"net weights.weights[{net_id!r}]: expected a positive number"
            )
        weights[net_id] = float(raw_weight)
    return weights


def _active_resource_fields(
    clusters: Sequence[Mapping[str, Any]],
    fpgas: Sequence[Any],
) -> List[str]:
    fields = []
    compact_columns = (
        clusters._resource_columns
        if isinstance(clusters, _CompactClusterRecords)
        else None
    )
    for field_index, field in enumerate(RESOURCE_FIELDS):
        present = (
            any(compact_columns[field_index])
            if compact_columns is not None
            else any(
                cluster["resources"].get(field, 0)
                for cluster in clusters
            )
        )
        if not present:
            continue
        if not all(fpga.effective_capacity.get(field, 0) > 0 for fpga in fpgas):
            continue
        fields.append(field)
    return fields


def _capacity_base_balance(
    fpgas: Sequence[Any],
    resource_fields: Sequence[str],
) -> List[float]:
    num_parts = len(fpgas)
    if not resource_fields:
        return [1.0 / num_parts] * num_parts

    reference: Optional[List[float]] = None
    for field in resource_fields:
        capacities = [
            float(fpga.effective_capacity[field]) for fpga in fpgas
        ]
        total = sum(capacities)
        shares = [capacity / total for capacity in capacities]
        if reference is None:
            reference = shares
            continue
        if any(
            not math.isclose(left, right, rel_tol=1e-6, abs_tol=1e-9)
            for left, right in zip(reference, shares)
        ):
            raise ValidationError(
                "TritonPart hypergraph mode cannot represent resource-specific "
                "heterogeneous FPGA capacity ratios; use homogeneous/proportionally "
                "scaled FPGAs or the greedy provider"
            )
    assert reference is not None
    return reference


def _vertex_weights(
    cluster: Mapping[str, Any],
    resource_fields: Sequence[str],
) -> List[int]:
    return [
        len(cluster["instances"]),
        *(cluster["resources"].get(field, 0) for field in resource_fields),
    ]


def _tritonpart_ubfactor_percent_points(
    base_balance: Sequence[float],
    effective_balance_percent: float,
) -> float:
    """Translate EmuFlow's relative tolerance to TritonPart percentage points.

    EmuFlow defines a 10% tolerance around a 25% target as an upper share of
    27.5%. TritonPart instead adds ``UBfactor * 0.01`` directly to the target,
    so the equivalent UBfactor is 2.5 percentage points, not 10.

    A single TritonPart UBfactor is shared by all partitions. Using the largest
    target share supplies the solver's scalar envelope; EmuFlow's strict
    integer bounds and independent validator enforce every resource and FPGA
    without relaxing the requested tolerance.
    """

    return max(base_balance) * effective_balance_percent


def _legal_hyperedges(
    ir: EmuIR,
    cluster_by_instance: Mapping[str, str],
    vertex_number: Mapping[str, int],
    net_weights: Mapping[str, float],
    transported_cut_classes: set[str],
) -> List[Dict[str, Any]]:
    known_nets = {net["id"] for net in ir.value["nets"]}
    unknown_weights = sorted(set(net_weights) - known_nets)
    if unknown_weights:
        raise ValidationError(
            f"net weights reference unknown nets {unknown_weights[:8]}"
        )

    hyperedges = []
    for net in ir.value["nets"]:
        if net["cut_class"] not in transported_cut_classes:
            continue
        drivers = sorted(
            {
                cluster_by_instance[endpoint["instance"]]
                for endpoint in net["drivers"]
                if endpoint["instance"] is not None
            }
        )
        sinks = sorted(
            {
                cluster_by_instance[endpoint["instance"]]
                for endpoint in net["sinks"]
                if endpoint["instance"] is not None
            }
        )
        cluster_ids = sorted(set(drivers) | set(sinks))
        if len(cluster_ids) < 2:
            continue
        weight = float(net_weights.get(net["id"], 1.0))
        hyperedges.append(
            {
                "net": net["id"],
                "cut_class": net["cut_class"],
                "objective_component": "net-cut",
                "weight": weight,
                "clusters": cluster_ids,
                "vertices": [
                    vertex_number[cluster_id]
                    for cluster_id in cluster_ids
                ],
            }
        )
    return hyperedges


def _compact_legal_hyperedges(
    ir: EmuIR,
    clusters: _CompactClusterRecords,
    net_weights: Mapping[str, float],
    transported_cut_classes: AbstractSet[str],
) -> _PackedHyperedgeRecords:
    """Build packed hyperedges directly from compact endpoint ordinals."""

    from .emuir_storage import open_emuir_sqlite_read_only

    path = _compact_partition_ir_path(ir)
    if path is None:
        raise ValidationError("compact TritonPart input has no EmuIR store")
    source_clusters = clusters.source_cluster_indices
    remaining_weights = set(net_weights)
    hyperedges = _PackedHyperedgeRecords()
    connection = open_emuir_sqlite_read_only(path)
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
            cluster_indices = set()
            while endpoint is not None and endpoint["net_seq"] == net_seq:
                if endpoint["role"] not in (0, 1):
                    raise ValidationError(
                        "compact EmuIR endpoint role is invalid"
                    )
                source_seq = endpoint["instance_seq"]
                if source_seq is not None:
                    if source_seq < 0 or source_seq >= len(source_clusters):
                        raise ValidationError(
                            "compact EmuIR endpoint owner is invalid"
                        )
                    cluster_indices.add(source_clusters[source_seq])
                endpoint = next(endpoint_rows, None)
            if endpoint is not None and endpoint["net_seq"] < net_seq:
                raise ValidationError("compact EmuIR has orphaned endpoints")

            net_id = net["id"]
            remaining_weights.discard(net_id)
            cut_class = net["cut_class"]
            if (
                cut_class not in transported_cut_classes
                or len(cluster_indices) < 2
            ):
                continue
            ordered_clusters = sorted(cluster_indices)
            hyperedges.append_record(
                net_id,
                cut_class,
                float(net_weights.get(net_id, 1.0)),
                ordered_clusters,
            )
        if observed_nets != len(ir.value["nets"]) or endpoint is not None:
            raise ValidationError("compact EmuIR endpoint coverage is invalid")
    finally:
        connection.close()
    if remaining_weights:
        raise ValidationError(
            "net weights reference unknown nets "
            f"{sorted(remaining_weights)[:8]}"
        )
    return hyperedges


def _write_hypergraph_file(
    path: Path,
    hyperedges: Sequence[Mapping[str, Any]],
    vertex_weights: Sequence[Sequence[int]],
    vertex_count: int,
    *,
    weighted: bool,
) -> None:
    """Stream one hMetis file without retaining the complete text."""

    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{len(hyperedges)} {vertex_count} 11\n")
            for edge in hyperedges:
                weight = f"{edge['weight']:.9g}" if weighted else "1"
                stream.write(weight)
                for vertex in edge["vertices"]:
                    stream.write(f" {vertex}")
                stream.write("\n")
            for row in vertex_weights:
                stream.write(" ".join(str(value) for value in row))
                stream.write("\n")
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_lines(
    path: Path,
    lines: Iterable[str],
) -> None:
    """Publish a generated line stream without a graph-sized join."""

    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.writelines(lines)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def export_tritonpart_inputs(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    output_dir: Path,
    net_weights: Optional[Mapping[str, float]] = None,
    num_initial_solutions: int = 50,
    num_best_initial_solutions: int = 10,
    write_manifest: bool = True,
) -> Dict[str, Any]:
    if (
        isinstance(num_initial_solutions, bool)
        or not isinstance(num_initial_solutions, int)
        or num_initial_solutions <= 0
    ):
        raise ValidationError(
            "TritonPart num_initial_solutions must be a positive integer"
        )
    if (
        isinstance(num_best_initial_solutions, bool)
        or not isinstance(num_best_initial_solutions, int)
        or num_best_initial_solutions <= 0
        or num_best_initial_solutions > num_initial_solutions
    ):
        raise ValidationError(
            "TritonPart num_best_initial_solutions must be in [1, "
            "num_initial_solutions]"
        )
    raw_clusters = clusters_artifact["clusters"]
    compact_clusters = (
        raw_clusters
        if isinstance(raw_clusters, _CompactClusterRecords)
        and _compact_partition_ir_path(ir) is not None
        else None
    )
    clusters = (
        compact_clusters
        if compact_clusters is not None
        else sorted(raw_clusters, key=lambda item: item["id"])
    )
    partition_fpgas = active_partition_fpgas(platform, constraints)
    if len(clusters) < len(partition_fpgas):
        raise ValidationError(
            "TritonPart needs at least one atomic cluster per FPGA"
        )

    resource_fields = _active_resource_fields(clusters, partition_fpgas)
    dimensions = ["cells", *resource_fields]
    weights: Sequence[Sequence[int]] = (
        _CompactVertexWeights(compact_clusters, resource_fields)
        if compact_clusters is not None
        else [
            _vertex_weights(cluster, resource_fields) for cluster in clusters
        ]
    )
    base_balance = _capacity_base_balance(partition_fpgas, resource_fields)
    fpga_ids = [fpga.id for fpga in partition_fpgas]
    tolerances = constraints.get("balance_tolerance_by_dimension", {})
    requested_balance = constraints["balance_tolerance"] * 100.0
    effective_balance = max(
        tolerances.get(field, constraints["balance_tolerance"]) * 100.0
        for field in dimensions
    )
    tritonpart_ubfactor = _tritonpart_ubfactor_percent_points(base_balance, effective_balance)
    weight_totals = (
        weights.totals()
        if isinstance(weights, _CompactVertexWeights)
        else [
            sum(row[index] for row in weights)
            for index in range(len(dimensions))
        ]
    )
    totals = dict(zip(dimensions, weight_totals))
    limits = strict_balance_limits(
        partition_fpgas,
        totals,
        constraints["balance_tolerance"],
        tolerances,
        constraints.get("balance_dimensions"),
    )
    if compact_clusters is not None:
        validate_indivisible_limits(
            (
                (
                    f"c{index:06d}",
                    dict(zip(dimensions, row)),
                    compact_clusters._fixed_by_cluster.get(index),
                )
                for index, row in enumerate(weights)
            ),
            limits,
        )
        hyperedges: Sequence[Mapping[str, Any]] = (
            _compact_legal_hyperedges(
                ir,
                compact_clusters,
                net_weights or {},
                transported_cut_classes_for_clusters(clusters_artifact),
            )
        )
    else:
        validate_indivisible_limits(
            (
                (
                    cluster["id"],
                    dict(zip(dimensions, row)),
                    cluster.get("fixed_fpga"),
                )
                for cluster, row in zip(clusters, weights)
            ),
            limits,
        )
        vertex_number = {
            cluster["id"]: index
            for index, cluster in enumerate(clusters, start=1)
        }
        cluster_by_instance = {
            instance_id: cluster["id"]
            for cluster in clusters
            for instance_id in cluster["instances"]
        }
        hyperedges = _legal_hyperedges(
            ir,
            cluster_by_instance,
            vertex_number,
            net_weights or {},
            transported_cut_classes_for_clusters(clusters_artifact),
        )
    if not hyperedges:
        raise ValidationError(
            "TritonPart hypergraph has no legal sequential-boundary "
            "hyperedges; "
            "use the greedy provider for disconnected designs"
        )
    specified_weights = net_weights or {}
    timed_legal_hyperedges = 0
    minimum_timed_weight = math.inf
    maximum_timed_weight = -math.inf
    for edge in hyperedges:
        if edge["objective_component"] != "net-cut":
            raise ValidationError(
                "TritonPart input contains an unsupported objective component"
            )
        if edge["net"] not in specified_weights:
            continue
        timed_legal_hyperedges += 1
        minimum_timed_weight = min(
            minimum_timed_weight, float(edge["weight"])
        )
        maximum_timed_weight = max(
            maximum_timed_weight, float(edge["weight"])
        )
    timing_weight_coverage = {
        "specified_nets": len(specified_weights),
        "legal_hyperedges": len(hyperedges),
        "timed_legal_hyperedges": timed_legal_hyperedges,
        "timed_legal_hyperedge_fraction": (
            timed_legal_hyperedges / len(hyperedges)
        ),
        "minimum_timed_weight": (
            minimum_timed_weight if timed_legal_hyperedges else 1.0
        ),
        "maximum_timed_weight": (
            maximum_timed_weight if timed_legal_hyperedges else 1.0
        ),
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    hypergraph_path = output_dir / "partition.hgr"
    baseline_hypergraph_path = output_dir / "partition.unweighted.hgr"
    fixed_path = output_dir / "partition.fix"
    edge_classes_path = output_dir / "partition.edge-classes"
    tcl_path = output_dir / "run_tritonpart.tcl"
    solution_path = output_dir / f"partition.hgr.part.{len(fpga_ids)}"

    _write_hypergraph_file(
        hypergraph_path,
        hyperedges,
        weights,
        len(clusters),
        weighted=True,
    )
    if specified_weights:
        _write_hypergraph_file(
            baseline_hypergraph_path,
            hyperedges,
            weights,
            len(clusters),
            weighted=False,
        )

    fpga_index = {fpga_id: index for index, fpga_id in enumerate(fpga_ids)}
    fixed_fpgas: Iterable[Optional[str]] = (
        (
            compact_clusters._fixed_by_cluster.get(index)
            for index in range(len(compact_clusters))
        )
        if compact_clusters is not None
        else (cluster["fixed_fpga"] for cluster in clusters)
    )
    _write_lines(
        fixed_path,
        (
            f"{fpga_index[fixed_fpga]}\n"
            if fixed_fpga is not None
            else "-1\n"
            for fixed_fpga in fixed_fpgas
        ),
    )
    _write_lines(
        edge_classes_path,
        (
            "1\n" if edge["cut_class"] == "combinational" else "0\n"
            for edge in hyperedges
        ),
    )

    def tcl_list(values: Sequence[Any]) -> str:
        return "{ " + " ".join(str(value) for value in values) + " }"

    tcl_lines = [
        "triton_part_hypergraph \\",
        f"  -hypergraph_file {{{hypergraph_path.resolve()}}} \\",
        f"  -fixed_file {{{fixed_path.resolve()}}} \\",
        f"  -num_parts {len(fpga_ids)} \\",
        f"  -balance_constraint {tritonpart_ubfactor:.9g} \\",
        f"  -base_balance {tcl_list([f'{value:.12g}' for value in base_balance])} \\",
        f"  -scale_factor {tcl_list([1.0] * len(fpga_ids))} \\",
        f"  -seed 0 \\",
        f"  -num_initial_solutions {num_initial_solutions} \\",
        (
            "  -num_best_initial_solutions "
            f"{num_best_initial_solutions} \\"
        ),
        f"  -vertex_dimension {len(dimensions)} \\",
        "  -hyperedge_dimension 1 \\",
        f"  -v_wt_factors {tcl_list([1.0] * len(dimensions))} \\",
        "  -e_wt_factors { 1.0 } \\",
        "  -min_num_vertices_each_part 1",
        "exit",
    ]
    tcl_path.write_text("\n".join(tcl_lines) + "\n", encoding="utf-8")

    artifact: Dict[str, Any] = {
        "schema": TRITONPART_INPUT_SCHEMA,
        "objective_encoding": TRITONPART_OBJECTIVE_NET_CUT,
        "objective_components": {
            "net_cut_hyperedges": len(hyperedges),
        },
        "design": ir.value["design"]["name"],
        "platform": platform.name,
        "fpga_order": fpga_ids,
        "cluster_order": (
            _CompactClusterOrder(len(compact_clusters))
            if compact_clusters is not None
            else [cluster["id"] for cluster in clusters]
        ),
        "vertex_dimensions": dimensions,
        "vertex_weights": weights,
        "hyperedges": hyperedges,
        "timing_weight_coverage": timing_weight_coverage,
        "base_balance": base_balance,
        "requested_balance_percent": requested_balance,
        "effective_balance_percent": effective_balance,
        "tritonpart_ubfactor_percent_points": tritonpart_ubfactor,
        "balance_auto_relaxed": False,
        "balance_allowed_loads": limits,
        "search_effort": {
            "num_initial_solutions": num_initial_solutions,
            "num_best_initial_solutions": num_best_initial_solutions,
        },
        "files": {
            "hypergraph": hypergraph_path.name,
            "unweighted_baseline_hypergraph": (
                baseline_hypergraph_path.name
                if specified_weights
                else None
            ),
            "fixed": fixed_path.name,
            "edge_classes": edge_classes_path.name,
            "tcl": tcl_path.name,
            "solution": solution_path.name,
        },
    }
    if write_manifest:
        write_json(output_dir / "tritonpart_input.json", artifact, compact=True)
    return artifact


def parse_tritonpart_solution(
    path: Path,
    tritonpart_input: Mapping[str, Any],
) -> Dict[str, str]:
    if not path.is_file():
        raise ValidationError(f"TritonPart solution does not exist: {path}")
    cluster_order = tritonpart_input["cluster_order"]
    fpga_order = tritonpart_input["fpga_order"]
    with path.open("r", encoding="utf-8") as stream:
        token_count = sum(len(line.split()) for line in stream)
    if token_count != len(cluster_order):
        raise ValidationError(
            "TritonPart solution vertex count mismatch: "
            f"expected {len(cluster_order)}, got {token_count}"
        )
    assignment: Dict[str, str] = {}
    cluster_ids = iter(cluster_order)
    index = 0
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            for token in line.split():
                cluster_id = next(cluster_ids)
                try:
                    part_id = int(token)
                except ValueError as error:
                    raise ValidationError(
                        "TritonPart solution line "
                        f"{index + 1}: expected an integer"
                    ) from error
                if part_id < 0 or part_id >= len(fpga_order):
                    raise ValidationError(
                        "TritonPart solution line "
                        f"{index + 1}: invalid part {part_id}"
                    )
                assignment[cluster_id] = fpga_order[part_id]
                index += 1
    return assignment


def _repair_min_used_fpgas(
    cluster_assignment: Mapping[str, str],
    clusters_artifact: Mapping[str, Any],
    platform: Platform,
    constraints: Mapping[str, Any],
    hyperedges: Sequence[Mapping[str, Any]],
) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    assignment = dict(cluster_assignment)
    clusters = {
        cluster["id"]: cluster for cluster in clusters_artifact["clusters"]
    }
    partition_fpgas = active_partition_fpgas(platform, constraints)
    fpga_ids = [fpga.id for fpga in partition_fpgas]
    capacity = {
        fpga.id: fpga.effective_capacity for fpga in partition_fpgas
    }
    loads = {
        fpga_id: {field: 0 for field in RESOURCE_FIELDS}
        for fpga_id in fpga_ids
    }
    cluster_counts = {fpga_id: 0 for fpga_id in fpga_ids}
    for cluster_id, fpga_id in assignment.items():
        cluster_counts[fpga_id] += 1
        for field, value in clusters[cluster_id]["resources"].items():
            loads[fpga_id][field] += value

    # Maintain the partition multiplicity of every incident hyperedge.  The
    # previous implementation rebuilt two Python sets over the complete
    # hyperedge for every candidate move.  That is exact but becomes
    # prohibitively expensive on contest-scale netlists (hundreds of
    # thousands of clusters and many high-fanout nets).  Partition counts
    # make the same cut-connectivity delta O(incident edges), independent of
    # hyperedge cardinality.
    edge_clusters = [list(edge["clusters"]) for edge in hyperedges]
    edge_weights = [float(edge["weight"]) for edge in hyperedges]
    incident_edges: Dict[str, List[int]] = defaultdict(list)
    edge_part_counts: List[Dict[str, int]] = []
    for edge_index, cluster_ids in enumerate(edge_clusters):
        counts: Dict[str, int] = defaultdict(int)
        for cluster_id in cluster_ids:
            incident_edges[cluster_id].append(edge_index)
            counts[assignment[cluster_id]] += 1
        edge_part_counts.append(dict(counts))

    def cut_delta(cluster_id: str, source: str, target: str) -> float:
        delta = 0.0
        for edge_index in incident_edges.get(cluster_id, []):
            counts = edge_part_counts[edge_index]
            before_parts = len(counts)
            after_parts = before_parts
            if counts[source] == 1:
                after_parts -= 1
            if counts.get(target, 0) == 0:
                after_parts += 1
            delta += (
                int(after_parts > 1) - int(before_parts > 1)
            ) * edge_weights[edge_index]
        return delta

    def apply_edge_move(cluster_id: str, source: str, target: str) -> None:
        for edge_index in incident_edges.get(cluster_id, []):
            counts = edge_part_counts[edge_index]
            counts[source] -= 1
            if counts[source] == 0:
                del counts[source]
            counts[target] = counts.get(target, 0) + 1

    moves = []
    while sum(count > 0 for count in cluster_counts.values()) < constraints[
        "min_used_fpgas"
    ]:
        target = next(
            fpga_id for fpga_id in fpga_ids if cluster_counts[fpga_id] == 0
        )
        candidates = []
        for cluster_id, source in assignment.items():
            cluster = clusters[cluster_id]
            if cluster_counts[source] <= 1 or cluster["fixed_fpga"] is not None:
                continue
            resources = cluster["resources"]
            if any(
                loads[target][field] + resources.get(field, 0) > limit
                for field, limit in capacity[target].items()
            ):
                continue
            estimated_delta = cut_delta(cluster_id, source, target)
            candidates.append(
                (
                    len(cluster["instances"]),
                    sum(resources.values()),
                    estimated_delta,
                    cluster_id,
                    source,
                )
            )
        if not candidates:
            raise ValidationError(
                "TritonPart min-used-FPGA repair found no legal movable "
                f"cluster for empty partition {target!r}"
            )
        instance_count, _, estimated_cut_delta, cluster_id, source = min(
            candidates
        )
        resources = clusters[cluster_id]["resources"]
        apply_edge_move(cluster_id, source, target)
        assignment[cluster_id] = target
        cluster_counts[source] -= 1
        cluster_counts[target] += 1
        for field in RESOURCE_FIELDS:
            value = resources.get(field, 0)
            loads[source][field] -= value
            loads[target][field] += value
        moves.append(
            {
                "cluster": cluster_id,
                "source": source,
                "target": target,
                "instances": instance_count,
                "resources": dict(resources),
                "estimated_cut_delta": estimated_cut_delta,
            }
        )
    return assignment, moves


def _repair_multi_resource_balance_reference(
    cluster_assignment: Mapping[str, str],
    clusters_artifact: Mapping[str, Any],
    platform: Platform,
    constraints: Mapping[str, Any],
    tritonpart_input: Mapping[str, Any],
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """Reference-oracle legalizer for bounded test fixtures only.

    Production Phase 3 uses the compiled native balance legalizer and then
    independently recomputes legality in Python. This historical algorithm is
    retained only so small tests can check the native implementation's repair
    semantics; it is never called by the production flow.
    """

    assignment = dict(cluster_assignment)
    clusters = {
        cluster["id"]: cluster for cluster in clusters_artifact["clusters"]
    }
    cluster_order = list(tritonpart_input["cluster_order"])
    fpga_order = list(tritonpart_input["fpga_order"])
    dimensions = list(tritonpart_input["vertex_dimensions"])
    vertex_weights = list(tritonpart_input["vertex_weights"])
    fpga_index = {
        fpga_id: index for index, fpga_id in enumerate(fpga_order)
    }
    labels = [
        fpga_index[assignment[cluster_id]] for cluster_id in cluster_order
    ]
    num_parts = len(fpga_order)
    num_dimensions = len(dimensions)
    totals = [
        sum(weights[dimension] for weights in vertex_weights)
        for dimension in range(num_dimensions)
    ]

    tolerance_overrides = constraints.get(
        "balance_tolerance_by_dimension", {}
    )
    fpga_by_id = {fpga.id: fpga for fpga in platform.fpgas}
    limits = strict_balance_limits(
        [fpga_by_id[fpga_id] for fpga_id in fpga_order],
        dict(zip(dimensions, totals)),
        constraints["balance_tolerance"],
        tolerance_overrides,
        constraints.get("balance_dimensions"),
    )
    allowed = [
        [limits[fpga_id][field] for field in dimensions]
        for fpga_id in fpga_order
    ]
    loads = [[0] * num_dimensions for _ in range(num_parts)]
    for part, weights in zip(labels, vertex_weights):
        for dimension, weight in enumerate(weights):
            loads[part][dimension] += weight
    initial_loads = [list(item) for item in loads]

    edge_vertices = [
        [vertex - 1 for vertex in edge["vertices"]]
        for edge in tritonpart_input["hyperedges"]
    ]
    hyperedges = list(tritonpart_input["hyperedges"])
    edge_weights = [float(edge["weight"]) for edge in hyperedges]
    combinational_edges = [
        edge.get("cut_class") == "combinational" for edge in hyperedges
    ]
    # Balance repair is a legality fallback, not a second partition optimizer.
    # In Static Exact mode, creating a new combinational boundary changes the
    # cross-layer timing contract and can dominate the ordinary hypergraph cut
    # delta.  Give each such transition a data-derived lexicographic penalty:
    # one extra combinational cut outweighs every ordinary edge-weight change
    # that any repair sequence can make.  Sequential-only inputs contain no
    # marked edge and therefore retain the exact historical objective.
    combinational_cut_penalty = (
        math.fsum(abs(weight) for weight in edge_weights) + 1.0
        if any(combinational_edges)
        else 0.0
    )
    repair_edge_weights = [
        weight + (combinational_cut_penalty if combinational else 0.0)
        for weight, combinational in zip(edge_weights, combinational_edges)
    ]
    incident_edges: List[List[int]] = [
        [] for _ in range(len(cluster_order))
    ]
    for edge_index, vertices in enumerate(edge_vertices):
        for vertex in vertices:
            incident_edges[vertex].append(edge_index)
    edge_part_counts: List[Dict[int, int]] = []
    for vertices in edge_vertices:
        counts: Dict[int, int] = defaultdict(int)
        for vertex in vertices:
            counts[labels[vertex]] += 1
        edge_part_counts.append(dict(counts))
    vertices_by_part: List[List[int]] = [[] for _ in range(num_parts)]
    for vertex, part in enumerate(labels):
        vertices_by_part[part].append(vertex)

    def overload(part: int) -> List[int]:
        return [
            max(0, loads[part][dimension] - allowed[part][dimension])
            for dimension in range(num_dimensions)
        ]

    def fits(vertex: int, part: int) -> bool:
        return all(
            loads[part][dimension] + vertex_weights[vertex][dimension]
            <= allowed[part][dimension]
            for dimension in range(num_dimensions)
        )

    def edge_cut_delta(
        vertex: int,
        source: int,
        target: int,
        weights: Sequence[float],
    ) -> float:
        delta = 0.0
        for edge_index in incident_edges[vertex]:
            counts = edge_part_counts[edge_index]
            before_parts = len(counts)
            after_parts = before_parts
            if counts[source] == 1:
                after_parts -= 1
            if counts.get(target, 0) == 0:
                after_parts += 1
            delta += (
                int(after_parts > 1) - int(before_parts > 1)
            ) * weights[edge_index]
        return delta

    def cut_delta(vertex: int, source: int, target: int) -> float:
        return edge_cut_delta(vertex, source, target, edge_weights)

    def repair_delta(vertex: int, source: int, target: int) -> float:
        return edge_cut_delta(vertex, source, target, repair_edge_weights)

    def apply_edge_move(vertex: int, source: int, target: int) -> None:
        for edge_index in incident_edges[vertex]:
            counts = edge_part_counts[edge_index]
            counts[source] -= 1
            if counts[source] == 0:
                del counts[source]
            counts[target] = counts.get(target, 0) + 1

    def cut_summary() -> Tuple[int, float, int]:
        cut_edges = 0
        cut_weight = 0.0
        combinational_cut_edges = 0
        for counts, weight, combinational in zip(
            edge_part_counts, edge_weights, combinational_edges
        ):
            if len(counts) > 1:
                cut_edges += 1
                cut_weight += weight
                combinational_cut_edges += int(combinational)
        return cut_edges, cut_weight, combinational_cut_edges

    (
        initial_cut_edges,
        initial_cut_weight,
        initial_combinational_cut_edges,
    ) = cut_summary()
    move_counts: Dict[Tuple[int, int], int] = defaultdict(int)
    moved_weights: Dict[Tuple[int, int], List[int]] = {}
    move_digest = hashlib.sha256()
    estimated_cut_delta = 0.0
    repair_objective_delta = 0.0
    move_total = 0
    paired_move_sequences = 0
    ejection_chain_sequences = 0
    max_ejection_chain_moves = 0

    def apply_move(vertex: int, source: int, target: int) -> float:
        """Apply and audit one deterministic repair move."""

        nonlocal estimated_cut_delta, repair_objective_delta, move_total
        delta = cut_delta(vertex, source, target)
        objective_delta = repair_delta(vertex, source, target)
        apply_edge_move(vertex, source, target)
        labels[vertex] = target
        weights = vertex_weights[vertex]
        for dimension, weight in enumerate(weights):
            loads[source][dimension] -= weight
            loads[target][dimension] += weight
        vertices_by_part[target].append(vertex)
        transition = (source, target)
        move_counts[transition] += 1
        if transition not in moved_weights:
            moved_weights[transition] = [0] * num_dimensions
        for dimension, weight in enumerate(weights):
            moved_weights[transition][dimension] += weight
        cluster_id = cluster_order[vertex]
        assignment[cluster_id] = fpga_order[target]
        move_digest.update(
            (
                f"{cluster_id}:{fpga_order[source]}"
                f"->{fpga_order[target]}\n"
            ).encode("utf-8")
        )
        estimated_cut_delta += delta
        repair_objective_delta += objective_delta
        move_total += 1
        return delta

    def overload_score(
        part: int, projected: Optional[Sequence[int]] = None
    ) -> float:
        candidate_load = loads[part] if projected is None else projected
        return sum(
            max(0, candidate_load[dimension] - allowed[part][dimension])
            / max(1, allowed[part][dimension])
            for dimension in range(num_dimensions)
        )

    while True:
        overloaded_parts = [
            part for part in range(num_parts) if any(overload(part))
        ]
        if not overloaded_parts:
            break
        source = max(
            overloaded_parts,
            key=lambda part: max(
                (
                    loads[part][dimension] - allowed[part][dimension]
                )
                / max(1, allowed[part][dimension])
                for dimension in range(num_dimensions)
            ),
        )
        source_overload = overload(source)
        ranked: List[Tuple[float, float, float, int]] = []
        for vertex in vertices_by_part[source]:
            if labels[vertex] != source:
                continue
            weights = vertex_weights[vertex]
            cluster_id = cluster_order[vertex]
            if clusters[cluster_id]["fixed_fpga"] is not None:
                continue
            relief = sum(
                min(weights[dimension], source_overload[dimension])
                / max(1, allowed[source][dimension])
                for dimension in range(num_dimensions)
            )
            if relief <= 0.0:
                continue
            choices = []
            for target in range(num_parts):
                if target == source or not fits(vertex, target):
                    continue
                delta = repair_delta(vertex, source, target)
                projected_peak = max(
                    (
                        loads[target][dimension] + weights[dimension]
                    )
                    / max(1, allowed[target][dimension])
                    for dimension in range(num_dimensions)
                )
                choices.append((delta, projected_peak, target))
            if choices:
                best_delta, _, _ = min(choices)
                ranked.append(
                    (
                        best_delta / relief,
                        best_delta,
                        -relief,
                        vertex,
                    )
                )
        ranked.sort()

        moved_from_source = 0
        for _, _, _, vertex in ranked:
            source_overload = overload(source)
            if not any(source_overload):
                break
            if labels[vertex] != source:
                continue
            weights = vertex_weights[vertex]
            if not any(
                weights[dimension] and source_overload[dimension]
                for dimension in range(num_dimensions)
            ):
                continue
            choices = []
            for target in range(num_parts):
                if target == source or not fits(vertex, target):
                    continue
                delta = repair_delta(vertex, source, target)
                projected_peak = max(
                    (
                        loads[target][dimension] + weights[dimension]
                    )
                    / max(1, allowed[target][dimension])
                    for dimension in range(num_dimensions)
                )
                choices.append((delta, projected_peak, target))
            if not choices:
                continue
            _, _, best_target = min(choices)
            apply_move(vertex, source, best_target)
            moved_from_source += 1
        if not moved_from_source:
            # A legal multidimensional assignment may require an exchange:
            # every destination can be full in a resource unrelated to the
            # source overload even though moving one blocker away makes room.
            # Search a deterministic two-move augmenting path
            #   target -> destination, source -> target
            # and commit both moves atomically from the repair perspective.
            best_pair = None
            before_source_score = overload_score(source)

            # A naive exchange search is quadratic in the number of vertices:
            # for every source vertex and target it scans every target vertex
            # and every possible destination.  Real designs can have hundreds
            # of thousands of clusters, even though the blocking resource is
            # usually sparse (for example DSP or URAM).  Index blockers by
            # resource weight and cache repeated deficit queries.  This keeps
            # the search complete while restricting each query to vertices
            # that can actually free the target's missing capacity.
            blocker_indexes: Dict[
                int, List[List[Tuple[int, int]]]
            ] = {}
            eligible_blocker_cache: Dict[
                Tuple[int, Tuple[int, ...]], Tuple[int, ...]
            ] = {}
            destination_cache: Dict[
                Tuple[int, int], Optional[Tuple[float, int]]
            ] = {}

            def blocker_index(target: int) -> List[List[Tuple[int, int]]]:
                cached = blocker_indexes.get(target)
                if cached is not None:
                    return cached
                indexes: List[List[Tuple[int, int]]] = [
                    [] for _ in range(num_dimensions)
                ]
                for blocker in vertices_by_part[target]:
                    if labels[blocker] != target:
                        continue
                    blocker_id = cluster_order[blocker]
                    if clusters[blocker_id]["fixed_fpga"] is not None:
                        continue
                    for dimension, weight in enumerate(
                        vertex_weights[blocker]
                    ):
                        indexes[dimension].append((weight, blocker))
                for index in indexes:
                    index.sort()
                blocker_indexes[target] = indexes
                return indexes

            def eligible_blockers(
                target: int, deficits: Sequence[int]
            ) -> Tuple[int, ...]:
                cache_key = (target, tuple(deficits))
                cached = eligible_blocker_cache.get(cache_key)
                if cached is not None:
                    return cached
                indexes = blocker_index(target)
                pivots = []
                for dimension, deficit in enumerate(deficits):
                    if deficit <= 0:
                        continue
                    start = bisect_left(indexes[dimension], (deficit, -1))
                    pivots.append(
                        (len(indexes[dimension]) - start, dimension, start)
                    )
                if not pivots:
                    eligible_blocker_cache[cache_key] = ()
                    return ()
                _, pivot_dimension, start = min(pivots)
                candidates = []
                for _, blocker in indexes[pivot_dimension][start:]:
                    weights = vertex_weights[blocker]
                    if all(
                        weights[dimension] >= deficits[dimension]
                        for dimension in range(num_dimensions)
                    ):
                        candidates.append(blocker)
                result = tuple(candidates)
                eligible_blocker_cache[cache_key] = result
                return result

            def best_non_source_destination(
                blocker: int, target: int
            ) -> Optional[Tuple[float, int]]:
                cache_key = (blocker, target)
                if cache_key in destination_cache:
                    return destination_cache[cache_key]
                choices = []
                blocker_weights = vertex_weights[blocker]
                for destination in range(num_parts):
                    if destination in {source, target}:
                        continue
                    if not fits(blocker, destination):
                        continue
                    choices.append(
                        (
                            repair_delta(blocker, target, destination),
                            destination,
                        )
                    )
                result = min(choices) if choices else None
                destination_cache[cache_key] = result
                return result

            source_candidates = []
            for vertex in vertices_by_part[source]:
                if labels[vertex] != source:
                    continue
                weights = vertex_weights[vertex]
                cluster_id = cluster_order[vertex]
                if clusters[cluster_id]["fixed_fpga"] is not None:
                    continue
                relief = sum(
                    min(weights[dimension], source_overload[dimension])
                    / max(1, allowed[source][dimension])
                    for dimension in range(num_dimensions)
                )
                if relief <= 0.0:
                    continue
                source_without_vertex = [
                    loads[source][dimension] - weights[dimension]
                    for dimension in range(num_dimensions)
                ]
                source_candidates.append(
                    (
                        overload_score(source, source_without_vertex),
                        cluster_id,
                        vertex,
                        relief,
                    )
                )

            # The first tuple field is a lower bound on every exchange using
            # this source vertex: sending a blocker back to the source can
            # only increase its post-move overload.  Once it exceeds the best
            # complete candidate, later vertices cannot win the deterministic
            # ranking and need not be inspected.
            source_candidates.sort()
            for (
                source_without_score,
                cluster_id,
                vertex,
                relief,
            ) in source_candidates:
                if (
                    best_pair is not None
                    and source_without_score > best_pair[0]
                ):
                    break
                weights = vertex_weights[vertex]
                for target in range(num_parts):
                    if target == source:
                        continue
                    source_delta = repair_delta(vertex, source, target)
                    target_after_vertex = [
                        loads[target][dimension] + weights[dimension]
                        for dimension in range(num_dimensions)
                    ]
                    if all(
                        target_after_vertex[dimension]
                        <= allowed[target][dimension]
                        for dimension in range(num_dimensions)
                    ):
                        continue
                    deficits = [
                        max(
                            0,
                            target_after_vertex[dimension]
                            - allowed[target][dimension],
                        )
                        for dimension in range(num_dimensions)
                    ]
                    for blocker in eligible_blockers(target, deficits):
                        blocker_id = cluster_order[blocker]
                        blocker_weights = vertex_weights[blocker]
                        non_source = best_non_source_destination(
                            blocker, target
                        )
                        if non_source is not None:
                            blocker_delta, destination = non_source
                            pair_delta = blocker_delta + source_delta
                            candidate = (
                                source_without_score,
                                pair_delta / relief,
                                pair_delta,
                                cluster_id,
                                blocker_id,
                                target,
                                destination,
                                vertex,
                                blocker,
                            )
                            if best_pair is None or candidate < best_pair:
                                best_pair = candidate

                        destination_after = [
                            loads[source][dimension]
                            - weights[dimension]
                            + blocker_weights[dimension]
                            for dimension in range(num_dimensions)
                        ]
                        after_source_score = overload_score(
                            source, destination_after
                        )
                        if after_source_score >= before_source_score:
                            continue
                        destination = source
                        pair_delta = (
                            repair_delta(blocker, target, destination)
                            + source_delta
                        )
                        candidate = (
                            after_source_score,
                            pair_delta / relief,
                            pair_delta,
                            cluster_id,
                            blocker_id,
                            target,
                            destination,
                            vertex,
                            blocker,
                        )
                        if best_pair is None or candidate < best_pair:
                            best_pair = candidate
            if best_pair is None:
                # A two-move exchange is still insufficient when the target
                # must evict several complementary resource vectors, or when
                # its blocker itself needs room made in another partition.
                # Search a bounded deterministic ejection chain over the
                # complete set of structurally relevant blockers.  The root
                # move is evaluated atomically: intermediate states may exceed
                # a capacity, but the committed sequence must reduce the
                # selected source overload without worsening another existing
                # violation.
                movable_by_part_dimension: List[List[List[int]]] = [
                    [[] for _ in range(num_dimensions)]
                    for _ in range(num_parts)
                ]
                for part in range(num_parts):
                    for candidate_vertex in vertices_by_part[part]:
                        if labels[candidate_vertex] != part:
                            continue
                        candidate_id = cluster_order[candidate_vertex]
                        if clusters[candidate_id]["fixed_fpga"] is not None:
                            continue
                        for dimension, weight in enumerate(
                            vertex_weights[candidate_vertex]
                        ):
                            if weight:
                                movable_by_part_dimension[part][
                                    dimension
                                ].append(candidate_vertex)
                for part_indexes in movable_by_part_dimension:
                    for index in part_indexes:
                        index.sort(key=lambda item: cluster_order[item])

                def exact_sequence_delta(
                    overrides: Mapping[int, int],
                ) -> float:
                    affected_edges = {
                        edge_index
                        for vertex in overrides
                        for edge_index in incident_edges[vertex]
                    }
                    delta = 0.0
                    for edge_index in affected_edges:
                        before_cut = len(edge_part_counts[edge_index]) > 1
                        after_parts = {
                            overrides.get(vertex, labels[vertex])
                            for vertex in edge_vertices[edge_index]
                        }
                        delta += (
                            int(len(after_parts) > 1) - int(before_cut)
                        ) * repair_edge_weights[edge_index]
                    return delta

                def violation_key(
                    state_loads: Sequence[Sequence[int]],
                    limits: Sequence[Sequence[int]],
                ) -> Tuple[int, float, float, int]:
                    violations = []
                    absolute = 0
                    for part in range(num_parts):
                        for dimension in range(num_dimensions):
                            excess = max(
                                0,
                                state_loads[part][dimension]
                                - limits[part][dimension],
                            )
                            if not excess:
                                continue
                            normalized = excess / max(
                                1, allowed[part][dimension]
                            )
                            violations.append(normalized)
                            absolute += excess
                    return (
                        len(violations),
                        sum(violations),
                        max(violations, default=0.0),
                        absolute,
                    )

                # This is a fallback for rare, nearly legal provider outputs.
                # Its limits scale with the number of bins and resource
                # dimensions, not with a benchmark identity.  Beam pruning is
                # deterministic and prioritizes capacity progress before cut
                # cost; the final assignment is always checked by the ordinary
                # independent balance validator.
                max_chain_moves = max(
                    3, min(16, num_parts * 2 + num_dimensions)
                )
                beam_width = max(64, min(512, num_parts * 64))
                transition_width = max(64, min(256, num_parts * 64))

                def find_ejection_chain(
                    root_vertex: int,
                    root_target: int,
                ) -> Optional[Tuple[Tuple[int, int, int], ...]]:
                    root_weights = vertex_weights[root_vertex]
                    limits = [
                        [
                            max(
                                allowed[part][dimension],
                                loads[part][dimension],
                            )
                            for dimension in range(num_dimensions)
                        ]
                        for part in range(num_parts)
                    ]
                    source_after = [
                        loads[source][dimension] - root_weights[dimension]
                        for dimension in range(num_dimensions)
                    ]
                    limits[source] = [
                        max(
                            allowed[source][dimension],
                            source_after[dimension],
                        )
                        for dimension in range(num_dimensions)
                    ]
                    state_loads = [list(item) for item in loads]
                    for dimension, weight in enumerate(root_weights):
                        state_loads[source][dimension] -= weight
                        state_loads[root_target][dimension] += weight
                    root_move = (root_vertex, source, root_target)
                    root_overrides = ((root_vertex, root_target),)
                    root_state = (
                        tuple(tuple(item) for item in state_loads),
                        root_overrides,
                        (root_vertex,),
                        (root_move,),
                        repair_delta(root_vertex, source, root_target),
                    )
                    if violation_key(state_loads, limits)[0] == 0:
                        return (root_move,)
                    beam = [root_state]
                    for _ in range(1, max_chain_moves):
                        next_states = []
                        goals = []
                        seen = set()
                        for (
                            frozen_loads,
                            frozen_overrides,
                            frozen_moved,
                            sequence,
                            approximate_delta,
                        ) in beam:
                            state_loads = [
                                list(item) for item in frozen_loads
                            ]
                            overrides = dict(frozen_overrides)
                            moved = set(frozen_moved)
                            exceeded = []
                            for part in range(num_parts):
                                dimensions_exceeded = [
                                    dimension
                                    for dimension in range(num_dimensions)
                                    if state_loads[part][dimension]
                                    > limits[part][dimension]
                                ]
                                if not dimensions_exceeded:
                                    continue
                                part_excess = max(
                                    (
                                        state_loads[part][dimension]
                                        - limits[part][dimension]
                                    )
                                    / max(1, allowed[part][dimension])
                                    for dimension in dimensions_exceeded
                                )
                                exceeded.append(
                                    (-part_excess, part, dimensions_exceeded)
                                )
                            if not exceeded:
                                goals.append(
                                    (
                                        exact_sequence_delta(overrides),
                                        sequence,
                                    )
                                )
                                continue
                            _, blocked_part, dimensions_exceeded = min(
                                exceeded
                            )
                            pivot = min(
                                dimensions_exceeded,
                                key=lambda dimension: (
                                    len(
                                        movable_by_part_dimension[
                                            blocked_part
                                        ][dimension]
                                    ),
                                    dimension,
                                ),
                            )
                            deficits = [
                                max(
                                    0,
                                    state_loads[blocked_part][dimension]
                                    - limits[blocked_part][dimension],
                                )
                                for dimension in range(num_dimensions)
                            ]
                            transitions = []
                            for blocker in movable_by_part_dimension[
                                blocked_part
                            ][pivot]:
                                if blocker in moved:
                                    continue
                                if (
                                    overrides.get(blocker, labels[blocker])
                                    != blocked_part
                                ):
                                    continue
                                blocker_weights = vertex_weights[blocker]
                                relief = sum(
                                    min(
                                        blocker_weights[dimension],
                                        deficits[dimension],
                                    )
                                    / max(
                                        1,
                                        allowed[blocked_part][dimension],
                                    )
                                    for dimension in range(num_dimensions)
                                )
                                if relief <= 0.0:
                                    continue
                                for destination in range(num_parts):
                                    if destination == blocked_part:
                                        continue
                                    candidate_loads = [
                                        list(item) for item in state_loads
                                    ]
                                    for dimension, weight in enumerate(
                                        blocker_weights
                                    ):
                                        candidate_loads[blocked_part][
                                            dimension
                                        ] -= weight
                                        candidate_loads[destination][
                                            dimension
                                        ] += weight
                                    move_delta = repair_delta(
                                        blocker,
                                        blocked_part,
                                        destination,
                                    )
                                    move = (
                                        blocker,
                                        blocked_part,
                                        destination,
                                    )
                                    transitions.append(
                                        (
                                            violation_key(
                                                candidate_loads, limits
                                            ),
                                            move_delta / relief,
                                            move_delta,
                                            cluster_order[blocker],
                                            destination,
                                            candidate_loads,
                                            move,
                                        )
                                    )
                            for transition in heapq.nsmallest(
                                transition_width, transitions
                            ):
                                (
                                    key,
                                    _,
                                    move_delta,
                                    _,
                                    destination,
                                    candidate_loads,
                                    move,
                                ) = transition
                                blocker = move[0]
                                candidate_overrides = dict(overrides)
                                candidate_overrides[blocker] = destination
                                candidate_sequence = sequence + (move,)
                                candidate_moved = tuple(
                                    sorted((*moved, blocker))
                                )
                                frozen_candidate_loads = tuple(
                                    tuple(item) for item in candidate_loads
                                )
                                state_identity = (
                                    frozen_candidate_loads,
                                    tuple(
                                        sorted(candidate_overrides.items())
                                    ),
                                )
                                if state_identity in seen:
                                    continue
                                seen.add(state_identity)
                                if key[0] == 0:
                                    goals.append(
                                        (
                                            exact_sequence_delta(
                                                candidate_overrides
                                            ),
                                            candidate_sequence,
                                        )
                                    )
                                    continue
                                next_states.append(
                                    (
                                        key,
                                        approximate_delta + move_delta,
                                        tuple(
                                            (
                                                cluster_order[move_vertex],
                                                move_source,
                                                move_target,
                                            )
                                            for (
                                                move_vertex,
                                                move_source,
                                                move_target,
                                            ) in candidate_sequence
                                        ),
                                        frozen_candidate_loads,
                                        tuple(
                                            sorted(
                                                candidate_overrides.items()
                                            )
                                        ),
                                        candidate_moved,
                                        candidate_sequence,
                                    )
                                )
                        if goals:
                            return min(goals)[1]
                        next_states.sort(key=lambda item: item[:3])
                        beam = [
                            (
                                item[3],
                                item[4],
                                item[5],
                                item[6],
                                item[1],
                            )
                            for item in next_states[:beam_width]
                        ]
                        if not beam:
                            break
                    return None

                best_chain = None
                for (
                    source_without_score,
                    cluster_id,
                    vertex,
                    relief,
                ) in source_candidates:
                    for target in range(num_parts):
                        if target == source:
                            continue
                        chain = find_ejection_chain(vertex, target)
                        if chain is None:
                            continue
                        overrides = {
                            moved_vertex: destination
                            for moved_vertex, _, destination in chain
                        }
                        chain_delta = exact_sequence_delta(overrides)
                        candidate = (
                            len(chain),
                            source_without_score,
                            chain_delta / relief,
                            chain_delta,
                            cluster_id,
                            tuple(
                                (
                                    cluster_order[moved_vertex],
                                    move_source,
                                    destination,
                                )
                                for (
                                    moved_vertex,
                                    move_source,
                                    destination,
                                ) in chain
                            ),
                            chain,
                        )
                        if best_chain is None or candidate < best_chain:
                            best_chain = candidate
                    if best_chain is not None and best_chain[0] == 3:
                        # Direct and two-move solutions were already proven
                        # absent, so three moves is globally minimal.
                        break
                if best_chain is None:
                    raise ValidationError(
                        "TritonPart balance repair found no legal direct, "
                        "two-move, or bounded ejection-chain repair for "
                        f"{fpga_order[source]!r}; overload={overload(source)}"
                    )
                chain = best_chain[-1]
                for vertex, move_source, destination in chain:
                    apply_move(vertex, move_source, destination)
                ejection_chain_sequences += 1
                max_ejection_chain_moves = max(
                    max_ejection_chain_moves, len(chain)
                )
                continue
            (
                _,
                _,
                _,
                _,
                _,
                target,
                destination,
                vertex,
                blocker,
            ) = best_pair
            apply_move(blocker, target, destination)
            apply_move(vertex, source, target)
            paired_move_sequences += 1

    (
        final_cut_edges,
        final_cut_weight,
        final_combinational_cut_edges,
    ) = cut_summary()
    validate_cluster_assignment_balance(
        platform,
        clusters_artifact["clusters"],
        assignment,
        constraints["balance_tolerance"],
        constraints.get("balance_tolerance_by_dimension", {}),
        constraints.get("active_fpgas"),
        constraints.get("balance_dimensions"),
    )
    return assignment, {
        "moves": move_total,
        "paired_move_sequences": paired_move_sequences,
        "ejection_chain_sequences": ejection_chain_sequences,
        "max_ejection_chain_moves": max_ejection_chain_moves,
        "move_sha256": move_digest.hexdigest(),
        "move_counts": {
            f"{fpga_order[source]}->{fpga_order[target]}": count
            for (source, target), count in sorted(move_counts.items())
        },
        "moved_weights": {
            f"{fpga_order[source]}->{fpga_order[target]}": {
                dimension: value
                for dimension, value in zip(
                    dimensions,
                    moved_weights[(source, target)],
                )
            }
            for source, target in sorted(moved_weights)
        },
        "dimensions": dimensions,
        "initial_loads": {
            fpga_order[part]: {
                dimension: value
                for dimension, value in zip(
                    dimensions,
                    initial_loads[part],
                )
            }
            for part in range(num_parts)
        },
        "final_loads": {
            fpga_order[part]: {
                dimension: value
                for dimension, value in zip(dimensions, loads[part])
            }
            for part in range(num_parts)
        },
        "allowed_loads": {
            fpga_order[part]: {
                dimension: value
                for dimension, value in zip(dimensions, allowed[part])
            }
            for part in range(num_parts)
        },
        "initial_cut_hyperedges": initial_cut_edges,
        "final_cut_hyperedges": final_cut_edges,
        "initial_combinational_cut_hyperedges": (
            initial_combinational_cut_edges
        ),
        "final_combinational_cut_hyperedges": (
            final_combinational_cut_edges
        ),
        "initial_cut_weight": initial_cut_weight,
        "final_cut_weight": final_cut_weight,
        "estimated_cut_delta": estimated_cut_delta,
        "repair_objective_delta": repair_objective_delta,
        "combinational_cut_penalty": combinational_cut_penalty,
    }


def _parse_native_balance_report(
    path: Path, tritonpart_input: Mapping[str, Any]
) -> Dict[str, Any]:
    if not path.is_file():
        raise ValidationError("native TritonPart balance report is missing")
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "EMUFLOW_TRITONPART_BALANCE_REPORT_V1":
        raise ValidationError("invalid native TritonPart balance report header")
    status = None
    saw_end = False
    saw_error = False
    metrics: Dict[str, int] = {}
    loads: Dict[str, Dict[str, Dict[str, int]]] = {
        kind: {} for kind in ("initial", "final", "allowed")
    }
    fpga_order = tritonpart_input["fpga_order"]
    dimensions = tritonpart_input["vertex_dimensions"]

    def report_integer(token: str) -> int:
        try:
            return int(token)
        except ValueError as error:
            raise ValidationError(
                "native TritonPart balance report contains a non-integer"
            ) from error

    for line_index, line in enumerate(lines[1:], start=1):
        fields = line.split()
        if fields == ["END"]:
            if line_index != len(lines) - 1:
                raise ValidationError(
                    "native TritonPart balance report has trailing records"
                )
            saw_end = True
            break
        if len(fields) == 2 and fields[0] == "STATUS":
            if status is not None:
                raise ValidationError(
                    "native TritonPart balance report repeats status"
                )
            status = fields[1].lower()
        elif len(fields) == 3 and fields[0] == "METRIC":
            if fields[1] in metrics:
                raise ValidationError(
                    "native TritonPart balance report repeats metric"
                )
            value = report_integer(fields[2])
            if value < 0:
                raise ValidationError(
                    "native TritonPart balance metric is negative"
                )
            metrics[fields[1]] = value
        elif fields and fields[0] in {"INITIAL", "FINAL", "ALLOWED"}:
            if len(fields) != len(dimensions) + 2:
                raise ValidationError("invalid native balance load record")
            part = report_integer(fields[1])
            if part < 0 or part >= len(fpga_order):
                raise ValidationError("native balance part index is invalid")
            kind = fields[0].lower()
            fpga_id = fpga_order[part]
            if fpga_id in loads[kind]:
                raise ValidationError(
                    "native TritonPart balance report repeats load record"
                )
            record = {
                dimension: report_integer(value)
                for dimension, value in zip(dimensions, fields[2:])
            }
            if any(value < 0 for value in record.values()):
                raise ValidationError(
                    "native TritonPart balance load is negative"
                )
            loads[kind][fpga_id] = record
        elif fields and fields[0] == "ERROR":
            saw_error = True
        else:
            raise ValidationError("invalid native TritonPart balance record")
    if not saw_end or status != "pass" or saw_error or set(metrics) != {
        "moves",
        "direct_moves",
        "paired_sequences",
        "ejection_chain_sequences",
        "replayed_chain_sequences",
        "max_ejection_chain_moves",
        "elapsed_milliseconds",
    }:
        raise ValidationError("native TritonPart balance report is incomplete")
    expected_parts = set(fpga_order)
    if any(set(records) != expected_parts for records in loads.values()):
        raise ValidationError("native TritonPart balance loads are incomplete")
    expected_allowed = tritonpart_input["balance_allowed_loads"]
    if loads["allowed"] != expected_allowed:
        raise ValidationError("native TritonPart balance limits changed")
    for fpga in fpga_order:
        for dimension in dimensions:
            if loads["final"][fpga][dimension] > expected_allowed[fpga][dimension]:
                raise ValidationError("native TritonPart balance result exceeds limit")
    return {
        **metrics,
        "provider": "emuflow-native-tritonpart-balance-legalizer-v1",
        "initial_loads": loads["initial"],
        "final_loads": loads["final"],
        "allowed_loads": loads["allowed"],
    }


def _native_balance_assignment_loads(
    assignment: Mapping[str, str],
    tritonpart_input: Mapping[str, Any],
) -> Dict[str, Dict[str, int]]:
    fpga_order = tritonpart_input["fpga_order"]
    dimensions = tritonpart_input["vertex_dimensions"]
    cluster_order = tritonpart_input["cluster_order"]
    vertex_weights = tritonpart_input["vertex_weights"]
    if (
        len(cluster_order) != len(vertex_weights)
        or len(assignment) != len(cluster_order)
    ):
        raise ValidationError(
            "native TritonPart balance assignment coverage changed"
        )
    loads = {
        fpga_id: {dimension: 0 for dimension in dimensions}
        for fpga_id in fpga_order
    }
    for cluster_id, row in zip(
        cluster_order,
        vertex_weights,
    ):
        try:
            fpga_id = assignment[cluster_id]
        except KeyError as error:
            raise ValidationError(
                "native TritonPart balance assignment coverage changed"
            ) from error
        if fpga_id not in loads or len(row) != len(dimensions):
            raise ValidationError(
                "native TritonPart balance assignment dimensions changed"
            )
        for dimension, value in zip(dimensions, row):
            loads[fpga_id][dimension] += int(value)
    return loads


def _run_native_balance_legalizer(
    candidate: Mapping[str, str],
    tritonpart_input: Mapping[str, Any],
    output_dir: Path,
    input_solution: Path,
    output_solution: Path,
    *,
    executable: Optional[str],
    timeout_seconds: int,
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    limits_path = output_dir / "balance-limits.txt"
    report_path = output_dir / "balance-repair-report.txt"
    log_path = output_dir / "balance-repair.log"
    fpga_order = tritonpart_input["fpga_order"]
    dimensions = tritonpart_input["vertex_dimensions"]
    allowed = tritonpart_input["balance_allowed_loads"]
    limits_path.write_text(
        "\n".join([
            "EMUFLOW_TRITONPART_BALANCE_LIMITS_V1",
            f"PARAM {len(fpga_order)} {len(dimensions)}",
            *(
                "CAP " + str(index) + " "
                + " ".join(str(allowed[fpga][field]) for field in dimensions)
                for index, fpga in enumerate(fpga_order)
            ),
            "END",
            "",
        ]),
        encoding="utf-8",
    )
    resolved = resolve_native_executable(
        "emuflow_tritonpart_balance_legalizer", executable
    )
    files = tritonpart_input["files"]
    output_solution.unlink(missing_ok=True)
    report_path.unlink(missing_ok=True)
    command = [
        resolved,
        str((output_dir / files["hypergraph"]).resolve()),
        str((output_dir / files["fixed"]).resolve()),
        str((output_dir / files["edge_classes"]).resolve()),
        str(limits_path.resolve()),
        str(input_solution.resolve()),
        str(output_solution.resolve()),
        str(report_path.resolve()),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=output_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise ValidationError(
            f"native TritonPart balance repair exceeded {timeout_seconds} seconds"
        ) from error
    log_path.write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        detail = completed.stdout[-2000:]
        raise ValidationError(
            "native TritonPart balance legalizer failed with exit code "
            f"{completed.returncode}: {detail}"
        )
    result = parse_tritonpart_solution(output_solution, tritonpart_input)
    if len(result) != len(candidate) or any(
        cluster_id not in result for cluster_id in candidate
    ):
        raise ValidationError(
            "native TritonPart balance assignment coverage changed"
        )
    summary = _parse_native_balance_report(report_path, tritonpart_input)
    if summary["initial_loads"] != _native_balance_assignment_loads(
        candidate, tritonpart_input
    ):
        raise ValidationError(
            "native TritonPart balance initial loads do not match assignment"
        )
    if summary["final_loads"] != _native_balance_assignment_loads(
        result, tritonpart_input
    ):
        raise ValidationError(
            "native TritonPart balance final loads do not match assignment"
        )
    summary.update({
        "executable": resolved,
        "solution": output_solution.name,
        "report": report_path.name,
        "log": log_path.name,
    })
    return result, summary


def _stream_assignment_cut_summary(
    hyperedges: Iterable[Mapping[str, Any]],
    assignment: Mapping[str, str],
    timed_nets: AbstractSet[str],
) -> Dict[str, float | int]:
    """Accumulate assignment cut metrics without retaining decoded edges."""

    cut_hyperedges = 0
    cut_weight = 0.0
    timed_cut_hyperedges = 0
    timed_cut_weight = 0.0
    for edge in hyperedges:
        clusters = iter(edge["clusters"])
        try:
            first_fpga = assignment[next(clusters)]
        except StopIteration:
            continue
        if not any(assignment[cluster_id] != first_fpga for cluster_id in clusters):
            continue
        weight = float(edge["weight"])
        cut_hyperedges += 1
        cut_weight += weight
        if edge["net"] in timed_nets:
            timed_cut_hyperedges += 1
            timed_cut_weight += weight
    return {
        "cut_hyperedges": cut_hyperedges,
        "cut_weight": cut_weight,
        "timed_cut_hyperedges": timed_cut_hyperedges,
        "timed_cut_weight": timed_cut_weight,
    }


def run_tritonpart(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    output_dir: Path,
    seed: int,
    executable: Optional[str] = None,
    solution_input: Optional[Path] = None,
    net_weights: Optional[Mapping[str, float]] = None,
    timeout_seconds: int = 3600,
    seed_attempts: int = 1,
    num_initial_solutions: int = 50,
    num_best_initial_solutions: int = 10,
    repair_min_used_fpgas: bool = False,
    repair_balance: bool = False,
    balance_legalizer: Optional[str] = None,
    persist_input_manifest: bool = True,
    run_unweighted_baseline: bool = False,
    defer_semantic_contract: bool = False,
) -> Dict[str, Any]:
    if seed_attempts <= 0:
        raise ValueError("TritonPart seed_attempts must be positive")
    if solution_input is not None and seed_attempts != 1:
        raise ValueError(
            "precomputed TritonPart solutions require seed_attempts=1"
        )
    tritonpart_input = phase3_runtime_call(
        "tritonpart.export_inputs",
        export_tritonpart_inputs,
        ir,
        platform,
        clusters_artifact,
        constraints,
        output_dir,
        net_weights=net_weights,
        num_initial_solutions=num_initial_solutions,
        num_best_initial_solutions=num_best_initial_solutions,
        write_manifest=False,
    )
    compact_hyperedges = tritonpart_input["hyperedges"]
    if isinstance(compact_hyperedges, _PackedHyperedgeRecords):
        phase3_runtime_observe(
            tritonpart_compact_hyperedges=len(compact_hyperedges),
            tritonpart_compact_hyperedge_payload_bytes=(
                compact_hyperedges.payload_bytes
            ),
        )
    num_parts = len(tritonpart_input["fpga_order"])
    tritonpart_input["seed"] = seed
    if persist_input_manifest:
        # Standalone qualification retains one human-inspectable manifest.
        # Managed production consumes the native files directly and must not
        # serialize this large duplicate only to delete it after selection.
        write_json(
            output_dir / "tritonpart_input.json", tritonpart_input, compact=True
        )
    tcl_path = output_dir / tritonpart_input["files"]["tcl"]
    tcl_template = tcl_path.read_text(encoding="utf-8")

    solution_path = output_dir / tritonpart_input["files"]["solution"]
    resolved_executable: Optional[str] = None
    mode = "execute"
    if solution_input is None:
        resolved_executable = resolve_native_executable(
            "openroad", executable
        )

    cluster_assignment: Optional[Dict[str, str]] = None
    selected_seed = seed
    selected_log: Optional[Path] = None
    selected_solution_path: Optional[Path] = None
    selected_repair_moves: List[Dict[str, Any]] = []
    selected_balance_repair: Optional[Dict[str, Any]] = None
    selected_timing_cut_summary: Optional[Dict[str, float | int]] = None
    selected_objective: Optional[Tuple[Any, ...]] = None
    selected_attempt_mode: Optional[str] = None
    attempts = []
    timed_nets = set(net_weights or {})
    attempt_specs = [
        {
            "mode": "timing_weighted",
            "seed": seed + offset,
            "hypergraph": output_dir
            / tritonpart_input["files"]["hypergraph"],
        }
        for offset in range(seed_attempts)
    ]
    baseline_name = tritonpart_input["files"][
        "unweighted_baseline_hypergraph"
    ]
    if (
        run_unweighted_baseline
        and net_weights
        and baseline_name is not None
        and solution_input is None
    ):
        attempt_specs.append(
            {
                "mode": "unweighted_baseline",
                "seed": seed,
                "hypergraph": output_dir / baseline_name,
            }
        )
    weighted_hypergraph = (
        output_dir / tritonpart_input["files"]["hypergraph"]
    ).resolve()
    for spec in attempt_specs:
        attempt_seed = spec["seed"]
        provider_solution_path = (
            solution_path
            if spec["mode"] == "timing_weighted"
            else Path(
                f"{spec['hypergraph']}.part.{num_parts}"
            )
        )
        tcl_text = tcl_template.replace(
            "  -seed 0 \\\n", f"  -seed {attempt_seed} \\\n"
        )
        tcl_text = tcl_text.replace(
            str(weighted_hypergraph),
            str(spec["hypergraph"].resolve()),
        )
        tcl_path.write_text(tcl_text, encoding="utf-8")
        tritonpart_input["seed"] = attempt_seed
        if solution_input is not None:
            if not solution_input.is_file():
                raise ValidationError(
                    "precomputed TritonPart solution does not exist: "
                    f"{solution_input}"
                )
            if solution_input.resolve() != solution_path.resolve():
                phase3_runtime_call(
                    "tritonpart.import_solution",
                    shutil.copyfile,
                    solution_input,
                    solution_path,
                )
            mode = "import"
            log_path = None
        else:
            if spec["mode"] == "unweighted_baseline":
                log_name = (
                    "openroad-tritonpart.unweighted-baseline."
                    f"seed-{attempt_seed}.log"
                )
            else:
                log_name = (
                    "openroad-tritonpart.log"
                    if len(attempt_specs) == 1
                    else f"openroad-tritonpart.seed-{attempt_seed}.log"
                )
            log_path = output_dir / log_name
            if provider_solution_path.exists():
                provider_solution_path.unlink()
            try:
                completed = phase3_runtime_call(
                    "tritonpart.execute_native",
                    subprocess.run,
                    [resolved_executable, "-exit", str(tcl_path.resolve())],
                    cwd=output_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired as error:
                raise EmuFlowError(
                    f"TritonPart exceeded timeout of {timeout_seconds} seconds"
                ) from error
            log_path.write_text(completed.stdout, encoding="utf-8")
            if completed.returncode != 0:
                tail = "\n".join(completed.stdout.splitlines()[-30:])
                raise EmuFlowError(
                    "OpenROAD/TritonPart failed with exit code "
                    f"{completed.returncode}\n{tail}"
                )
            if not provider_solution_path.is_file():
                raise EmuFlowError(
                    "OpenROAD/TritonPart reported success but did not create "
                    f"{provider_solution_path}"
                )

        candidate = phase3_runtime_call(
            "tritonpart.parse_solution",
            parse_tritonpart_solution,
            provider_solution_path,
            tritonpart_input,
        )
        attempt_solution = None
        if len(attempt_specs) > 1:
            solution_stem = (
                f"partition.hgr.seed-{attempt_seed}"
                if spec["mode"] == "timing_weighted"
                else (
                    "partition.hgr.unweighted-baseline."
                    f"seed-{attempt_seed}"
                )
            )
            attempt_solution = output_dir / (
                f"{solution_stem}.part.{num_parts}"
            )
            shutil.copyfile(provider_solution_path, attempt_solution)
        raw_used_fpgas = len(set(candidate.values()))
        balance_repair = None
        repaired_solution_path = None
        if repair_balance:
            repaired_tag = (
                f"seed-{attempt_seed}"
                if spec["mode"] == "timing_weighted"
                else f"unweighted-baseline.seed-{attempt_seed}"
            )
            repaired_solution_path = output_dir / (
                (
                    f"partition.hgr.{repaired_tag}.repaired.part."
                    f"{num_parts}"
                )
                if len(attempt_specs) > 1
                else f"partition.hgr.repaired.part.{num_parts}"
            )
            try:
                candidate, balance_repair = phase3_runtime_call(
                    "tritonpart.balance_legalizer",
                    _run_native_balance_legalizer,
                    candidate,
                    tritonpart_input,
                    output_dir,
                    provider_solution_path,
                    repaired_solution_path,
                    executable=balance_legalizer,
                    timeout_seconds=timeout_seconds,
                )
            except ValidationError as error:
                attempts.append(
                    {
                        "mode": spec["mode"],
                        "seed": attempt_seed,
                        "raw_used_fpgas": raw_used_fpgas,
                        "used_fpgas": raw_used_fpgas,
                        "balance_repair": None,
                        "repair_moves": [],
                        "solution": (
                            attempt_solution.name
                            if attempt_solution is not None
                            else solution_path.name
                        ),
                        "repaired_solution": None,
                        "log": (
                            log_path.name if log_path is not None else None
                        ),
                        "accepted": False,
                        "rejection": "balance_repair",
                        "error": str(error),
                    }
                )
                continue
        repair_moves: List[Dict[str, Any]] = []
        if (
            repair_min_used_fpgas
            and raw_used_fpgas < constraints["min_used_fpgas"]
        ):
            candidate, repair_moves = _repair_min_used_fpgas(
                candidate,
                clusters_artifact,
                platform,
                constraints,
                tritonpart_input["hyperedges"],
            )
        used_fpgas = len(set(candidate.values()))
        attempt: Dict[str, Any] = {
            "mode": spec["mode"],
            "seed": attempt_seed,
            "raw_used_fpgas": raw_used_fpgas,
            "used_fpgas": used_fpgas,
            "balance_repair": balance_repair,
            "repair_moves": repair_moves,
            "solution": (
                attempt_solution.name
                if attempt_solution is not None
                else solution_path.name
            ),
            "repaired_solution": (
                repaired_solution_path.name
                if repaired_solution_path is not None
                else None
            ),
            "log": log_path.name if log_path is not None else None,
        }
        if used_fpgas < constraints["min_used_fpgas"]:
            attempt["accepted"] = False
            attempt["rejection"] = "min_used_fpgas"
            attempts.append(attempt)
            continue
        try:
            attempt["balance_validation"] = (
                validate_cluster_assignment_balance(
                    platform,
                    clusters_artifact["clusters"],
                    candidate,
                    constraints["balance_tolerance"],
                    constraints.get("balance_tolerance_by_dimension", {}),
                    constraints.get("active_fpgas"),
                    constraints.get("balance_dimensions"),
                )
            )
        except ValidationError as error:
            attempt["accepted"] = False
            attempt["rejection"] = "multi_resource_balance"
            attempt["error"] = str(error)
            attempts.append(attempt)
            continue

        attempt["accepted"] = True
        cut_summary = _stream_assignment_cut_summary(
            tritonpart_input["hyperedges"], candidate, timed_nets
        )
        attempt["cut_hyperedges"] = cut_summary["cut_hyperedges"]
        attempt["cut_weight"] = cut_summary["cut_weight"]
        attempts.append(attempt)
        objective = (
            float(attempt["cut_weight"]),
            int(attempt["cut_hyperedges"]),
            attempt_seed,
        )
        if selected_objective is None or objective < selected_objective:
            cluster_assignment = candidate
            selected_seed = attempt_seed
            selected_objective = objective
            selected_attempt_mode = spec["mode"]
            selected_log = log_path
            selected_solution_path = (
                repaired_solution_path
                if repaired_solution_path is not None
                else (
                    attempt_solution
                    if attempt_solution is not None
                    else provider_solution_path
                )
            )
            selected_repair_moves = repair_moves
            selected_balance_repair = balance_repair
            selected_timing_cut_summary = {
                "timed_cut_hyperedges": cut_summary["timed_cut_hyperedges"],
                "timed_cut_weight": cut_summary["timed_cut_weight"],
            }

    if cluster_assignment is None:
        raise ValidationError(
            "TritonPart seed sweep produced no independently acceptable "
            "assignment; "
            f"attempts={attempts}"
        )
    if (
        len(attempt_specs) > 1
        and selected_solution_path is not None
        and selected_solution_path.resolve() != solution_path.resolve()
    ):
        shutil.copyfile(selected_solution_path, solution_path)
    tritonpart_input["seed"] = selected_seed
    if selected_timing_cut_summary is None:
        raise ValidationError("TritonPart selected cut summary is missing")
    timing_weight_result = {
        **tritonpart_input["timing_weight_coverage"],
        **selected_timing_cut_summary,
    }
    provider_metadata = {
        "mode": mode,
        "selected_attempt_mode": selected_attempt_mode,
        "executable": resolved_executable,
        "input_schema": TRITONPART_INPUT_SCHEMA,
        "vertex_dimensions": tritonpart_input["vertex_dimensions"],
        "hyperedges": len(tritonpart_input["hyperedges"]),
        "timing_weights": timing_weight_result,
        "requested_balance_percent": tritonpart_input[
            "requested_balance_percent"
        ],
        "effective_balance_percent": tritonpart_input[
            "effective_balance_percent"
        ],
        "tritonpart_ubfactor_percent_points": tritonpart_input[
            "tritonpart_ubfactor_percent_points"
        ],
        "balance_auto_relaxed": tritonpart_input["balance_auto_relaxed"],
        "seed_attempts": attempts,
        "search_effort": tritonpart_input["search_effort"],
        "objective_encoding": tritonpart_input["objective_encoding"],
        "objective_components": tritonpart_input["objective_components"],
        "min_used_fpgas_repair": {
            "enabled": repair_min_used_fpgas,
            "moves": selected_repair_moves,
        },
        "balance_repair": {
            "enabled": repair_balance,
            "summary": selected_balance_repair,
        },
        "artifacts": (
            {
                **tritonpart_input["files"],
                "input": "tritonpart_input.json",
                "log": (
                    selected_log.name
                    if selected_log is not None and mode == "execute"
                    else None
                ),
            }
            if persist_input_manifest
            else {"retained": False}
        ),
    }
    return phase3_runtime_call(
        "tritonpart.build_assignment",
        build_partition_assignment,
        ir,
        platform,
        clusters_artifact,
        constraints,
        cluster_assignment,
        provider=TRITONPART_PROVIDER,
        seed=selected_seed,
        provider_metadata=provider_metadata,
        _include_semantic_contract=not defer_semantic_contract,
        _stream_semantic_records=defer_semantic_contract,
    )
