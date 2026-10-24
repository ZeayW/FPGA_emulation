"""Partition-sized reductions over shared native primary ownership.

This consumer independently sums primary resources under the final assignment;
it never accepts an optimizer's cached loads or expands cell/member records.
"""
from array import array
import json
import os
from pathlib import Path
import sys
import tempfile

from .errors import ValidationError
from .native_tools import native_file_generation, resolve_native_executable
from .phase3_pressure_native import _Writer
from .resources import RESOURCE_FIELDS
from .tool_runtime import NativeToolProcess


def reduce_native_partition_loads(clusters, assignment, fpga_ids, *, metrics=None):
    from .partition import _PackedClusterAssignment, _validate_cluster_assignment_coverage
    _validate_cluster_assignment_coverage(clusters, assignment)
    if sys.byteorder != 'little':
        raise ValidationError('native partition loads require little-endian storage')
    source = getattr(clusters, 'native_source_path', None)
    primary = getattr(clusters, 'native_artifact', None)
    if source is None or primary is None:
        raise ValidationError('native primary artifact reference is unavailable')
    source, primary = Path(source), Path(primary)
    generations = (clusters.native_source_generation, clusters.native_generation)
    if tuple(native_file_generation(path) for path in (source, primary)) != generations:
        raise ValidationError('native partition load source generation changed')
    if isinstance(assignment, _PackedClusterAssignment):
        parts, indices = assignment.part_ids, assignment.part_indices
    else:
        parts = tuple(fpga_ids)
        lookup = {part: index for index, part in enumerate(parts)}
        try:
            indices = array('I', (lookup[assignment[f'c{i:06d}']] for i in range(len(clusters))))
        except KeyError as error:
            raise ValidationError('balance assignment references unknown FPGA') from error
    if not parts or len(parts) > 65536 or indices.itemsize != 4:
        raise ValidationError('native partition load ownership dimensions disagree')
    with tempfile.TemporaryDirectory(prefix='emuflow-partition-loads-') as temporary:
        root = Path(temporary)
        request, output = root/'request.bin', root/'loads.json'
        writer = _Writer(request)
        try:
            writer.put('QI', 0x3151454C504D5545, 1)
            for generation in generations:
                writer.put('3Q4q', *generation)
            writer.put('QI', len(clusters), len(parts))
            for part in parts:
                writer.text(part)
            writer.bytes(indices)
            writer.finish()
        finally:
            writer.close()
        executable = resolve_native_executable('emuflow-native')
        command = [executable, 'phase3', 'reduce-loads', str(source), str(primary), str(request), str(output)]
        with NativeToolProcess(executable, command, environment=dict(os.environ),
                               timeout_ms=21600000, scratch_parent=root) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError('native partition load reduction failed: '+completed.stdout)
        if tuple(native_file_generation(path) for path in (source, primary)) != generations:
            raise ValidationError('native partition load source generation changed')
        if output.stat().st_size > 4096+len(parts)*512:
            raise ValidationError('native partition load summary exceeds bound')
        summary = json.loads(output.read_text())
        if not isinstance(summary, dict):
            raise ValidationError('native partition load summary disagrees')
        if metrics is not None:
            metrics.update(completed.native_metrics)
    def integers(values, count):
        return (isinstance(values, list) and len(values) == count and
                all(type(value) is int and value >= 0 for value in values))
    loads, counts, totals = (summary.get(key) for key in ('loads', 'cluster_counts', 'totals'))
    if (summary.get('schema') != 'emuflow.partition-load-summary/v1' or
            summary.get('clusters') != len(clusters) or
            summary.get('instances') != len(clusters.source_cluster_indices) or
            not integers(totals, 13) or not integers(counts, len(parts)) or
            not isinstance(loads, list) or len(loads) != len(parts) or
            not all(integers(row, 13) for row in loads) or
            sum(counts) != len(clusters) or totals[0] != summary['instances'] or
            any(sum(row[d] for row in loads) != totals[d] for d in range(13))):
        raise ValidationError('native partition load summary disagrees')
    allowed = set(fpga_ids)
    if any(count for part, count in zip(parts, counts) if part not in allowed):
        raise ValidationError('balance assignment references unknown FPGA')
    dimensions = ('cells', *RESOURCE_FIELDS)
    loads_by_part = {part: dict.fromkeys(dimensions, 0) for part in fpga_ids}
    counts_by_part = dict.fromkeys(fpga_ids, 0)
    for part, row, count in zip(parts, loads, counts):
        if part in allowed:
            loads_by_part[part] = dict(zip(dimensions, row))
            counts_by_part[part] = count
    return dict(zip(dimensions, totals)), loads_by_part, counts_by_part


