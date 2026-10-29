"""Bounded source references for native original-path partition projection."""
import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.storage.store_publication import bind_store_source, require_store_sources_unchanged, validate_store_destination


def _small(source):
    if source.generation[2] > 512 * 1024:
        raise ValidationError('STA projection control document exceeds bound')
    value = json.loads(source.path.read_text(encoding='utf-8'))
    require_store_sources_unchanged((source,))
    return value


def project_sta_path_database(database_path, assignment_path, output_path, *, input_already_validated=False):
    """Project and compress typed original paths without host path/cut objects."""
    require_native_schema('sta-projection-request-v1')
    selector = bind_store_source(Path(database_path), 'sta-path-selector-v1')
    document = _small(selector)
    if document.get('schema') != 'emuflow.sta-path-database/v3':
        raise ValidationError('STA projection requires current indexed TimingPathDB')
    member = document.get('payloads', {}).get('paths', {}).get('path')
    if not isinstance(member, str) or member in {'', '.', '..'} or Path(member).name != member:
        raise ValidationError('STA projection payload must be a sibling of its selector')
    paths = bind_store_source(selector.path.parent / member, 'timing-path-store-v1')
    assignment_path = Path(assignment_path)
    if assignment_path.suffix == '.sqlite':
        assignment = bind_store_source(assignment_path, 'assignment-index-v4')
        assignment_selector = assignment
    else:
        assignment_selector = bind_store_source(assignment_path, 'assignment-selector-v4')
        value = _small(assignment_selector)
        if value.get('schema') != 'emuflow.phase3-assignment-storage/v4':
            raise ValidationError('STA projection requires current assignment selector')
        member = value.get('cut_nets_index', {}).get('artifact')
        if not isinstance(member, str) or member in {'', '.', '..'} or Path(member).name != member:
            raise ValidationError('STA projection assignment owner is invalid')
        assignment = bind_store_source(assignment_selector.path.parent / member, 'assignment-index-v4')
    sources = (selector, paths, assignment, assignment_selector)
    display = str(output_path)
    output_path = Path(output_path).absolute()
    validate_store_destination(output_path, sources)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.sta-projection-host-', dir=output_path.parent) as temporary:
        root = Path(temporary)
        request, summary, operation = root / 'request.bin', root / 'summary.json', root / 'operation.json'
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QI', 0x31525053464D5545, 1)
            for source in sources:
                writer.put('3Q4q', *source.generation)
            writer.text(json.dumps({'verify_payload': not input_already_validated, 'output': display}, separators=(',', ':'), allow_nan=False))
            writer.finish()
        finally:
            writer.close()
        require_store_sources_unchanged(sources)
        native = resolve_native_executable('emuflow-native')
        command = [native, '--report', str(operation), 'timing', 'project-paths',
                   *(str(source.path) for source in sources), str(request), str(output_path), str(summary)]
        with NativeToolProcess(native, command, environment=dict(os.environ), scratch_parent=root) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError('native STA projection failed: ' + completed.stdout)
        require_store_sources_unchanged(sources)
        proof = _small(bind_store_source(operation, 'native-stage-result-v1'))
        if (proof.get('stage') != 'timing.project-paths' or proof.get('status') != 'pass'
                or len(proof.get('outputs', [])) != 2):
            raise ValidationError('native STA projection producer identity disagrees')
        names = ('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
        for row, path, schema in zip(proof['outputs'], (output_path, summary), ('system-routing-paths-v1','sta-projection-summary-v1')):
            if (row.get('schema') != schema or row.get('path') != str(path.resolve())
                    or tuple(row.get('generation', {}).get(k) for k in names) != native_file_generation(path)):
                raise ValidationError('native STA projection output generation changed')
        report = _small(bind_store_source(summary, 'sta-projection-summary-v1'))
        if (report.get('schema') != 'emuflow.sta-projection-report/v1'
                or report.get('status') != 'pass' or report.get('implementation_id') != 'native-sta-path-projection-v1'):
            raise ValidationError('native STA projection summary identity disagrees')
        return report['summary']
