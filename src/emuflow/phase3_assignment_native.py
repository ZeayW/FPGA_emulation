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
from .native_protocol import CheckedRequestWriter as _Writer
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
    from .phase3_decision_native import ownership_sources
    owned = ownership_sources(clusters, assignment)
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
            if owned is not None:
                writer.put('QI', 0x3251454C504D5545, 2)
                for item in (owned[1], owned[2], *owned[3]):
                    writer.put('3Q4q', *item.generation)
                writer.put('I', len(parts))
                for part in parts:
                    writer.text(part)
            else:
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
        owner_sources = () if owned is None else tuple(owned[3])
        command = [executable, 'phase3', 'reduce-loads', str(source), str(primary), str(request),
                   *(str(item.path) for item in owner_sources), str(output)]
        with NativeToolProcess(executable, command, environment=dict(os.environ),
                               timeout_ms=21600000, scratch_parent=root) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError('native partition load reduction failed: '+completed.stdout)
        if tuple(native_file_generation(path) for path in (source, primary)) != generations:
            raise ValidationError('native partition load source generation changed')
        from .store_publication import require_store_sources_unchanged
        require_store_sources_unchanged(owner_sources)
        if output.stat().st_size > 4096+len(parts)*512:
            raise ValidationError('native partition load summary exceeds bound')
        summary = json.loads(output.read_text())
        if not isinstance(summary, dict):
            raise ValidationError('native partition load summary disagrees')
        if metrics is not None:
            metrics.update(completed.native_metrics)
    if owned is not None and summary.get('parts') != list(parts):
        raise ValidationError('native partition load part catalog disagrees')
    return decode_partition_load_summary(summary, clusters, parts, fpga_ids)


def decode_partition_load_summary(summary, clusters, parts, fpga_ids):
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


def validate_native_assignment_cuts(path):
    """Validate published cut rows independently through the shared view."""
    path = Path(path).resolve()
    generation = native_file_generation(path)
    executable = resolve_native_executable("emuflow-native")
    with tempfile.TemporaryDirectory(prefix="emuflow-assignment-cuts-") as temporary:
        root = Path(temporary)
        output = root / "summary.json"
        command = [executable, "phase3", "validate-cuts", str(path), str(output)]
        with NativeToolProcess(
            executable,
            command,
            environment=dict(os.environ),
            timeout_ms=21600000,
            scratch_parent=root,
        ) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError(
                "native assignment cut validation failed: " + completed.stdout
            )
        if native_file_generation(path) != generation:
            raise ValidationError("assignment cut validation source generation changed")
        try:
            if output.stat().st_size > 1024:
                raise ValidationError("assignment cut validation summary exceeds bound")
            summary = json.loads(output.read_text())
        except (OSError, ValueError) as error:
            raise ValidationError(
                "assignment cut validation summary is invalid"
            ) from error
    if (
        not isinstance(summary, dict)
        or set(summary) != {"schema", "status", "records", "sinks", "sha256"}
        or summary["schema"] != "emuflow.assignment-cut-validation/v1"
        or summary["status"] != "pass"
        or any(
            type(summary[key]) is not int or summary[key] < 0
            for key in ("records", "sinks")
        )
        or summary["sinks"] < summary["records"]
        or not isinstance(summary["sha256"], str)
        or len(summary["sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in summary["sha256"])
    ):
        raise ValidationError("assignment cut validation summary disagrees")
    return summary
