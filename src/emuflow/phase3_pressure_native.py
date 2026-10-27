"""Native pressure modeling from source-bound, shared primary ownership.

Production references the native primary store and sends packed initial/fixed
ownership plus bounded board configuration. Missing primary ownership is an
error; no complete membership/resource snapshot is constructed.
"""
from __future__ import annotations

from array import array
from collections.abc import Sequence
import json
import mmap
from pathlib import Path
import struct
import sys
import zlib

from .errors import ValidationError
# Existing protocol fixtures import this private spelling.
from .native_protocol import CheckedRequestWriter as _Writer
from .native_tools import native_file_generation, resolve_native_executable
from .tool_runtime import NativeToolProcess
from .resources import RESOURCE_FIELDS


def supports_native_pressure_model(ir, clusters, timing):
    from .partition_ir import CompactPartitionIR
    from .partition import _CompactClusterRecords, CUT_MODE_STATIC_EXACT
    from .sta import STA_PATH_DATABASE_INDEX_SCHEMA, STA_PATH_DATABASE_SQLITE_FORMAT
    payloads = timing.get("payloads")
    descriptor = payloads.get("paths") if isinstance(payloads, dict) else None
    return (isinstance(ir, CompactPartitionIR)
            and isinstance(clusters.get("clusters"), _CompactClusterRecords)
            and clusters.get("policy", {}).get("cut_mode") == CUT_MODE_STATIC_EXACT
            and timing.get("schema") == STA_PATH_DATABASE_INDEX_SCHEMA
            and isinstance(descriptor, dict) and descriptor.get("format") == STA_PATH_DATABASE_SQLITE_FORMAT)


class _NativeClusterOrder(Sequence):
    def __init__(self, path, count):
        self.stream = Path(path).open("rb")
        self.data = None
        try:
            self.data = mmap.mmap(self.stream.fileno(), 0, access=mmap.ACCESS_READ)
            if len(self.data) != 24 + 4 * count:
                raise ValidationError("native PATRON cluster order size disagrees")
            if struct.unpack_from("<QIQ", self.data) != (0x3152444F54415045, 1, count):
                raise ValidationError("native PATRON cluster order header disagrees")
            crc = 0
            for start in range(0, len(self.data) - 4, 1024 * 1024):
                crc = zlib.crc32(self.data[start:min(start + 1024 * 1024, len(self.data) - 4)], crc)
            if crc != struct.unpack_from("<I", self.data, len(self.data) - 4)[0]:
                raise ValidationError("native PATRON cluster order checksum disagrees")
            self.count = count
        except BaseException:
            self.close()
            raise

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if not isinstance(index, int) or not 0 <= index < self.count:
            raise IndexError(index)
        ordinal = struct.unpack_from("<I", self.data, 20 + 4 * index)[0]
        if ordinal >= self.count:
            raise ValidationError("native PATRON cluster identity is invalid")
        return f"c{ordinal:06d}"

    def close(self):
        if self.data is not None:
            self.data.close()
            self.data = None
        self.stream.close()


