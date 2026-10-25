"""Topology-aware Phase 3 legality audit and native FM refinement."""

from __future__ import annotations

import hashlib
import math
import json
import os
from array import array
from collections import deque
from pathlib import Path
from typing import (
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
from .io import read_json
from .ir import EmuIR
from .native_tools import is_unified_native_executable, resolve_native_stage_command
from .tool_runtime import NativeToolProcess, native_tool_runner
from .partition import (
    _CompactClusterRecords,
    _PackedClusterAssignment,
    _compact_partition_ir_path,
    active_partition_fpgas,
    build_partition_assignment,
    transported_cut_classes_for_clusters,
    validate_cluster_assignment_balance,
)
from .partition_limits import planning_resource_capacities
from .platform import Platform
from .phase3_runtime import phase3_runtime_observe
from .phase3_storage import read_phase3_assignment_view
from .replication import apply_replication
from .resources import RESOURCE_FIELDS
from .routing import build_directed_graph, load_route_constraints
from .tritonpart import _CompactClusterOrder, load_partition_net_weights


HOP_REFINER_INPUT_SCHEMA = "emuflow.hop-partition-refiner-input/v1"
HOP_REFINER_REPORT_SCHEMA = "emuflow.hop-partition-refinement/v1"
HOP_REFINER_PROVIDER = "topology-constrained-fm-v1"


class _PackedHopNetRecords(list):
    """Directed hop-refiner nets stored as fixed-width cluster ordinals."""

    def __init__(self) -> None:
        super().__init__()
        self.name_bytes = bytearray()
        self.name_offsets = array("Q", [0])
        self.weights = array("d")
        self.sources = array("I")
        self.sink_offsets = array("Q", [0])
        self.sinks = array("I")

    def append_record(
        self,
        net_id: str,
        weight: float,
        source: int,
        sinks: Sequence[int],
    ) -> None:
        encoded = net_id.encode("utf-8")
        self.name_bytes.extend(encoded)
        self.name_offsets.append(len(self.name_bytes))
        self.weights.append(float(weight))
        self.sources.append(source)
        self.sinks.extend(sinks)
        self.sink_offsets.append(len(self.sinks))

    def __len__(self) -> int:
        return len(self.sources)

    def iter_columns(self) -> Iterator[Tuple[float, int, array]]:
        for index, (weight, source) in enumerate(
            zip(self.weights, self.sources)
        ):
            begin = self.sink_offsets[index]
            end = self.sink_offsets[index + 1]
            yield weight, source, self.sinks[begin:end]

    def _record(self, index: int) -> Dict[str, Any]:
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError("packed hop net index out of range")
        name_begin = self.name_offsets[index]
        name_end = self.name_offsets[index + 1]
        sink_begin = self.sink_offsets[index]
        sink_end = self.sink_offsets[index + 1]
        source = self.sources[index]
        return {
            "net": self.name_bytes[name_begin:name_end].decode("utf-8"),
            "weight": self.weights[index],
            "source": f"c{source:06d}",
            "sinks": [
                f"c{sink:06d}" for sink in self.sinks[sink_begin:sink_end]
            ],
        }

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for index in range(len(self)):
            yield self._record(index)

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, slice):
            return [self._record(item) for item in range(*index.indices(len(self)))]
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("packed hop net index must be an integer")
        return self._record(index)

    @property
    def payload_bytes(self) -> int:
        columns = (
            self.name_offsets,
            self.weights,
            self.sources,
            self.sink_offsets,
            self.sinks,
        )
        return len(self.name_bytes) + sum(
            len(column) * column.itemsize for column in columns
        )


def _shortest_hop_distances(
    platform: Platform,
    route_constraints: Mapping[str, Any],
) -> Dict[str, Dict[str, Optional[int]]]:
    adjacency, _, _ = build_directed_graph(platform, route_constraints)
    result: Dict[str, Dict[str, Optional[int]]] = {}
    for source in sorted(adjacency):
        distances = {source: 0}
        queue = deque([source])
        while queue:
            node = queue.popleft()
            for arc in adjacency[node]:
                sink = arc["to"]
                if sink in distances:
                    continue
                distances[sink] = distances[node] + 1
                queue.append(sink)
        result[source] = {
            sink: distances.get(sink) for sink in sorted(adjacency)
        }
    return result


