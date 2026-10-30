"""Bounded host for native structural cut and original-path qualification."""
from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile
from emuflow.common.errors import ValidationError
from emuflow.common.paths import DATA_ROOT
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.storage.store_publication import bind_store_source, require_store_sources_unchanged
from emuflow.timing.engine_runtime import DEFAULT_TIMING_MODEL

CUT_SEGMENT_INDEXED_QUALIFICATION_SCHEMA = 'emuflow.cut-segment-timing-qualification/v2'
CUT_SEGMENT_QUALIFICATION_PROVIDER = 'emuir-structural-cut-segment-qualification-v1'


def _small(source):
    if source.generation[2] > 8*1024*1024:
        raise ValidationError('cut qualification control document exceeds bound')
    value = json.loads(source.path.read_text())
    require_store_sources_unchanged([source])
    return value


def prepare_request(path, ir_path, assignment_path, path_database_path, *,
        timing_model_path=DEFAULT_TIMING_MODEL, architecture_timing_db_path=None,
        input_already_validated=False):
    """Bind original stores and finite timing policy; never read graph records."""
    require_native_schema('cut-qualification-request-v1')
    assignment = Path(assignment_path).resolve()
    selectors = []
    if assignment.suffix != '.sqlite':
        selector = bind_store_source(assignment, 'assignment-selector-v4')
        value = _small(selector)
        if value.get('schema') != 'emuflow.phase3-assignment-storage/v4':
            raise ValidationError('cut qualification requires current assignment storage')
        member = value.get('cut_nets_index', {}).get('artifact')
        if type(member) is not str or Path(member).name != member or member in {'', '.', '..'}:
            raise ValidationError('cut qualification assignment must be an indexed sibling')
        assignment = selector.path.parent/member
        selectors.append(selector)
    selector = bind_store_source(Path(path_database_path).resolve(), 'sta-path-selector-v1')
    database = _small(selector)
    if database.get('schema') != 'emuflow.sta-path-database/v3':
        raise ValidationError('cut qualification requires current indexed TimingPathDB')
    member = database.get('payloads', {}).get('paths', {}).get('path')
    if type(member) is not str or Path(member).name != member or member in {'', '.', '..'}:
        raise ValidationError('cut qualification paths must be an indexed sibling')
    sources = [('emuir', bind_store_source(Path(ir_path).resolve(), 'emuir-compact-v2')),
        ('assignment', bind_store_source(assignment, 'assignment-index-v4')),
        ('selector', selector), ('paths', bind_store_source(selector.path.parent/member, 'timing-path-store-v1')),
        ('timing-model', bind_store_source(DEFAULT_TIMING_MODEL if architecture_timing_db_path is not None else Path(timing_model_path).resolve(), 'phase2-source-v1'))]
    if architecture_timing_db_path is not None:
        sources.extend([('architecture-timing-db', bind_store_source(Path(architecture_timing_db_path).resolve(), 'phase2-source-v1')),
            ('architecture-contract', bind_store_source(DATA_ROOT/'rapidwright/xcvu19p-timing-contract.json', 'phase2-source-v1'))])
    value = {'schema': 'emuflow.cut-qualification-request/v1', 'verify_payload': not input_already_validated,
        'sources': [{'role': role, 'path': str(ref.path), 'schema': ref.schema, 'generation': list(ref.generation)} for role, ref in sources]}
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n'
    if len(encoded.encode()) > 8*1024*1024:
        raise ValidationError('cut qualification request exceeds bound')
    require_store_sources_unchanged([ref for role, ref in sources]+selectors)
    path = Path(path);path.write_text(encoded)
    require_store_sources_unchanged([ref for role, ref in sources]+selectors)
    return path, tuple(ref for role, ref in sources)+tuple(selectors)


def build_cut_segment_qualification(ir_path, assignment_path, path_database_path, *,
        timing_model_path=DEFAULT_TIMING_MODEL, architecture_timing_db_path=None,
        input_already_validated=False):
    with tempfile.TemporaryDirectory(prefix='emuflow-cut-qualification-') as temporary:
        root = Path(temporary).resolve()
        request, sources = prepare_request(root/'request.json', ir_path, assignment_path, path_database_path,
            timing_model_path=timing_model_path, architecture_timing_db_path=architecture_timing_db_path,
            input_already_validated=input_already_validated)
        tool = resolve_native_executable('emuflow-native')
        output, operation = root/'qualification.json', root/'operation.json'
        with NativeToolProcess(tool, [tool, '--report', str(operation), 'timing', 'qualify-cuts', str(request), str(output)],
                environment=dict(os.environ), scratch_parent=root) as process:
            result = process.finish()
        if result.returncode:
            raise ValidationError('native cut qualification failed: '+result.stdout)
        proof = _small(bind_store_source(operation, 'native-stage-result-v1'))
        records = proof.get('outputs', [])
        if proof.get('schema') != 'emuflow.native-stage-result/v1' or proof.get('stage') != 'timing.qualify-cuts' or proof.get('status') != 'pass' or len(records) != 1:
            raise ValidationError('native cut qualification producer identity disagrees')
        fields = ('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
        record = records[0]
        if record.get('path') != str(output) or record.get('schema') != 'cut-qualification-report-v2' or tuple(record.get('generation', {}).get(k) for k in fields) != native_file_generation(output):
            raise ValidationError('native cut qualification output generation changed')
        report = _small(bind_store_source(output, 'cut-qualification-report-v2'))
        require_store_sources_unchanged(sources)
        if report.get('schema') != CUT_SEGMENT_INDEXED_QUALIFICATION_SCHEMA or report.get('status') != 'pass':
            raise ValidationError('native cut qualification report identity disagrees')
        return report


def validate_cut_segment_qualification(artifact, ir_path, assignment_path, path_database_path, *,
        timing_model_path=DEFAULT_TIMING_MODEL, architecture_timing_db_path=None):
    expected = build_cut_segment_qualification(ir_path, assignment_path, path_database_path,
        timing_model_path=timing_model_path, architecture_timing_db_path=architecture_timing_db_path)
    if dict(artifact) != expected:
        raise ValidationError('cut-segment qualification is not independently reconstructed')
    return {'status': 'pass', **expected['summary']}