class NativePressureModel(dict):
    implementation_id = "native-primary-store-model-v2"

    def __init__(self, ir, platform, clusters, constraints, timing, routes, timing_path):
        from . import patron_policy as ref
        from .partition_topology import _shortest_routes, _capacity_key_by_arc
        from .partition import active_partition_fpgas, transported_cut_classes_for_clusters
        from .partition_limits import planning_resource_capacities
        from .routing import build_directed_graph
        from .sta import _validate_database_normalization, sta_path_database_payload_path

        if timing.get("design") != ir.value["design"]["name"]:
            raise ValidationError("partition pressure timing design mismatch")
        normalization = _validate_database_normalization(timing.get("normalization"))
        self.ir = ir
        self.primary_artifact = getattr(clusters["clusters"], "native_artifact", None)
        if self.primary_artifact is None:
            raise ValidationError("native primary artifact reference is unavailable")
        self.primary_generation = clusters["clusters"].native_generation
        if clusters["clusters"].native_source_generation != native_file_generation(ir.path):
            raise ValidationError("native primary cluster source generation changed")
        self.platform = platform
        self.clusters = clusters["clusters"]
        self.constraints = constraints
        self.cluster_artifact = clusters
        self.routes = routes
        self.timing_path = sta_path_database_payload_path(timing_path, timing)
        self.generations = (native_file_generation(ir.path), native_file_generation(self.timing_path))
        self.partition_fpgas = sorted(active_partition_fpgas(platform, constraints), key=lambda fpga: fpga.id)
        self.planning = planning_resource_capacities(self.partition_fpgas, constraints.get("resource_reservations_by_fpga", {}))
        self.transported = sorted(transported_cut_classes_for_clusters(clusters))
        _adjacency, _links, capacities = build_directed_graph(platform, routes, reserve_arrival_and_commit=True)
        self.expected_paths = timing["payloads"]["paths"]["records"]
        configuration = {
            "max_scalable_sweeps": ref.MAX_SCALABLE_SWEEPS,
            "scalable_ejection_critical_limit": ref.SCALABLE_EJECTION_CRITICAL_LIMIT,
            "scalable_ejection_donor_limit": ref.SCALABLE_EJECTION_DONOR_LIMIT,
            "max_scalable_ejections": ref.MAX_SCALABLE_EJECTIONS,
        }
        super().__init__(design=ir.value["design"]["name"], platform=platform.name,
                         configuration=configuration, normalization=normalization,
                         clusters=range(len(self.clusters)), nets=range(0), paths=range(self.expected_paths),
                         capacities=range(len(capacities)))
        links = {link.id: link for link in platform.links}
        self.domains = [dict(value, lanes=links[value["link"]].transport_bits_per_cycle_per_direction)
                        for _key, value in sorted(capacities.items())]
        self.small_route_model = {"capacities": self.domains, "shortest_routes": _shortest_routes(platform, routes)}

    def _write_initial_ownership(self, path, initial):
        from .partition import _PackedClusterAssignment
        if sys.byteorder != "little":
            raise ValidationError("native PATRON cluster columns require little-endian storage")
        current = initial["cluster_assignment"]
        if not isinstance(current, _PackedClusterAssignment):
            parts = [fpga.id for fpga in self.partition_fpgas]
            lookup = {part: index for index, part in enumerate(parts)}
            current = _PackedClusterAssignment(parts, (lookup[current[f"c{i:06d}"]] for i in range(len(self.clusters))))
        if len(current) != len(self.clusters):
            raise ValidationError("native PATRON initial ownership coverage disagrees")
        writer = _Writer(path)
        try:
            writer.put("QIQQ", 0x315450494E415045, 1,
                       len(self.clusters), len(self.clusters.source_cluster_indices))
            if current.part_indices.itemsize != 4:
                raise ValidationError("native PATRON initial column width is invalid")
            writer.bytes(current.part_indices)
            parts = [fpga.id for fpga in self.partition_fpgas]
            writer.put("Q", len(self.clusters._fixed_by_cluster))
            for cluster, fpga in self.clusters._fixed_by_cluster.items():
                writer.put("II", cluster, parts.index(fpga))
            writer.finish()
        finally:
            writer.close()
        return current.part_ids

    def write_requests(self, root, initial, max_moves):
        from . import patron_policy as ref
        from .partition_topology import _shortest_routes, _capacity_key_by_arc
        from .routing import route_link_delay_coefficients_ns, route_link_delay_ns
        if self.primary_artifact is None:
            raise ValidationError("selected direct primary ownership is unavailable")
        if native_file_generation(self.primary_artifact) != self.primary_generation:
            raise ValidationError("native primary column generation changed")
        columns, board = root / "initial.bin", root / "board.bin"
        current = initial["cluster_assignment"]
        native_initial = getattr(current, "native_initial_source", None)
        if native_initial is not None:
            from .store_publication import require_store_sources_unchanged
            require_store_sources_unchanged((native_initial,current.native_primary_source,current.native_emuir_source))
            if (current.part_indices is not current.native_initial_column
                    or current.part_ids != current.native_initial_parts
                    or current.native_primary_source.path != Path(self.primary_artifact).resolve()
                    or current.native_primary_source.generation != self.primary_generation
                    or current.native_emuir_source.path != Path(self.ir.path).resolve()
                    or current.native_emuir_source.generation != self.generations[0]
                    or current.native_initial_fixed != self.clusters._fixed_by_cluster
                    or current.native_initial_fixed_parts != tuple(fpga.id for fpga in self.partition_fpgas)):
                raise ValidationError("native initializer ownership belongs to another pressure model")
            columns, initial_parts = native_initial.path, current.part_ids
        else:
            initial_parts = self._write_initial_ownership(columns, initial)
        parts = [fpga.id for fpga in self.partition_fpgas]
        domain_index = {domain["key"]: i for i, domain in enumerate(self.domains)}
        arc_domain = _capacity_key_by_arc(self.small_route_model)
        links = {link.id: link for link in self.platform.links}
        writer = _Writer(board)
        try:
            writer.put("QI", 0x3151455254415045, 2)
            generations = (*self.generations, self.primary_generation, native_file_generation(columns))
            for generation in generations:
                writer.put("3Q4q", *generation)
            writer.catalog(parts)
            writer.catalog(initial_parts)
            writer.catalog(self.transported)
            writer.put("Q", self.expected_paths)
            cfg = self["configuration"]
            writer.put("10i", -1 if self.routes.get("max_route_hops") is None else self.routes["max_route_hops"],
                       self.routes["tdm_max_ratio"], self.routes["tdm_ratio_quantum"], self.constraints["min_used_fpgas"],
                       max_moves, cfg["max_scalable_sweeps"], cfg["scalable_ejection_critical_limit"],
                       cfg["scalable_ejection_donor_limit"], cfg["max_scalable_ejections"], self.routes.get("tdm_min_ratio", 1))
            norm = self["normalization"]
            writer.put("5d", norm["positive_slack_scale_ns"], norm["negative_slack_scale_ns"], norm["max_clock_period_ns"],
                       ref.BOUNDARY_FANOUT_PENALTY_SCALE_NS, 0.0)
            writer.put("6i", ref.PATRON_FLOW_CORRIDOR_DISTANCE, ref.PATRON_FLOW_PIERCING_STRATEGY,
                       ref.PATRON_FLOW_MAX_LEGAL_CANDIDATES, ref.PATRON_FLOW_MAX_POLISH_MOVES,
                       ref.PATRON_FLOW_MAX_FRONTIER_PATHS, ref.PATRON_FLOW_MAX_TAIL_MOVES)
            dimensions = ("cells", *RESOURCE_FIELDS)
            balanced = self.constraints.get("balance_dimensions", ())
            for field in dimensions:
                writer.put("Bd", int(balanced is None or field in balanced),
                           self.constraints.get("balance_tolerance_by_dimension", {}).get(field, self.constraints["balance_tolerance"]))
            for fpga in self.partition_fpgas:
                for field in dimensions:
                    writer.put("QQ", fpga.effective_capacity.get(field, 0), self.planning[fpga.id].get(field, 0))
            writer.put("I", len(self.domains))
            for domain in self.domains:
                link = links[domain["link"]]
                slope = route_link_delay_coefficients_ns(self.platform, link.id, *link.endpoints, self.routes)[1]
                writer.put("iBdiiiq", domain["lanes"], int(link.id in self.routes["sll_links"]), slope,
                           domain_index.get(domain.get("opposite_capacity_key"), -1), domain.get("bank_count", 0),
                           domain.get("wires_per_bank", 0), int(domain["capacity_bits"]))
            for source in parts:
                for sink in parts:
                    route = self.small_route_model["shortest_routes"][source][sink]
                    writer.put("i", -1 if route is None else len(route))
                    for arc in route or ():
                        writer.put("Id", domain_index[arc_domain[(arc["link"], arc["from"], arc["to"])]],
                                   route_link_delay_ns(self.platform, arc["link"], arc["from"], arc["to"], self.routes))
            writer.finish()
        finally:
            writer.close()
        for path, expected in zip((self.ir.path, self.timing_path), self.generations):
            if native_file_generation(path) != expected:
                raise ValidationError("native PATRON source generation changed")
        return columns, board

    def execute(self, root, output, initial, max_moves, timeout_seconds, environment, executable=None):
        from .store_publication import StoreSource, require_store_sources_unchanged
        columns, board = self.write_requests(root, initial, max_moves)
        executable = resolve_native_executable("emuflow-native", executable)
        if Path(executable).name != 'emuflow-native':
            raise ValidationError('native PATRON requires the unified source-owned entry')
        proof_path = root / 'producer.json'
        command = [executable, "--report", str(proof_path), "phase3", "refine-patron-from-primary",
                   str(self.ir.path), str(self.timing_path), str(self.primary_artifact),
                   str(columns), str(board), str(output)]
        with NativeToolProcess(executable, command, environment=environment, timeout_ms=timeout_seconds * 1000,
                               scratch_parent=root) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError("native PATRON store model failed: " + completed.stdout)
        summary_path = Path(str(output) + ".summary.json")
        if proof_path.stat().st_size > 256*1024:
            raise ValidationError('native PATRON producer result exceeds bound')
        proof = json.loads(proof_path.read_text())
        if (proof.get('stage') != 'phase3.refine-patron-from-primary' or proof.get('status') != 'pass'
                or not isinstance(proof.get('outputs'),list) or len(proof['outputs']) != 3):
            raise ValidationError('native PATRON producer result disagrees')
        originals = []
        fields = ('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
        for record,path,schema in zip(proof['outputs'],(output,Path(str(output)+'.order'),summary_path),
                ('patron-output-v1','patron-cluster-order-v1','patron-model-summary-v1')):
            generation = record.get('generation',{})
            if (record.get('path') != str(path.resolve()) or record.get('schema') != schema
                    or any(type(generation.get(key)) is not int for key in fields)):
                raise ValidationError('native PATRON original output binding disagrees')
            originals.append(StoreSource(path.resolve(),schema,tuple(generation[key] for key in fields)))
        require_store_sources_unchanged(originals)
        if summary_path.stat().st_size > 65536:
            raise ValidationError("native PATRON model summary exceeds bound")
        summary = json.loads(summary_path.read_text())
        if (not isinstance(summary, dict) or summary.get("schema") != "emuflow.patron-native-model/v1" or summary.get("implementation_id") != self.implementation_id
                or summary.get("ownership_order") != 'canonical-cluster-ordinal-v1'
                or summary.get("clusters") != len(self.clusters) or summary.get("paths") != self.expected_paths
                or any(isinstance(summary.get(key), bool) or not isinstance(summary.get(key), int) or summary[key] < 0
                       for key in ("clusters", "nets", "paths", "dimensions", "capacity_domains"))
                or not 1 <= summary["dimensions"] <= len(RESOURCE_FIELDS) + 1
                or not 1 <= summary["nets"] <= len(self.ir.value["nets"])
                or summary["capacity_domains"] != len(self["capacities"])):
            raise ValidationError("native PATRON model summary is invalid")
        self["nets"] = range(summary["nets"])
        self.native_counts = {key: summary[key] for key in ("clusters", "nets", "paths", "capacity_domains")}
        metrics = {}
        for key, stage in (("model_build", "phase3.model-build"), ("search", "phase3.patron-search")):
            operation = summary.get(key)
            if (not isinstance(operation, dict) or operation.get("schema") != "emuflow.native-stage-result/v1"
                    or operation.get("stage") != stage or operation.get("status") != "pass"
                    or not isinstance(operation.get("metrics"), dict)):
                raise ValidationError("native PATRON substage metrics are invalid")
            metrics[key] = operation["metrics"]
        self.native_input = {"transport": self.implementation_id, "bytes": columns.stat().st_size + board.stat().st_size,
                             "records": 2, "model": self.native_counts, "metrics": metrics,
                             "shared_primary_bytes": self.primary_artifact.stat().st_size if self.primary_artifact is not None else 0}
        order = _NativeClusterOrder(str(output) + ".order", len(self.clusters))
        try:
            require_store_sources_unchanged(originals)
        except BaseException:
            order.close()
            raise
        return {"parts": [fpga.id for fpga in self.partition_fpgas], "clusters": order, "input": self.native_input,
                'native_output_source': originals[0], 'native_order_source': originals[1],
                'native_primary_source': StoreSource(Path(self.primary_artifact).resolve(),'partition-cluster-columns-v2',self.primary_generation),
                'native_emuir_source': StoreSource(Path(self.ir.path).resolve(),'emuir-compact-v2',self.generations[0])}

    def refine(self, initial, *, max_moves=None, timeout_seconds=3600, executable=None):
        """Borrow current typed native ownership; no reference model or parser fallback."""
        import os
        from .native_columns import _ClusterScratch
        from .partition import build_partition_assignment
        limit = len(self.clusters) if max_moves is None else max_moves
        if type(limit) is not int or limit < 0:
            raise ValidationError('native PATRON max_moves is invalid')
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            raise ValidationError('native PATRON timeout is invalid')
        workspace = _ClusterScratch(prefix='emuflow-patron-')
        root = Path(workspace.name)
        retained = False
        indexes = None
        try:
            output = root / 'patron.out'
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith('EMUFLOW_PATRON_') or key == 'EMUFLOW_PATRON_CONTAINER_BINARY'}
            indexes = self.execute(root, output, initial, limit, timeout_seconds, environment,
                                   executable=executable)
            owners, initial_metrics, final_metrics, summary = _borrow_result(output, indexes, workspace)
            retained = True
            for name in ('initial.bin', 'board.bin'):
                (root / name).unlink(missing_ok=True)
            final = build_partition_assignment(self.ir, self.platform,
                self.cluster_artifact,
                self.constraints, owners, provider='patron-predictive-timing-flow-native-v14',
                seed=initial.get('seed', 0), _include_semantic_contract=False,
                _stream_semantic_records=True, _defer_derived_records=True)
            trace = {'schema': 'emuflow.partition-pressure-trace/v14',
                     'provider': 'patron-predictive-timing-flow-native-v14',
                     'mode': 'endpoint-exact-critical-flow-v14',
                     'initial_metrics': initial_metrics, 'final_metrics': final_metrics,
                     'move_count': summary['moves'], 'batch_count': summary['batches'],
                     'change_count': summary['changes'], 'trace_storage': 'summary-only',
                     'native_input_transport': self.native_input['transport'],
                     'native_input_bytes': self.native_input['bytes'],
                     'native_input_records': self.native_input['records'],
                     'model_implementation_id': self.implementation_id,
                     'model_counts': self.native_counts,
                     'native_model_metrics': self.native_input['metrics']}
            return final, trace
        finally:
            if indexes is not None:
                indexes['clusters'].close()
            if not retained:
                workspace.cleanup()