def audit_assignment_hops(
    assignment: Mapping[str, Any],
    distances: Mapping[str, Mapping[str, Optional[int]]],
    hop_limit: int,
) -> Dict[str, Any]:
    violations = []
    routed_endpoints = 0
    total_hops = 0
    maximum_hops = 0
    unreachable = 0
    for cut in assignment["cut_nets"]:
        sources = cut.get("source_fpgas", [])
        if len(sources) != 1:
            raise ValidationError(
                f"hop audit requires one source FPGA for net {cut['net']!r}"
            )
        source = sources[0]
        for sink in sorted(set(cut["sink_fpgas"])):
            routed_endpoints += 1
            hops = distances[source][sink]
            if hops is None:
                unreachable += 1
            else:
                total_hops += hops
                maximum_hops = max(maximum_hops, hops)
            if hops is None or hops > hop_limit:
                violations.append(
                    {
                        "net": cut["net"],
                        "source": source,
                        "sink": sink,
                        "hops": hops,
                        "limit": hop_limit,
                    }
                )
    return {
        "status": "pass" if not violations else "violation",
        "max_route_hops": hop_limit,
        "routed_net_sink_pairs": routed_endpoints,
        "violating_net_sink_pairs": len(violations),
        "unreachable_net_sink_pairs": unreachable,
        "maximum_observed_hops": maximum_hops,
        "total_shortest_path_hops": total_hops,
        "violations": violations[:64],
        "violations_truncated": max(0, len(violations) - 64),
    }


def validate_assignment_hop_constraints(
    assignment_path: Path,
    platform_path: Path,
    route_constraints_path: Path,
) -> Dict[str, Any]:
    """Independently recheck Phase 3 reachability and maximum-hop legality."""

    platform = Platform.load(platform_path)
    constraints = load_route_constraints(route_constraints_path, platform)
    hop_limit = constraints.get("max_route_hops")
    if hop_limit is None:
        return {"status": "not-requested", "max_route_hops": None}
    audit = audit_assignment_hops(
        read_phase3_assignment_view(assignment_path),
        _shortest_hop_distances(platform, constraints),
        hop_limit,
    )
    if audit["status"] != "pass":
        raise ValidationError(
            "partition assignment violates route reachability/max-hop constraints"
        )
    return audit


def validate_assignment_hops(
    platform: Platform,
    assignment: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
) -> Dict[str, Any]:
    """Check an in-memory assignment without materializing refiner input."""

    hop_limit = route_constraints.get("max_route_hops")
    if hop_limit is None:
        return {"status": "not-requested", "max_route_hops": None}
    audit = audit_assignment_hops(
        assignment,
        _shortest_hop_distances(platform, route_constraints),
        hop_limit,
    )
    if audit["status"] != "pass":
        raise ValidationError(
            "partition assignment violates route reachability/max-hop "
            "constraints"
        )
    return audit