def publish_native_assignment_index(path, ir, assignment, part_ids, metadata):
    """Publish current typed ownership without iterating source instances in Python."""
    from .native_protocol import CheckedRequestWriter
    from .phase3_assignment_index import _json, _normalized_cut_record
    instance_assignment = assignment['instance_assignment']
    source = Path(instance_assignment.path)
    if instance_assignment._native_primary_artifact is None:
        raise ValidationError("native assignment primary ownership is missing")
    primary = Path(instance_assignment._native_primary_artifact)
    if source != Path(ir.path) or sys.byteorder != 'little':
        raise ValidationError('native assignment publication source disagrees')
    generations = (instance_assignment._native_source_generation,
                   instance_assignment._native_primary_generation)
    if tuple(native_file_generation(p) for p in (source, primary)) != generations:
        raise ValidationError('native assignment publication source generation changed')
    replication = assignment.get('replication')
    if replication and replication.get('replicas'):
        raise ValidationError('native assignment publication does not support retired replication')
    raw_cuts = assignment.get('cut_nets')
    if not isinstance(raw_cuts, list):
        raise ValidationError('cut-net assignment is invalid')
    part_index = {part: index for index, part in enumerate(part_ids)}
    with tempfile.TemporaryDirectory(prefix='emuflow-assignment-publish-', dir=path.parent) as temporary:
        request = Path(temporary) / 'request.bin'
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QI', 0x31514241554D5545, 1)
            for generation in generations:
                writer.put('3Q4q', *generation)
            writer.catalog(part_ids)
            writer.catalog(instance_assignment.part_ids)
            writer.put('QQ', len(instance_assignment._cluster_parts), len(instance_assignment))
            writer.bytes(instance_assignment._cluster_parts)
            writer.put('I', len(metadata))
            for key, value in sorted(metadata.items()):
                writer.text(key)
                writer.text(_json(value))
            writer.put('Q', len(raw_cuts))
            for raw in raw_cuts:
                cut = _normalized_cut_record(raw, part_index)
                writer.text(cut['net'])
                writer.text(cut['cut_class'])
                writer.put('II', part_index[cut['source_fpgas'][0]], len(cut['sink_fpgas']))
                for sink in cut['sink_fpgas']:
                    writer.put('I', part_index[sink])
                writer.put('Qqqq', cut['sink_endpoints'], cut.get('transport_round', -1),
                           cut.get('dependency_level', -1), cut.get('combinational_dependency_depth', -1))
                predecessors = cut.get('predecessor_cut_nets', ())
                writer.put('I', len(predecessors))
                for predecessor in predecessors:
                    writer.text(predecessor)
            writer.finish()
        finally:
            writer.close()
        executable = resolve_native_executable('emuflow-native')
        command = [executable, 'phase3', 'publish-assignment', str(source), str(primary), str(request), str(path)]
        with NativeToolProcess(executable, command, environment=dict(os.environ),
                               timeout_ms=21600000, scratch_parent=Path(temporary)) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError('native assignment publication failed: ' + completed.stdout)
        if tuple(native_file_generation(p) for p in (source, primary)) != generations:
            raise ValidationError('native assignment publication source generation changed')
        return completed.native_metrics