def build_native_pressure_model(ir, platform, clusters, constraints, timing, routes, *, timing_database_path):
    """Construct only the current source-owned native model configuration."""
    if getattr(clusters.get('clusters'), 'native_artifact', None) is None:
        raise ValidationError('native primary artifact reference is unavailable')
    if not supports_native_pressure_model(ir, clusters, timing):
        raise ValidationError('native pressure requires current compact source, primary columns and indexed timing')
    return NativePressureModel(ir, platform, clusters, constraints, timing, routes, timing_database_path)


def _borrow_result(path, indexes, workspace):
    """Read a fixed-size summary and lend the source-bound ownership column."""
    import math
    from .native_columns import _ColumnOwner, _MappedColumn
    from .partition import _PackedClusterAssignment
    from .store_publication import require_store_sources_unchanged
    keys = ('native_output_source', 'native_order_source', 'native_primary_source', 'native_emuir_source')
    sources = tuple(indexes[key] for key in keys)
    require_store_sources_unchanged(sources)
    parts = tuple(indexes['parts'])
    if not parts or parts != tuple(sorted(set(parts))) or sources[0].path != path.resolve():
        raise ValidationError('native PATRON mapped ownership binding disagrees')
    try:
        with path.open('rb') as stream:
            data = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
    except (OSError, ValueError) as error:
        raise ValidationError('native PATRON typed result is unreadable') from error
    retained = False
    try:
        offset = 0
        def take(fmt):
            nonlocal offset
            size = struct.calcsize(fmt)
            if offset + size > len(data):
                raise ValidationError('native PATRON typed result is truncated')
            values = struct.unpack_from(fmt, data, offset)
            offset += size
            return values
        magic, version, algorithm, length = take('<QIII')
        if (magic, version, algorithm) != (0x3154554F54415045, 1, 14) or length > 256:
            raise ValidationError('native PATRON typed result header is invalid')
        if data[offset:offset+length] != b'endpoint-exact-critical-flow-v14':
            raise ValidationError('native PATRON typed result mode is invalid')
        offset += length
        before, after = take('<8d'), take('<8d')
        capacity, path_max = take('<4q'), take('<4q')
        moves, batches, changes, detail = take('<3QI')
        count = take('<Q')[0]
        if (detail or count != len(indexes['clusters'])
                or any(value < 0 for value in (*capacity, *path_max))
                or len(data) != offset + 4 * count + 4):
            raise ValidationError('native PATRON typed result summary is invalid')
        checksum = 0
        for begin in range(0, len(data)-4, 1024*1024):
            checksum = zlib.crc32(data[begin:min(begin+1024*1024, len(data)-4)], checksum)
        if checksum != struct.unpack_from('<I', data, len(data)-4)[0]:
            raise ValidationError('native PATRON typed result checksum disagrees')
        def metrics(values, selected):
            if not all(math.isfinite(value) for value in values):
                raise ValidationError('native PATRON objective is invalid')
            result = dict(zip(('negative_paths', 'maximum_predicted_tdm_ratio',
                'maximum_capacity_domain_load', 'total_path_partition_transitions',
                'total_bit_hops', 'cut_bits'), (int(value) for value in values[2:])))
            result.update(worst_normalized_slack=-values[0],
                total_negative_normalized_slack=-values[1], objective_key=list(values),
                transport_capacity_violations=capacity[2*selected],
                transport_capacity_excess_bits=capacity[2*selected+1],
                maximum_path_hops=path_max[2*selected], maximum_path_tdm_ratio_sum=path_max[2*selected+1])
            return result
        initial_metrics, final_metrics = metrics(before, 0), metrics(after, 1)
        require_store_sources_unchanged(sources)
        owner = _ColumnOwner(data, workspace)
        result = object.__new__(_PackedClusterAssignment)
        result.part_ids = parts
        result.part_indices = _MappedColumn(owner, offset, count, 'I')
        result.native_output_column = result.part_indices
        result.native_output_parts = parts
        for key in keys:
            setattr(result, key, indexes[key])
        retained = True
        return result, initial_metrics, final_metrics, {'moves': moves, 'batches': batches, 'changes': changes}
    finally:
        if not retained:
            data.close()