def _primary_net_records(
    ir: EmuIR,
    clusters_artifact: Mapping[str, Any],
    net_weights: Mapping[str, float],
) -> List[Dict[str, Any]]:
    cluster_by_instance = {
        instance_id: cluster["id"]
        for cluster in clusters_artifact["clusters"]
        for instance_id in cluster["instances"]
    }
    records = []
    transported_cut_classes = transported_cut_classes_for_clusters(
        clusters_artifact
    )
    for net in ir.value["nets"]:
        if net["cut_class"] not in transported_cut_classes:
            continue
        sources = sorted(
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
        if not sources or not sinks:
            continue
        if len(sources) != 1:
            raise ValidationError(
                f"hop refinement requires one driver cluster for net {net['id']!r}"
            )
        sinks = [cluster for cluster in sinks if cluster != sources[0]]
        if sinks:
            records.append(
                {
                    "net": net["id"],
                    "weight": float(net_weights.get(net["id"], 1.0)),
                    "source": sources[0],
                    "sinks": sinks,
                }
            )
    return records


def _compact_primary_net_records(
    ir: EmuIR,
    clusters: _CompactClusterRecords,
    transported_cut_classes: Sequence[str],
    net_weights: Mapping[str, float],
) -> _PackedHopNetRecords:
    """Build directed net records from compact endpoint ownership."""

    from .emuir_storage import open_emuir_sqlite_read_only

    path = _compact_partition_ir_path(ir)
    if path is None:
        raise ValidationError("compact hop-refiner input has no EmuIR store")
    source_clusters = clusters.source_cluster_indices
    records = _PackedHopNetRecords()
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
            sources = set()
            sinks = set()
            while endpoint is not None and endpoint["net_seq"] == net_seq:
                role = endpoint["role"]
                source_seq = endpoint["instance_seq"]
                if role not in (0, 1):
                    raise ValidationError(
                        "compact EmuIR endpoint role is invalid"
                    )
                if source_seq is not None:
                    if source_seq < 0 or source_seq >= len(source_clusters):
                        raise ValidationError(
                            "compact EmuIR endpoint owner is invalid"
                        )
                    owner = source_clusters[source_seq]
                    (sources if role == 0 else sinks).add(owner)
                endpoint = next(endpoint_rows, None)
            if endpoint is not None and endpoint["net_seq"] < net_seq:
                raise ValidationError("compact EmuIR has orphaned endpoints")
            if net["cut_class"] not in transported_cut_classes:
                continue
            if not sources or not sinks:
                continue
            if len(sources) != 1:
                raise ValidationError(
                    "hop refinement requires one driver cluster for net "
                    f"{net['id']!r}"
                )
            source = next(iter(sources))
            ordered_sinks = sorted(sink for sink in sinks if sink != source)
            if ordered_sinks:
                records.append_record(
                    net["id"],
                    float(net_weights.get(net["id"], 1.0)),
                    source,
                    ordered_sinks,
                )
        if observed_nets != len(ir.value["nets"]) or endpoint is not None:
            raise ValidationError("compact EmuIR endpoint coverage is invalid")
    finally:
        connection.close()
    return records


def _replica_targets(
    assignment: Mapping[str, Any],
) -> Dict[str, List[str]]:
    replication = assignment.get("replication")
    if replication is None:
        return {}
    result: Dict[str, List[str]] = {}
    for record in replication["replicas"]:
        result.setdefault(record["cluster"], []).append(
            record["target_fpga"]
        )
    return {cluster: sorted(targets) for cluster, targets in result.items()}


def _write_native_input(
    path: Path,
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    partition_constraints: Mapping[str, Any],
    assignment: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    distances: Mapping[str, Mapping[str, Optional[int]]],
    net_weights: Mapping[str, float],
) -> Dict[str, Any]:
    hop_limit = route_constraints["max_route_hops"]
    assert hop_limit is not None
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
    partition_fpgas = active_partition_fpgas(
        platform, partition_constraints
    )
    fpga_ids = [fpga.id for fpga in partition_fpgas]
    fpga_index = {fpga_id: index for index, fpga_id in enumerate(fpga_ids)}
    balance = validate_cluster_assignment_balance(
        platform,
        clusters,
        assignment["cluster_assignment"],
        partition_constraints["balance_tolerance"],
        partition_constraints.get("balance_tolerance_by_dimension", {}),
        partition_constraints.get("active_fpgas"),
        partition_constraints.get("balance_dimensions", ()),
        partition_constraints.get("resource_reservations_by_fpga", {}),
    )
    planning_capacity = planning_resource_capacities(
        partition_fpgas,
        partition_constraints.get("resource_reservations_by_fpga", {}),
    )
    dimensions = balance["balance_dimensions"]
    transported = transported_cut_classes_for_clusters(clusters_artifact)
    nets: Sequence[Mapping[str, Any]] = (
        _compact_primary_net_records(
            ir, compact_clusters, transported, net_weights
        )
        if compact_clusters is not None
        else _primary_net_records(ir, clusters_artifact, net_weights)
    )
    cluster_order: Sequence[str] = (
        _CompactClusterOrder(len(compact_clusters))
        if compact_clusters is not None
        else [cluster["id"] for cluster in clusters]
    )
    temporary = path.with_name(f".{path.name}.tmp")
    digest = hashlib.sha256()
    try:
        with temporary.open("wb") as stream:
            def emit(line: str) -> None:
                record = (line + "\n").encode("ascii")
                stream.write(record)
                digest.update(record)

            emit("EMUFLOW_HOP_PARTITION_REFINER_INPUT_V1")
            emit(
                "PARAM "
                + " ".join(
                    str(value)
                    for value in (
                        len(fpga_ids),
                        len(clusters),
                        len(dimensions),
                        len(nets),
                        hop_limit,
                        partition_constraints["min_used_fpgas"],
                    )
                )
            )
            for source in fpga_ids:
                for sink in fpga_ids:
                    value = distances[source][sink]
                    emit(
                        f"DIST {fpga_index[source]} {fpga_index[sink]} "
                        f"{value if value is not None else -1}"
                    )
            for fpga in partition_fpgas:
                for dimension_index, dimension in enumerate(dimensions):
                    balance_bound = balance["balance_allowed_loads"][fpga.id][
                        dimension
                    ]
                    hard_capacity = (
                        math.ceil(balance_bound)
                        if dimension == "cells"
                        else planning_capacity[fpga.id][dimension]
                    )
                    emit(
                        f"BOUND {fpga_index[fpga.id]} {dimension_index} "
                        f"{balance_bound:.17g} {hard_capacity}"
                    )
            owners = assignment["cluster_assignment"]
            if compact_clusters is not None:
                field_index = {
                    field: index
                    for index, field in enumerate(RESOURCE_FIELDS)
                }
                resource_columns = tuple(
                    compact_clusters._resource_columns[field_index[dimension]]
                    for dimension in dimensions
                    if dimension != "cells"
                )
                for index in range(len(compact_clusters)):
                    part = (
                        owners.part_for_index(index)
                        if isinstance(owners, _PackedClusterAssignment)
                        else owners[f"c{index:06d}"]
                    )
                    fixed_fpga = compact_clusters._fixed_by_cluster.get(index)
                    weights = (
                        compact_clusters.cluster_member_count(index),
                        *(column[index] for column in resource_columns),
                    )
                    emit(
                        "CLUSTER "
                        + " ".join(
                            str(value)
                            for value in (
                                index,
                                fpga_index[part],
                                -1
                                if fixed_fpga is None
                                else fpga_index[fixed_fpga],
                                *weights,
                            )
                        )
                    )
            else:
                for index, cluster in enumerate(clusters):
                    current = fpga_index[owners[cluster["id"]]]
                    fixed = (
                        -1
                        if cluster["fixed_fpga"] is None
                        else fpga_index[cluster["fixed_fpga"]]
                    )
                    weights = [
                        len(cluster["instances"]),
                        *[
                            cluster["resources"].get(dimension, 0)
                            for dimension in dimensions
                            if dimension != "cells"
                        ],
                    ]
                    emit(
                        "CLUSTER "
                        + " ".join(
                            str(value)
                            for value in (index, current, fixed, *weights)
                        )
                    )
            if isinstance(nets, _PackedHopNetRecords):
                net_rows: Iterable[Tuple[float, int, Sequence[int]]] = (
                    nets.iter_columns()
                )
            else:
                cluster_index = {
                    cluster["id"]: index
                    for index, cluster in enumerate(clusters)
                }
                net_rows = (
                    (
                        float(net["weight"]),
                        cluster_index[net["source"]],
                        [cluster_index[item] for item in net["sinks"]],
                    )
                    for net in nets
                )
            for index, (weight, source, sink_indices) in enumerate(net_rows):
                emit(
                    "NET "
                    + " ".join(
                        str(value)
                        for value in (
                            index,
                            f"{weight:.17g}",
                            source,
                            len(sink_indices),
                            *sink_indices,
                        )
                    )
                )
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    if isinstance(nets, _PackedHopNetRecords):
        phase3_runtime_observe(
            hop_refiner_compact_nets=len(nets),
            hop_refiner_compact_net_payload_bytes=nets.payload_bytes,
        )
    return {
        "schema": HOP_REFINER_INPUT_SCHEMA,
        "algorithm": HOP_REFINER_PROVIDER,
        "fpga_order": fpga_ids,
        "cluster_order": cluster_order,
        "dimensions": dimensions,
        "nets": len(nets),
        "max_route_hops": hop_limit,
        "balance": balance,
        "sha256": digest.hexdigest(),
    }


def _parse_native_output(
    path: Path,
    native_input: Mapping[str, Any],
    *,
    packed_assignment: bool = False,
    retain_diagnostics: bool = False,
) -> Tuple[
    str,
    Mapping[str, str],
    List[Dict[str, Any]],
    Dict[str, float],
]:
    if not path.is_file():
        raise EmuFlowError("hop partition refiner produced no output")
    status = None
    assignments: Dict[int, int] = {}
    packed_parts = array("I")
    moves = []
    metrics: Dict[str, float] = {}
    fpga_order = native_input["fpga_order"]
    with path.open("r", encoding="utf-8") as stream:
        if stream.readline().rstrip("\r\n") != (
            "EMUFLOW_HOP_PARTITION_REFINER_OUTPUT_V1"
        ):
            raise ValidationError("invalid hop partition refiner output header")
        for line in stream:
            fields = line.split()
            if not fields:
                continue
            if fields[0] == "STATUS" and len(fields) == 2:
                if status is not None:
                    raise ValidationError("duplicate hop refiner status")
                status = fields[1]
            elif fields[0] == "ASSIGN" and len(fields) == 3:
                cluster = int(fields[1])
                part = int(fields[2])
                if not 0 <= cluster < len(native_input["cluster_order"]):
                    raise ValidationError("invalid hop refiner cluster identity")
                if packed_assignment:
                    if cluster != len(packed_parts):
                        raise ValidationError(
                            "hop partition refiner assignment order mismatch"
                        )
                    if part < 0 or part >= len(fpga_order):
                        raise ValidationError(
                            "hop partition refiner emitted invalid part"
                        )
                    packed_parts.append(part)
                else:
                    if cluster in assignments:
                        raise ValidationError("duplicate hop refiner assignment")
                    assignments[cluster] = part
            elif fields[0] == "MOVE" and len(fields) == 4:
                if not retain_diagnostics:
                    raise ValidationError(
                        "unexpected hop refiner trace in summary output"
                    )
                cluster, source, target = map(int, fields[1:])
                if (
                    not 0 <= cluster < len(native_input["cluster_order"])
                    or not 0 <= source < len(fpga_order)
                    or not 0 <= target < len(fpga_order)
                ):
                    raise ValidationError("invalid hop refiner move identity")
                moves.append(
                    {
                        "cluster": native_input["cluster_order"][cluster],
                        "source": native_input["fpga_order"][source],
                        "target": native_input["fpga_order"][target],
                    }
                )
            elif fields[0] == "METRIC" and len(fields) == 3:
                value = float(fields[2])
                if fields[1] in metrics or not math.isfinite(value):
                    raise ValidationError("duplicate or non-finite hop refiner metric")
                metrics[fields[1]] = value
            else:
                raise ValidationError(
                    f"invalid hop partition refiner output record {line!r}"
                )
    if status not in {"PASS", "STUCK"}:
        raise ValidationError("hop partition refiner output has no valid status")
    committed = metrics.get("committed_moves", -1)
    if (
        committed < 0
        or not committed.is_integer()
        or (retain_diagnostics and committed != len(moves))
    ):
        raise ValidationError("hop refiner committed move count disagrees")
    cluster_count = len(native_input["cluster_order"])
    if packed_assignment:
        if len(packed_parts) != cluster_count:
            raise ValidationError(
                "hop partition refiner assignment coverage mismatch"
            )
        if packed_parts and max(packed_parts) >= len(fpga_order):
            raise ValidationError("hop partition refiner emitted invalid part")
        assignment: Mapping[str, str] = _PackedClusterAssignment(
            fpga_order, packed_parts
        )
        return status, assignment, moves, metrics
    if set(assignments) != set(range(cluster_count)):
        raise ValidationError("hop partition refiner assignment coverage mismatch")
    materialized_assignment: Dict[str, str] = {}
    for cluster, part in sorted(assignments.items()):
        if part < 0 or part >= len(fpga_order):
            raise ValidationError("hop partition refiner emitted invalid part")
        materialized_assignment[
            native_input["cluster_order"][cluster]
        ] = fpga_order[part]
    return status, materialized_assignment, moves, metrics


def refine_partition_hops(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    partition_constraints: Mapping[str, Any],
    assignment: Mapping[str, Any],
    output_dir: Path,
    *,
    route_constraints_path: Optional[Path] = None,
    net_weights_path: Optional[Path] = None,
    executable: Optional[str] = None,
    defer_semantic_contract: bool = False,
    retain_diagnostics: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    route_constraints = load_route_constraints(
        route_constraints_path, platform
    )
    hop_limit = route_constraints["max_route_hops"]
    if hop_limit is None:
        return dict(assignment), {
            "schema": HOP_REFINER_REPORT_SCHEMA,
            "status": "not-requested",
            "enabled": False,
            "reason": "route constraints do not define max_route_hops",
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    distances = _shortest_hop_distances(platform, route_constraints)
    before = audit_assignment_hops(assignment, distances, hop_limit)
    net_weights = load_partition_net_weights(net_weights_path)
    native_input_path = output_dir / "hop_refiner.in"
    native_output_path = output_dir / "hop_refiner.out"
    native_input = _write_native_input(
        native_input_path,
        ir,
        platform,
        clusters_artifact,
        partition_constraints,
        assignment,
        route_constraints,
        distances,
        net_weights,
    )
    command = resolve_native_stage_command("phase3", "refine-hops", executable)
    unified = is_unified_native_executable(command[0])
    operation_path = output_dir / "hop_refiner.operation.json"
    if unified:
        command[1:1] = ["--report", str(operation_path.resolve())]
        if retain_diagnostics:
            command.append("--trace-output")
    command.extend(
        [str(native_input_path.resolve()), str(native_output_path.resolve())]
    )
    runner = native_tool_runner(command[0])
    if runner is None:
        raise EmuFlowError("hop refiner requires the shared native tool supervisor")
    with NativeToolProcess(
        runner,
        command,
        cwd=output_dir,
        environment=dict(os.environ),
        timeout_ms=21600000,
        scratch_parent=output_dir,
    ) as process:
        completed = process.finish()
    (output_dir / "hop_refiner.log").write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        raise EmuFlowError(
            "hop partition refiner failed with exit code "
            f"{completed.returncode}: {completed.stdout[-2000:]}"
        )
    if unified:
        if not operation_path.is_file() or operation_path.stat().st_size > 262144:
            raise ValidationError(
                "hop refiner operation report is missing or exceeds bound"
            )
        operation = json.loads(operation_path.read_text(encoding="utf-8"))
        if (
            not isinstance(operation, dict)
            or operation.get("schema") != "emuflow.native-stage-result/v1"
            or operation.get("stage") != "phase3.refine-hops"
            or operation.get("status") != "pass"
            or not isinstance(operation.get("metrics"), dict)
        ):
            raise ValidationError("hop refiner operation report disagrees")
    status, cluster_assignment, moves, native_metrics = _parse_native_output(
        native_output_path,
        native_input,
        retain_diagnostics=retain_diagnostics,
        packed_assignment=(
            defer_semantic_contract
            and isinstance(native_input["cluster_order"], _CompactClusterOrder)
        ),
    )

    metadata = dict(assignment.get("provider_metadata", {}))
    refinement_summary = {
        "algorithm": HOP_REFINER_PROVIDER,
        "research_basis": [
            "TopoPart (ICCAD 2021)",
            "cut-violation-correction partitioner (DATE 2024)",
            "HoPart direction (DATE 2026 programme abstract)",
        ],
        "claim_scope": (
            "topology-constrained FM post-refinement; not an exact HoPart "
            "reproduction"
        ),
        "native_status": status,
        "move_count": int(native_metrics["committed_moves"]),
        "native_metrics": native_metrics,
    }
    metadata["hop_feasibility"] = refinement_summary
    if cluster_assignment == assignment.get("cluster_assignment"):
        # The native hop checker commonly proves the input already feasible.
        # Preserve its canonical assignment/Static Exact contract instead of
        # rebuilding the complete instance/net representation solely to add a
        # compact proof summary.
        primary = dict(assignment)
        primary["provider_metadata"] = metadata
    else:
        primary = build_partition_assignment(
            ir,
            platform,
            clusters_artifact,
            partition_constraints,
            cluster_assignment,
            provider=assignment["provider"],
            seed=assignment["seed"],
            provider_metadata=metadata,
            _include_semantic_contract=not defer_semantic_contract,
            _stream_semantic_records=defer_semantic_contract,
        )
    replicas = _replica_targets(assignment)
    if replicas:
        replicas = {
            cluster: [
                target
                for target in targets
                if target != cluster_assignment[cluster]
            ]
            for cluster, targets in replicas.items()
        }
        replicas = {
            cluster: targets
            for cluster, targets in replicas.items()
            if targets
        }
        primary = apply_replication(
            ir, platform, clusters_artifact, primary, replicas
        )
    after = audit_assignment_hops(primary, distances, hop_limit)
    report = {
        "schema": HOP_REFINER_REPORT_SCHEMA,
        "status": "pass" if after["status"] == "pass" else "failed",
        "enabled": True,
        "algorithm": HOP_REFINER_PROVIDER,
        "before": before,
        "after": after,
        "moves": moves,
        "move_count": int(native_metrics["committed_moves"]),
        "native_metrics": native_metrics,
        "timing_weighted_nets": len(net_weights),
        "artifacts": {
            "input": native_input_path.name,
            "output": native_output_path.name,
            "log": "hop_refiner.log",
        },
        "input": native_input,
    }
    if status != "PASS" or after["status"] != "pass":
        raise ValidationError(
            "hop-feasible partition refinement could not satisfy "
            f"max_route_hops={hop_limit}; remaining violations="
            f"{after['violating_net_sink_pairs']}"
        )
    return primary, report
