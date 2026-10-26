"""Original partition owners to native hard-hop generation and source audit."""
import json
import math
import mmap
import os
from pathlib import Path
import struct
import zlib

from .errors import ValidationError
from .native_columns import _ClusterScratch, _ColumnOwner, _MappedColumn
from .native_protocol import CheckedRequestWriter
from .native_tools import require_native_schema, resolve_native_executable
from .store_publication import StoreSource, bind_store_source, require_store_sources_unchanged
from .tool_runtime import NativeToolProcess


def run(ir, platform, clusters, constraints, assignment, routes, *, weights_path=None,
        executable=None, audit_only=False):
    from .phase3_semantics_native import _ownership_sources
    from .partition import active_partition_fpgas, transported_cut_classes_for_clusters, validate_cluster_assignment_balance, _PackedClusterAssignment
    from .partition_hops import _shortest_hop_distances
    from .partition_limits import planning_resource_capacities
    from .sta import partition_net_weights_payload_path
    from .sta_weights_native import _selector

    records = clusters['clusters']
    selected = _ownership_sources(ir, records, assignment['cluster_assignment'])
    if selected is None:
        return None  # Only explicit reference owners use the numeric protocol.
    if assignment.get('replication') is not None:
        raise ValidationError('native primary hop refinement does not accept replicated ownership')
    kind, source, primary, owners = selected
    require_native_schema('partition-hop-request-v1')
    sources = [source, primary, *owners]
    if weights_path is not None:
        weights_path = Path(weights_path).resolve()
        selector, selector_source = _selector(weights_path, 'net-weight-selector-v2')
        payload = partition_net_weights_payload_path(weights_path, selector)
        if payload is None or selector.get('schema') != 'emuflow.partition-net-weights/v2':
            raise ValidationError('native hop refinement requires current typed weights')
        sources.extend((selector_source, bind_store_source(payload, 'net-weight-store-v1')))
    fpgas = active_partition_fpgas(platform, constraints)
    parts = tuple(fpga.id for fpga in fpgas)
    balance = validate_cluster_assignment_balance(platform, records,
        assignment['cluster_assignment'], constraints['balance_tolerance'],
        constraints.get('balance_tolerance_by_dimension', {}), constraints.get('active_fpgas'),
        constraints.get('balance_dimensions', ()), constraints.get('resource_reservations_by_fpga', {}))
    planning = planning_resource_capacities(fpgas, constraints.get('resource_reservations_by_fpga', {}))
    dimensions = balance['balance_dimensions']
    distances = _shortest_hop_distances(platform, routes)
    initial_parts = assignment['cluster_assignment'].part_ids
    scratch = _ClusterScratch(prefix='phase3-native-hops-')
    root = Path(scratch.name)
    try:
        request, output, operation = root/'request.bin', root/'selected', root/'operation.json'
        require_store_sources_unchanged(sources)
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QII', 0x31514850464D5545, 1, kind | (4 if weights_path is not None else 0) | (8 if audit_only else 0))
            for item in sources:
                writer.put('3Q4q', *item.generation)
            writer.catalog(parts)
            writer.catalog(initial_parts)
            writer.catalog(sorted(transported_cut_classes_for_clusters(clusters)))
            writer.catalog(dimensions)
            writer.put('QQ', routes['max_route_hops'], constraints['min_used_fpgas'])
            for a in parts:
                for b in parts:
                    writer.put('i', -1 if distances[a][b] is None else distances[a][b])
            for fpga in fpgas:
                for dimension in dimensions:
                    allowed = balance['balance_allowed_loads'][fpga.id][dimension]
                    capacity = math.ceil(allowed) if dimension == 'cells' else planning[fpga.id][dimension]
                    writer.put('dQ', allowed, capacity)
            fixed = records._fixed_by_cluster
            writer.put('I', len(fixed))
            for vertex, fpga in sorted(fixed.items()):
                writer.put('II', vertex, parts.index(fpga))
            writer.finish()
        finally:
            writer.close()
        tool = resolve_native_executable('emuflow-native', executable)
        stage = 'audit-hops-from-primary' if audit_only else 'refine-hops-from-primary'
        arguments = [tool, '--report', str(operation), 'phase3', stage,
                     str(source.path), str(primary.path), str(request), str(output),
                     *(str(item.path) for item in sources[2:])]
        with NativeToolProcess(tool, arguments, environment=dict(os.environ),
                               scratch_parent=root, timeout_ms=21600000) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError('native primary hop operation failed: ' + completed.stdout)
        require_store_sources_unchanged(sources)
        if operation.stat().st_size > 256*1024:
            raise ValidationError('native hop result exceeds bound')
        proof = json.loads(operation.read_text())
        expected = [('summary.json', 'partition-hop-summary-v1')]
        if not audit_only:
            expected.insert(0, ('ownership.bin', 'patron-initial-columns-v1'))
        outputs = proof.get('outputs')
        if (proof.get('stage') != 'phase3.'+stage or proof.get('status') != 'pass'
                or not isinstance(outputs, list) or len(outputs) != len(expected)):
            raise ValidationError('native hop original output coverage disagrees')
        fields = ('device', 'inode', 'bytes', 'mtime_seconds', 'mtime_nanoseconds', 'ctime_seconds', 'ctime_nanoseconds')
        originals = []
        for record, (name, schema) in zip(outputs, expected):
            path = (output/name).resolve()
            generation = record.get('generation', {})
            if (record.get('path') != str(path) or record.get('schema') != schema
                    or any(type(generation.get(key)) is not int for key in fields)):
                raise ValidationError('native hop original generation disagrees')
            originals.append(StoreSource(path, schema, tuple(generation[key] for key in fields)))
        require_store_sources_unchanged((*sources, *originals))
        if originals[-1].path.stat().st_size > 4*1024*1024:
            raise ValidationError('native hop summary exceeds bound')
        summary = json.loads(originals[-1].path.read_text())
        if (summary.get('schema') != 'emuflow.partition-hops/v1' or summary.get('status') != 'pass'
                or summary.get('implementation_id') != 'native-primary-hop-refinement-v1'
                or summary.get('parts') != list(parts) or summary.get('clusters') != len(records)
                or summary.get('instances') != len(records.source_cluster_indices)
                or summary.get('after', {}).get('status') != 'pass'
                or type(summary.get('move_count')) is not int or summary['move_count'] < 0):
            raise ValidationError('native hop summary disagrees')
        summary['native_operation_metrics'] = proof['metrics']
        if audit_only:
            scratch.cleanup()
            return None, summary
        with originals[0].path.open('rb') as stream:
            data = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
        mapping = _ColumnOwner(data, scratch)
        try:
            count = len(records)
            if (len(data) != 40 + 4*count + 8*len(fixed)
                    or struct.unpack_from('<QIQQ', data) != (0x315450494E415045, 1, count, len(records.source_cluster_indices))):
                raise ValidationError('native hop ownership envelope disagrees')
            crc = 0
            for offset in range(0, len(data)-4, 1024*1024):
                crc = zlib.crc32(data[offset:min(offset+1024*1024, len(data)-4)], crc)
            if crc != struct.unpack_from('<I', data, len(data)-4)[0]:
                raise ValidationError('native hop ownership checksum disagrees')
            current = object.__new__(_PackedClusterAssignment)
            current.part_ids = parts
            current.part_indices = _MappedColumn(mapping, 28, count, 'I')
            current.native_initial_source = originals[0]
            current.native_initial_column = current.part_indices
            current.native_initial_parts = parts
            current.native_primary_source = primary
            current.native_emuir_source = source
            current.native_initial_fixed = dict(fixed)
            current.native_initial_fixed_parts = tuple(sorted(parts))
            require_store_sources_unchanged((*sources, *originals))
            return current, summary
        except BaseException:
            mapping.close()
            raise
    except BaseException:
        scratch.cleanup()
        raise
