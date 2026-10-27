"""Bounded source locators for complete native local-path preparation/query."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import shutil
from pathlib import Path
import tempfile
from typing import Any, Mapping

from emuflow.common.errors import ValidationError
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.storage.store_publication import StoreSource, bind_store_source, require_store_sources_unchanged
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.physical.xilinx_primitives import DEFAULT_XILINX_PRIMITIVE_LIBRARY

_FIELDS = ('device', 'inode', 'bytes', 'mtime_seconds', 'mtime_nanoseconds',
           'ctime_seconds', 'ctime_nanoseconds')


def _small(path, maximum=8*1024*1024):
    path = Path(path)
    if path.stat().st_size > maximum:
        raise ValidationError('local query control document exceeds bound')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValidationError('local query control document is invalid')
    return value


def _payload(locator, raw, key):
    value = raw.get(key)
    if not isinstance(value, str) or not value or Path(value).is_absolute() or '..' in Path(value).parts:
        raise ValidationError('local query payload locator is invalid')
    return Path(locator).resolve().parent / value


def _sources(original_ir_path, assignment_path, path_database_path, routes_path):
    source, assignment, paths, routes = (Path(p).resolve() for p in (
        original_ir_path, assignment_path, path_database_path, routes_path))
    path_manifest, route_manifest = _small(paths), _small(routes)
    if (path_manifest.get('schema') != 'emuflow.sta-path-database/v3' or
            route_manifest.get('schema') != 'emuflow.system-route-storage/v1'):
        raise ValidationError('native local queries require current indexed timing and routes')
    descriptor = path_manifest.get('payloads', {}).get('paths', {})
    if descriptor.get('format') != 'sqlite-sta-path/v1':
        raise ValidationError('native local queries require the typed path owner')
    path_store = _payload(paths, descriptor, 'path')
    if assignment.suffix == '.sqlite':
        assignment_store = assignment
    else:
        # The descriptor may retain compressed ownership columns. The host
        # never loads them; the native owner validates the source locator.
        assignment_store = assignment.with_name('assignment.sqlite')
    return tuple(bind_store_source(path, schema) for path, schema in (
        (source, 'emuir-compact-v2'), (assignment_store, 'assignment-index-v4'),
        (path_store, 'timing-path-store-v1'),
        (_payload(routes, route_manifest, 'artifact'), 'system-route-store-v3'),
        (assignment, 'physical-input-document-v1'), (paths, 'sta-path-selector-v1'),
        (routes, 'system-route-selector-v1')))


def _execute(operation, sources, output, scratch, *, backend=None, fpga=None):
    output, scratch = Path(output).absolute(), Path(scratch)
    schema = 'local-path-query-request-v1' if backend else 'local-path-prepare-request-v1'
    require_native_schema(schema)
    request, proof = scratch/'request.bin', scratch/'operation.json'
    writer = CheckedRequestWriter(request)
    try:
        writer.put('QI', 0x31514C51464D5545 if backend else 0x31504C51464D5545, 1)
        for source in sources:
            writer.put('3Q4q', *source.generation)
        if backend:
            writer.text(backend)
            writer.text(fpga)
        writer.finish()
    finally:
        writer.close()
    require_store_sources_unchanged(sources)
    executable = resolve_native_executable('emuflow-native')
    command = [executable, '--report', str(proof), 'physical', operation,
               *(str(source.path) for source in sources), str(request), str(output)]
    with NativeToolProcess(executable, command, environment=dict(os.environ), scratch_parent=scratch) as process:
        result = process.finish()
    if result.returncode:
        raise ValidationError('native local path owner failed: '+result.stdout)
    expected = (('summary.json', 'local-path-query-report-v1'),
                ('query.tsv', 'local-path-query-tsv-v1'),
                ('identity.json', 'local-path-timing-selector-v1')) if backend else (
                ('summary.json', 'local-path-prepare-report-v1'),
                ('ownership.sqlite', 'local-path-partition-store-v1'))
    value = _small(proof, 262144)
    if (value.get('stage') != 'physical.'+operation or value.get('status') != 'pass' or
            not isinstance(value.get('outputs'), list) or len(value['outputs']) != len(expected)):
        raise ValidationError('native local path producer proof disagrees')
    outputs = []
    for row, (name, kind) in zip(value['outputs'], expected):
        path = output/name
        generation = tuple(row.get('generation', {}).get(field) for field in _FIELDS)
        if (path.is_symlink() or row.get('path') != str(path.resolve()) or row.get('schema') != kind or
                any(type(n) is not int for n in generation) or generation != native_file_generation(path)):
            raise ValidationError('native local path output generation changed')
        outputs.append(StoreSource(path.resolve(), kind, generation))
    report = _small(output/'summary.json', 262144)
    expected_id = 'native-local-path-query-v1' if backend else 'native-local-path-partition-v1'
    if report.get('status') != 'pass' or report.get('implementation_id') != expected_id:
        raise ValidationError('native local path summary disagrees')
    require_store_sources_unchanged((*sources, *outputs))
    return report, outputs


@dataclass(frozen=True)
class LocalPathQueryInputs:
    """Leased source references plus a native ownership index; no path records."""
    original_ir_path: Path
    assignment_path: Path
    path_database_path: Path
    routes_path: Path
    sources: tuple[StoreSource, ...]
    partition_source: StoreSource
    source_manifest: Mapping[str, Any]
    design: str
    _lifetime: Any

    @property
    def original_ir(self):
        return None

    def close(self):
        self._lifetime.cleanup()


def prepare_local_path_query_inputs(original_ir_path, assignment_path, path_database_path, routes_path):
    sources = _sources(original_ir_path, assignment_path, path_database_path, routes_path)
    lifetime = tempfile.TemporaryDirectory(prefix='emuflow-native-local-paths-')
    root = Path(lifetime.name)
    try:
        report, outputs = _execute('prepare-local-paths', sources, root/'prepared', root)
        return LocalPathQueryInputs(*(Path(p).resolve() for p in (
            original_ir_path, assignment_path, path_database_path, routes_path)),
            sources, outputs[1], report['source'], report['design'], lifetime)
    except BaseException:
        lifetime.cleanup()
        raise


def write_native_local_path_query(merged_ir_path, fpga, query_path, identity_path, prepared, *, backend):
    destinations = (Path(merged_ir_path).resolve(), Path(query_path).resolve(), Path(identity_path).resolve())
    if (len(set(destinations)) != 3 or any(a.exists() and b.exists() and os.path.samefile(a, b)
            for i, a in enumerate(destinations) for b in destinations[:i])):
        raise ValidationError('local query destinations alias a source/output')
    if not isinstance(prepared, LocalPathQueryInputs):
        raise ValidationError('local query requires the source-owned native preparation')
    if not isinstance(fpga, str) or not fpga or backend not in {'xilinx', 'vivado'}:
        raise ValidationError('local query FPGA/backend is invalid')
    sources = (*prepared.sources, prepared.partition_source,
        bind_store_source(Path(merged_ir_path).resolve(), 'emuir-compact-v2'),
        bind_store_source(DEFAULT_XILINX_PRIMITIVE_LIBRARY, 'xilinx-primitive-library-v1'))
    require_store_sources_unchanged(sources)
    parent = Path(identity_path).resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    for path in (Path(query_path).resolve(), Path(identity_path).resolve()):
        if any(path == source.path or (path.exists() and os.path.samefile(path, source.path)) for source in sources):
            raise ValidationError('local query destination aliases an original source')
    with tempfile.TemporaryDirectory(prefix='.local-query-host-', dir=parent) as temporary:
        root = Path(temporary)
        # The generation remains beneath the physical-run owner. Returning its
        # source references avoids copying TSV/SQLite payloads to legacy names.
        generation = Path(tempfile.mkdtemp(prefix=Path(identity_path).name+'.generation.', dir=parent))
        generation.rmdir()
        try:
            report, _ = _execute('query-local-paths', sources, generation, root, backend=backend, fpga=fpga)
            return report
        except BaseException:
            shutil.rmtree(generation, ignore_errors=True)
            raise


def write_xilinx_local_path_query(original_ir_path, assignment_path, path_database_path,
        routes_path, merged_ir_path, fpga, query_path, identity_path, *, prepared_inputs=None):
    prepared = prepared_inputs or prepare_local_path_query_inputs(
        original_ir_path, assignment_path, path_database_path, routes_path)
    expected = tuple(Path(p).resolve() for p in (original_ir_path, assignment_path, path_database_path, routes_path))
    if expected != (prepared.original_ir_path, prepared.assignment_path, prepared.path_database_path, prepared.routes_path):
        raise ValidationError('local query preparation belongs to another original run')
    return write_native_local_path_query(merged_ir_path, fpga, query_path, identity_path, prepared, backend='xilinx')
