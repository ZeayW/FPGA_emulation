"""Partition-sized reductions over shared native primary ownership.

This consumer independently sums primary resources under the final assignment;
it never accepts an optimizer's cached loads or expands cell/member records.
"""
from array import array
from dataclasses import dataclass
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


@dataclass(frozen=True)
class NativeAssignmentOwnership:
    sources: tuple
    parts: tuple
    column: object


@dataclass(frozen=True)
class NativeAssignmentCutSource:
    paths: tuple
    generations: tuple
    endpoint_counts: object
    auxiliary_sources: tuple = ()
    ownership: object = None


def native_assignment_cut_source(assignment):
    """Borrow final native nodes and the already paired compact count column.

    Reference/nonsemantic inputs still use their current explicit-record request.
    Once native nodes exist, missing or changed ownership is an error, never a
    reason to rebuild the records through the host compatibility protocol.
    """
    from .partition import _StaticExactCutNetRecords

    cuts = assignment.get("cut_nets")
    contract = assignment.get("semantic_contract")
    nodes = (
        cuts._nodes
        if isinstance(cuts, _StaticExactCutNetRecords)
        else contract.get("cut_nodes") if isinstance(contract, dict) else None
    )
    path = getattr(nodes, "native_semantic_artifact_path", None)
    if path is None:
        if getattr(nodes, "native_semantic_version", None) is not None:
            raise ValidationError("native assignment semantic artifact is missing")
        return None
    source = getattr(nodes, "direct_binary_source", None)
    if (
        not isinstance(contract, dict)
        or contract.get("cut_nodes") is not nodes
        or source is None
        or getattr(nodes, "native_semantic_version", None) != 2
    ):
        raise ValidationError("native assignment semantic owner is missing or mixed")
    paths = source.native_record_paths()
    if isinstance(cuts, _StaticExactCutNetRecords):
        counts = cuts._cuts._sink_endpoints
    elif type(cuts) is list:
        # A noncanonical source-order reference can already own plain records.
        # Carry only its endpoint column, never normalize/re-emit names or edges.
        counts = array("Q")
        for cut in cuts:
            value = cut.get("sink_endpoints") if isinstance(cut, dict) else None
            if type(value) is not int or not 0 < value < 2**64:
                raise ValidationError("native assignment endpoint count is invalid")
            counts.append(value)
    else:
        raise ValidationError("native assignment cut owner is unavailable")
    generations = (
        getattr(nodes, "native_semantic_generation", None),
        getattr(source.captures, "native_capture_generation", None),
    )
    if (
        paths is None
        or Path(path).resolve() != paths[0]
        or any(generation is None for generation in generations)
        or len(counts) != len(cuts)
        or len(cuts) != len(nodes)
        or counts.itemsize != 8
        or sys.byteorder != "little"
    ):
        raise ValidationError("native assignment semantic source dimensions disagree")
    if tuple(native_file_generation(p) for p in paths) != generations:
        raise ValidationError("native assignment semantic generation changed")
    auxiliary_sources = (counts.native_source,) if getattr(counts, 'native_source', None) is not None else ()
    if auxiliary_sources:
        from .store_publication import require_store_sources_unchanged
        require_store_sources_unchanged(auxiliary_sources)
    ownership = getattr(nodes, 'native_assignment_ownership', None)
    if ownership is not None:
        from .store_publication import require_store_sources_unchanged
        selected = assignment.get('cluster_assignment')
        instance = assignment.get('instance_assignment')
        if (getattr(selected, 'part_indices', None) is not ownership.column
                or getattr(selected, 'part_ids', None) != ownership.parts
                or ownership.sources[0].path != Path(instance.path).resolve()
                or ownership.sources[0].generation != instance._native_source_generation
                or ownership.sources[1].path != Path(instance._native_primary_artifact).resolve()
                or ownership.sources[1].generation != instance._native_primary_generation
                or not auxiliary_sources):
            raise ValidationError('native assignment selected ownership is missing or mixed')
        require_store_sources_unchanged(ownership.sources)
        auxiliary_sources += ownership.sources[2:]
    return NativeAssignmentCutSource(paths, generations, counts, auxiliary_sources, ownership)


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
    cut_source = native_assignment_cut_source(assignment)
    borrowed = cut_source is not None and cut_source.ownership is not None
    cut_version = 3 if borrowed else 2 if cut_source is not None else 1
    with tempfile.TemporaryDirectory(prefix='emuflow-assignment-publish-', dir=path.parent) as temporary:
        request = Path(temporary) / 'request.bin'
        writer = CheckedRequestWriter(request)
        try:
            writer.put("QI", 0x31514241554D5545, cut_version)
            for generation in generations:
                writer.put('3Q4q', *generation)
            if cut_source is not None:
                writer.put("3Q4q", *cut_source.generations[0])
            if borrowed:
                for item in cut_source.auxiliary_sources:
                    writer.put('3Q4q', *item.generation)
            writer.catalog(part_ids)
            writer.catalog(cut_source.ownership.parts if borrowed else instance_assignment.part_ids)
            writer.put('QQ', len(instance_assignment._cluster_parts), len(instance_assignment))
            if not borrowed:
                writer.bytes(instance_assignment._cluster_parts)
            writer.put('I', len(metadata))
            for key, value in sorted(metadata.items()):
                writer.text(key)
                writer.text(_json(value))
            writer.put('Q', len(raw_cuts))
            if borrowed:
                pass
            elif cut_source is not None:
                writer.bytes(cut_source.endpoint_counts)
            else:
                for raw in raw_cuts:
                    cut = _normalized_cut_record(raw, part_index)
                    writer.text(cut["net"])
                    writer.text(cut["cut_class"])
                    writer.put(
                        "II", part_index[cut["source_fpgas"][0]], len(cut["sink_fpgas"])
                    )
                    for sink in cut["sink_fpgas"]:
                        writer.put("I", part_index[sink])
                    writer.put(
                        "Qqqq",
                        cut["sink_endpoints"],
                        cut.get("transport_round", -1),
                        cut.get("dependency_level", -1),
                        cut.get("combinational_dependency_depth", -1),
                    )
                    predecessors = cut.get("predecessor_cut_nets", ())
                    writer.put("I", len(predecessors))
                    for predecessor in predecessors:
                        writer.text(predecessor)
            writer.finish()
        finally:
            writer.close()
        executable = resolve_native_executable('emuflow-native')
        command = [
            executable,
            "phase3",
            "publish-assignment",
            str(source),
            str(primary),
            str(request),
        ]
        if cut_source is not None:
            command.append(str(cut_source.paths[0]))
        if borrowed:
            command.extend(str(item.path) for item in cut_source.auxiliary_sources)
        command.append(str(path))
        with NativeToolProcess(executable, command, environment=dict(os.environ),
                               timeout_ms=21600000, scratch_parent=Path(temporary)) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError('native assignment publication failed: ' + completed.stdout)
        if tuple(native_file_generation(p) for p in (source, primary)) != generations:
            raise ValidationError('native assignment publication source generation changed')
        if (
            cut_source is not None
            and tuple(native_file_generation(p) for p in cut_source.paths)
            != cut_source.generations
        ):
            raise ValidationError("native assignment semantic generation changed")
        if cut_source is not None and cut_source.auxiliary_sources:
            from .store_publication import require_store_sources_unchanged
            require_store_sources_unchanged(cut_source.auxiliary_sources)
        return dict(
            completed.native_metrics,
            native_cut_records=len(raw_cuts) if cut_source is not None else 0,
            host_cut_records=0 if cut_source is not None else len(raw_cuts),
            host_ownership_records=0 if borrowed else len(instance_assignment._cluster_parts),
            cut_request_bytes=request.stat().st_size,
        )


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


