"""Bounded configuration and report bridge for the complete native timing stage."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.timing.engine_runtime import DEFAULT_TIMING_MODEL, OPENSTA_EXPORT_SCRIPT, OPENSTA_RUNTIME_CHECK_SCRIPT, _runtime_data_path
from emuflow.common.paths import DATA_ROOT
from emuflow.storage.store_publication import StoreSource, bind_store_source, require_store_sources_unchanged
from emuflow.common.tool_runtime import NativeToolProcess


_POLICY_LIMIT = 8 * 1024 * 1024
_GENERATION_FIELDS = ('device', 'inode', 'bytes', 'mtime_seconds', 'mtime_nanoseconds',
                      'ctime_seconds', 'ctime_nanoseconds')


def _encode_policy(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True,
                         separators=(',', ':'), allow_nan=False) + '\n'
    if len(encoded.encode('utf-8')) > _POLICY_LIMIT:
        raise ValidationError('Phase 2 configuration exceeds bound')
    return encoded


def _configuration_value(ir_path, *, clocks=None, timing_io=None,
                   timing_model_path=DEFAULT_TIMING_MODEL,
                   architecture_timing_db_path=None, executable=None,
                   max_paths=200000, optimization_enabled=True,
                   criticality_scale=9.0, criticality_exponent=2.0):
    """Pass bounded options and source references to the native policy owner."""
    require_native_schema('phase2-configuration-request-v1')
    options = {'clocks': clocks, 'timing_io': timing_io, 'max_paths': max_paths,
        'optimization_enabled': optimization_enabled, 'criticality_scale': criticality_scale,
        'criticality_exponent': criticality_exponent}
    _encode_policy(options)
    engine = resolve_native_executable('sta', executable)
    roles = [('timing-model', DEFAULT_TIMING_MODEL if architecture_timing_db_path is not None else Path(timing_model_path), 'phase2-source-v1'),
        ('opensta-executable', Path(engine), 'external-tool-executable-v1'),
        ('export-script', OPENSTA_EXPORT_SCRIPT, 'phase2-source-v1'),
        ('runtime-check-script', OPENSTA_RUNTIME_CHECK_SCRIPT, 'phase2-source-v1'),
        ('runtime-launcher', _runtime_data_path(Path('scripts/opensta/container_runtime.sh')), 'phase2-source-v1')]
    if ir_path is not None:
        roles.insert(0, ('emuir', Path(ir_path), 'emuir-compact-v2'))
    if architecture_timing_db_path is not None:
        roles.extend([('architecture-timing-db', Path(architecture_timing_db_path), 'phase2-source-v1'),
            ('architecture-contract', DATA_ROOT / 'rapidwright/xcvu19p-timing-contract.json', 'phase2-source-v1')])
    if os.environ.get('EMUFLOW_OPENSTA_CONTAINER_BINARY'):
        roles.append(('container-binary', Path(os.environ['EMUFLOW_OPENSTA_CONTAINER_BINARY']).expanduser(), 'external-tool-executable-v1'))
    sources = [(role, bind_store_source(path, schema)) for role, path, schema in roles]
    request_value = {'schema': 'emuflow.phase2-configuration-request/v1', 'options': options,
        'sources': [{'role': role, 'path': str(source.path), 'schema': source.schema,
            'generation': list(source.generation)} for role, source in sources]}
    encoded = _encode_policy(request_value)
    require_store_sources_unchanged(source for role, source in sources)
    return encoded, sources


def prepare_configuration_request(directory, ir_path=None, **options):
    """Bind finite settings before synthesis; compile binds its published IR."""
    encoded, sources = _configuration_value(ir_path, **options)
    directory = Path(directory).absolute()
    directory.mkdir(parents=True, exist_ok=False)
    try:
        request = directory / 'request.json'
        request.write_text(encoded, encoding='utf-8')
        require_store_sources_unchanged(source for role, source in sources)
        return request
    except BaseException:
        shutil.rmtree(directory)
        raise


def prepare_policy(directory, ir_path, **options):
    """Submit the complete native policy owner from the optional Python API."""
    encoded, sources = _configuration_value(ir_path, **options)
    directory = Path(directory).absolute()
    directory.mkdir(parents=True, exist_ok=False)
    try:
        with tempfile.TemporaryDirectory(prefix='.phase2-policy-host-', dir=directory) as temporary:
            root = Path(temporary)
            request, operation = root / 'request.json', root / 'operation.json'
            request.write_text(encoded, encoding='utf-8')
            output = directory / 'phase2-policy.json'
            native = resolve_native_executable('emuflow-native')
            command = [native, '--report', str(operation), 'phase2', 'prepare-policy', str(request), str(output)]
            with NativeToolProcess(native, command, environment=dict(os.environ), scratch_parent=root) as process:
                result = process.finish()
            if result.returncode:
                raise ValidationError('native Phase 2 configuration failed: ' + result.stdout)
            if operation.stat().st_size > 262144:
                raise ValidationError('native Phase 2 policy operation exceeds bound')
            proof = json.loads(operation.read_text())
            outputs = proof.get('outputs', [])
            if (proof.get('schema') != 'emuflow.native-stage-result/v1' or proof.get('stage') != 'phase2.prepare-policy'
                    or proof.get('status') != 'pass' or len(outputs) != 1):
                raise ValidationError('native Phase 2 policy producer identity disagrees')
            row = outputs[0]
            generation = tuple(row.get('generation', {}).get(key) for key in _GENERATION_FIELDS)
            if row.get('schema') != 'phase2-policy-v1' or row.get('path') != str(output.resolve()) or generation != native_file_generation(output):
                raise ValidationError('native Phase 2 policy generation changed')
            require_store_sources_unchanged(source for role, source in sources)
            return output
    except BaseException:
        shutil.rmtree(directory)
        raise


def run_native_phase2(ir_path, output, *, clocks=None, timing_io=None,
                      timing_model_path=DEFAULT_TIMING_MODEL,
                      architecture_timing_db_path=None, executable=None,
                      max_paths=200000, optimization_enabled=True,
                      criticality_scale=9.0, criticality_exponent=2.0,
                      native_executable=None):
    """Execute the whole native owner and consume its bounded sealed report."""
    require_native_schema('phase2-report-v1')
    output = Path(output).absolute()
    if output.exists():
        raise ValidationError('native Phase 2 requires a fresh stage directory')
    output.parent.mkdir(parents=True, exist_ok=True)
    tool = resolve_native_executable('emuflow-native', native_executable)
    if Path(tool).name != 'emuflow-native':
        raise ValidationError('Phase 2 requires the unified native entry')
    with tempfile.TemporaryDirectory(prefix='.native-phase2-', dir=output.parent) as temporary:
        root = Path(temporary)
        policy = prepare_policy(root / 'policy', ir_path, clocks=clocks, timing_io=timing_io,
            timing_model_path=timing_model_path, architecture_timing_db_path=architecture_timing_db_path,
            executable=executable, max_paths=max_paths, optimization_enabled=optimization_enabled,
            criticality_scale=criticality_scale, criticality_exponent=criticality_exponent)
        bound = json.loads(policy.read_text())
        sources = tuple(StoreSource(Path(row['path']), row['schema'], tuple(row['generation']))
                        for row in bound['sources'])
        operation = root / 'operation.json'
        command = [tool, '--report', str(operation), 'phase2', 'run',
                   next(row['path'] for row in bound['sources'] if row['role'] == 'emuir'), str(policy), str(output)]
        with NativeToolProcess(tool, command, environment=dict(os.environ), scratch_parent=root) as process:
            result = process.finish()
        if result.returncode:
            raise ValidationError('native Phase 2 failed: ' + result.stdout)
        require_store_sources_unchanged(sources)
        if operation.stat().st_size > 262144:
            raise ValidationError('native Phase 2 operation exceeds bound')
        proof = json.loads(operation.read_text())
        if (proof.get('schema') != 'emuflow.native-stage-result/v1'
                or proof.get('stage') != 'phase2.run' or proof.get('status') != 'pass'):
            raise ValidationError('native Phase 2 operation identity disagrees')
        reports = [row for row in proof.get('outputs', [])
                   if row.get('schema') == 'phase2-report-v1']
        report_path = output / 'phase2_report.json'
        if len(reports) != 1 or report_path.is_symlink():
            raise ValidationError('native Phase 2 report coverage disagrees')
        record = reports[0]
        generation = tuple(record.get('generation', {}).get(key) for key in _GENERATION_FIELDS)
        if record.get('path') != str(report_path.resolve()) or generation != native_file_generation(report_path):
            raise ValidationError('native Phase 2 report generation changed')
        report_source = StoreSource(report_path.resolve(), 'phase2-report-v1', generation)
        if report_path.stat().st_size > 262144:
            raise ValidationError('native Phase 2 report exceeds bound')
        report = json.loads(report_path.read_text())
        if (report.get('schema') != 'emuflow.phase2-report/v1' or report.get('status') != 'pass'
                or report.get('stage_implementation_id') != 'native-phase2-v1'
                or report.get('sta', {}).get('design') != bound['configuration']['design']):
            raise ValidationError('native Phase 2 report identity disagrees')
        require_store_sources_unchanged((*sources, report_source))
        return report
