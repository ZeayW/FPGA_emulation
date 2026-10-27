"""Bounded source locators for native Vivado timing-query owners."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.storage.store_publication import StoreSource, bind_store_source, require_store_sources_unchanged
from emuflow.common.tool_runtime import NativeToolProcess

_FIELDS = ('device', 'inode', 'bytes', 'mtime_seconds', 'mtime_nanoseconds',
           'ctime_seconds', 'ctime_nanoseconds')


def _small(path, maximum=262144):
    path = Path(path)
    if path.stat().st_size > maximum:
        raise ValidationError('Vivado query control document exceeds bound')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError) as error:
        raise ValidationError('Vivado query control document is invalid') from error
    if not isinstance(value, dict):
        raise ValidationError('Vivado query control document is invalid')
    return value


def _execute(sources, output, *, boundary, resolve):
    requested = Path(output).absolute()
    if requested.is_symlink():
        raise ValidationError('Vivado query destination is a symbolic link')
    requested = requested.parent.resolve()/requested.name
    for source in sources:
        if requested.resolve() == source.path or (requested.exists() and os.path.samefile(requested, source.path)):
            raise ValidationError('Vivado query destination aliases an original source')
    requested.parent.mkdir(parents=True, exist_ok=True)
    operation = 'resolve-vivado-timing-query' if resolve else 'query-vivado-boundary'
    request_schema = 'vivado-resolve-query-request-v1' if resolve else 'vivado-boundary-query-request-v1'
    report_schema = 'vivado-resolve-query-report-v1' if resolve else 'vivado-boundary-query-report-v1'
    for schema in (request_schema, report_schema, 'vivado-timing-query-tsv-v1'):
        require_native_schema(schema)
    require_store_sources_unchanged(sources)
    with tempfile.TemporaryDirectory(prefix='.vivado-query-host-', dir=requested.parent) as temporary:
        scratch = Path(temporary)
        request, proof = scratch/'request.bin', scratch/'operation.json'
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QI', 0x31525156464D5545 if resolve else 0x31425156464D5545, 1)
            for source in sources:
                writer.put('3Q4q', *source.generation)
            if resolve:
                writer.put('B', int(boundary))
            writer.finish()
        finally:
            writer.close()
        generation = Path(tempfile.mkdtemp(prefix=requested.name+'.generation.', dir=requested.parent))
        generation.rmdir()
        try:
            executable = resolve_native_executable('emuflow-native')
            command = [executable, '--report', str(proof), 'physical', operation,
                       *(str(source.path) for source in sources), str(request), str(generation)]
            with NativeToolProcess(executable, command, environment=dict(os.environ), scratch_parent=scratch) as process:
                completed = process.finish()
            if completed.returncode:
                raise ValidationError('native Vivado timing query failed: '+completed.stdout)
            result = _small(proof)
            expected = (('summary.json', report_schema), ('query.tsv', 'vivado-timing-query-tsv-v1'))
            if (result.get('stage') != 'physical.'+operation or result.get('status') != 'pass' or
                    not isinstance(result.get('outputs'), list) or len(result['outputs']) != 2):
                raise ValidationError('native Vivado timing query proof disagrees')
            outputs = []
            for row, (name, schema) in zip(result['outputs'], expected):
                path = generation/name
                stamped = tuple(row.get('generation', {}).get(field) for field in _FIELDS)
                if (path.is_symlink() or row.get('path') != str(path.resolve()) or row.get('schema') != schema or
                        any(type(n) is not int for n in stamped) or stamped != native_file_generation(path)):
                    raise ValidationError('native Vivado timing query output generation changed')
                outputs.append(StoreSource(path.resolve(), schema, stamped))
            report = _small(generation/'summary.json')
            implementation = 'native-vivado-resolve-query-v1' if resolve else 'native-vivado-boundary-query-v1'
            if (report.get('schema') != ('emuflow.vivado-resolve-query-report/v1' if resolve else 'emuflow.vivado-boundary-query-report/v1') or
                    report.get('status') != 'pass' or report.get('implementation_id') != implementation or
                    report.get('output') != str((generation/'query.tsv').resolve())):
                raise ValidationError('native Vivado timing query summary disagrees')
            require_store_sources_unchanged((*sources, *outputs))
            return report
        except BaseException:
            shutil.rmtree(generation, ignore_errors=True)
            raise


def write_vivado_boundary_query(ir_path, identity_path, output):
    selector = bind_store_source(Path(identity_path).resolve(), 'boundary-identity-selector-v1')
    raw = _small(selector.path, 8*1024*1024)
    artifact = raw.get('artifact')
    if (raw.get('schema') != 'emuflow.boundary-identity-storage/v1' or not isinstance(artifact, str) or
            not artifact or Path(artifact).name != artifact or artifact in {'.', '..'}):
        raise ValidationError('Vivado query requires a current typed boundary identity selector')
    payload = selector.path.parent/artifact
    if payload.is_symlink():
        raise ValidationError('Vivado boundary payload is a symbolic link')
    sources = (bind_store_source(Path(ir_path).resolve(), 'emuir-compact-v2'),
               bind_store_source(payload, 'boundary-identity-store-v1'), selector)
    return _execute(sources, output, boundary=True, resolve=False)


def resolve_vivado_query(mapped, functions, lineage, source, output, *, boundary=False):
    if type(boundary) is not bool:
        raise ValidationError('Vivado timing query boundary flag must be boolean')
    sources = tuple(bind_store_source(Path(path).resolve(), schema) for path, schema in (
        (mapped, 'xilinx-mapped-index-v2'), (functions, 'vivado-source-functions-v1'),
        (lineage, 'vivado-lineage-store-v2'), (source, 'vivado-timing-query-tsv-v1')))
    return _execute(sources, output, boundary=boundary, resolve=True)