def publish_native_assignment_descriptor(path, ir, assignment, clusters, index_summary):
    """Write the production selector from its final typed owner, never host rows."""
    from .native_protocol import CheckedRequestWriter
    from .phase3_assignment_index import _json
    from .store_publication import (StoreSource, bind_store_source,
        validate_store_destination, require_store_sources_unchanged)

    instance = assignment['instance_assignment']
    sources = (
        StoreSource(Path(instance.path).resolve(), 'emuir-compact-v2', instance._native_source_generation),
        StoreSource(Path(instance._native_primary_artifact).resolve(), 'partition-cluster-columns-v2', instance._native_primary_generation),
        bind_store_source(Path(path).with_name('assignment.sqlite'), 'assignment-index-v4'),
    )
    sources = validate_store_destination(Path(path), sources)
    if Path(ir.path).resolve() != sources[0].path:
        raise ValidationError('native assignment descriptor EmuIR disagrees')
    require_store_sources_unchanged(sources)
    contract = assignment.get('semantic_contract')
    if (not isinstance(contract, dict) or contract.get('schema') != 'emuflow.static-exact-combinational-cut/v3'
            or len(clusters['clusters']) != len(assignment['cluster_assignment'])):
        raise ValidationError('native assignment descriptor owner disagrees')
    header = {
        'document': {key: value for key, value in assignment.items()
            if key not in {'cluster_assignment', 'instance_assignment', 'partitions', 'semantic_contract', 'cut_nets'}},
        'partitions': [{key: value for key, value in part.items() if key != 'clusters'}
            for part in assignment['partitions']],
        'semantic_contract_index': {'artifact': 'assignment.sqlite', 'schema': contract['schema'],
            'sha256': assignment['semantic_contract_sha256']},
        'cut_nets_index': {'artifact': 'assignment.sqlite', 'schema': 'emuflow.phase3-cut-records/v1',
            'records': index_summary['cut_records'], 'sha256': index_summary['cut_records_sha256']},
    }
    document = _json(header)
    if len(document.encode('utf-8')) > 8*1024*1024-1024:
        raise ValidationError('native assignment descriptor header exceeds bound')
    with tempfile.TemporaryDirectory(prefix='.assignment-descriptor-', dir=Path(path).parent) as temporary:
        request = Path(temporary)/'request.bin'
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QI', 0x31514441554D5545, 1)
            for item in sources: writer.put('3Q4q', *item.generation)
            writer.text(document);writer.finish()
        finally:
            writer.close()
        executable = resolve_native_executable('emuflow-native')
        command = [executable, 'phase3', 'publish-assignment-descriptor',
            *(str(item.path) for item in sources), str(request), str(path)]
        with NativeToolProcess(executable, command, environment=dict(os.environ),
                timeout_ms=21600000, scratch_parent=Path(temporary)) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError('native assignment descriptor publication failed: '+completed.stdout)
        require_store_sources_unchanged(sources)
        return dict(completed.native_metrics, request_bytes=request.stat().st_size)
