"""Bounded configuration and report bridge for the complete native timing stage."""
from __future__ import annotations

from contextlib import closing
import json
import math
import os
from pathlib import Path
import tempfile

from .timing_io import timing_io_sha256
from .errors import ValidationError
from .indexed_timing import IndexedTimingView
from .io import file_sha256
from .native_tools import native_file_generation, require_native_schema, resolve_native_executable
from .opensta import (
    DEFAULT_TIMING_MODEL, OPENSTA_EXPORT_SCRIPT, OPENSTA_RUNTIME_CHECK_SCRIPT,
    _clock_map, _sealed_string_set_summary,
    build_architecture_opensta_timing_model, load_timing_model,
    render_opensta_liberty, require_opensta_engine, validate_timing_model_coverage,
)
from .store_publication import StoreSource, bind_store_source, require_store_sources_unchanged
from .tool_runtime import NativeToolProcess


_POLICY_LIMIT = 8 * 1024 * 1024
_MODEL_CELL_LIMIT = 4096
_GENERATION_FIELDS = ('device', 'inode', 'bytes', 'mtime_seconds', 'mtime_nanoseconds',
                      'ctime_seconds', 'ctime_nanoseconds')


def _encode_policy(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True,
                         separators=(',', ':'), allow_nan=False) + '\n'
    if len(encoded.encode('utf-8')) > _POLICY_LIMIT:
        raise ValidationError('Phase 2 configuration exceeds bound')
    return encoded


def _timing_io_contract(value, clocks):
    """Validate only explicit bounded configuration; native checks EmuIR ports."""
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {'input_groups', 'output_groups'}:
        raise ValidationError('OpenSTA timing_io contract is invalid')
    for direction in ('input', 'output'):
        groups = value[direction + '_groups']
        if not isinstance(groups, list):
            raise ValidationError('OpenSTA timing_io groups are invalid')
        seen = set()
        for group in groups:
            if not isinstance(group, dict) or set(group) != {'clock', 'delay_ns', 'ports'}:
                raise ValidationError('OpenSTA timing_io group is invalid')
            clock, delay, ports = group['clock'], group['delay_ns'], group['ports']
            if (not isinstance(clock, str) or clock not in clocks
                    or isinstance(delay, bool) or not isinstance(delay, (int, float))
                    or not math.isfinite(float(delay)) or delay < 0
                    or not isinstance(ports, list) or not ports):
                raise ValidationError('OpenSTA timing_io group is invalid')
            for port in ports:
                if not isinstance(port, str) or not port or port in seen or port in clocks:
                    raise ValidationError('OpenSTA timing_io data port is invalid')
                seen.add(port)


def _check_model_bound(model):
    cells = model['cells']
    if len(cells) > _MODEL_CELL_LIMIT:
        raise ValidationError('Phase 2 primitive model exceeds bound')
    arcs = 0
    for name, cell in cells.items():
        names = [name]
        for field in ('inputs', 'outputs', 'controls'):
            names.extend(cell.get(field, []))
        names.extend(cell[field] for field in ('clock', 'data', 'output') if field in cell)
        if any(len(name.encode('utf-8')) > 1024 for name in names):
            raise ValidationError('Phase 2 primitive model identity exceeds bound')
        if cell['kind'] == 'combinational':
            arcs += len(cell['inputs']) * len(cell.get('outputs', [cell.get('output')]))
        else:
            arcs += len(names)
        if arcs > 32768:
            raise ValidationError('Phase 2 primitive model arcs exceed bound')


def prepare_policy(directory, ir_path, *, clocks=None, timing_io=None,
                   timing_model_path=DEFAULT_TIMING_MODEL,
                   architecture_timing_db_path=None, executable=None,
                   max_paths=200000, optimization_enabled=True,
                   criticality_scale=9.0, criticality_exponent=2.0):
    """Prepare a source-bound primitive model; never export a design-sized file."""
    require_native_schema('phase2-policy-v1')
    if type(max_paths) is not int or max_paths <= 0:
        raise ValidationError('OpenSTA max_paths must be positive')
    if type(optimization_enabled) is not bool:
        raise ValidationError('Phase 2 optimization_enabled must be a boolean')
    for name, value in (('criticality_scale', criticality_scale),
                        ('criticality_exponent', criticality_exponent)):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or value <= 0):
            raise ValidationError('Phase 2 ' + name + ' must be positive and finite')
    # Reject oversized explicit configuration before constructing a model.
    _encode_policy({'clock_map': clocks, 'timing_io': timing_io})
    engine_path = resolve_native_executable('sta', executable)
    model_path = DEFAULT_TIMING_MODEL if architecture_timing_db_path is not None else Path(timing_model_path)
    roles = [
        ('emuir', Path(ir_path), 'emuir-compact-v2'),
        ('timing-model', Path(model_path), 'phase2-source-v1'),
        ('opensta-executable', Path(engine_path), 'external-tool-executable-v1'),
        ('export-script', OPENSTA_EXPORT_SCRIPT, 'phase2-source-v1'),
        ('runtime-check-script', OPENSTA_RUNTIME_CHECK_SCRIPT, 'phase2-source-v1'),
    ]
    if architecture_timing_db_path is not None:
        roles.append(('architecture-timing-db', Path(architecture_timing_db_path), 'phase2-source-v1'))
    sources = [(role, bind_store_source(path, schema)) for role, path, schema in roles]
    if any(source.generation[2] > _POLICY_LIMIT for role, source in sources
           if role in {'timing-model', 'architecture-timing-db'}):
        raise ValidationError('Phase 2 primitive model document exceeds bound')
    with closing(IndexedTimingView(sources[0][1].path, include_ports=False)) as view:
        # The only Python instance objects are finite primitive representatives.
        count = view.connection.execute('SELECT count(*) FROM (SELECT type FROM cell_records GROUP BY type LIMIT ?)',
                                        (_MODEL_CELL_LIMIT + 1,)).fetchone()[0]
        if count > _MODEL_CELL_LIMIT:
            raise ValidationError('Phase 2 primitive catalog exceeds bound')
        if architecture_timing_db_path is None:
            model, timing_types, mode = load_timing_model(model_path), None, 0
        else:
            model, timing_types = build_architecture_opensta_timing_model(view, Path(architecture_timing_db_path))
            mode = 1
        _check_model_bound(model)
        coverage = validate_timing_model_coverage(view, model, timing_types)
        clock_map = _clock_map(view, clocks)
        _timing_io_contract(timing_io, clock_map)
        engine = require_opensta_engine(engine_path)
        if 'launcher_sha256' in engine:
            binary = os.environ.get('EMUFLOW_OPENSTA_CONTAINER_BINARY')
            if not binary:
                raise ValidationError('OpenSTA container engine identity lacks an explicit binary')
            source = bind_store_source(Path(binary).expanduser(), 'external-tool-executable-v1')
            if file_sha256(source.path) != engine['executable_sha256']:
                raise ValidationError('OpenSTA container engine identity changed')
            sources.append(('container-binary', source))
        configuration = {
            'design': view.value['design']['name'],
            'liberty': render_opensta_liberty(model),
            'clock_map': clock_map,
            'timing_io': timing_io,
            'timing_io_sha256': timing_io_sha256(timing_io),
            'model': {'name': model['name'], 'qualification': model['source']['qualification'],
                      'architecture_timing_db': str(Path(architecture_timing_db_path).resolve())
                      if architecture_timing_db_path is not None else None,
                      'source': model['source'], 'cell_types': sorted(model['cells'])},
            'engine': engine,
            'used_cell_type_summary': _sealed_string_set_summary(coverage['used_cell_types']),
            'export_mode': mode,
            'max_paths': max_paths,
            'optimization_enabled': optimization_enabled,
            'criticality_scale': float(criticality_scale),
            'criticality_exponent': float(criticality_exponent),
        }
        if view.source != sources[0][1]:
            raise ValidationError('Phase 2 EmuIR generation changed during configuration')
    value = {'schema': 'emuflow.phase2-policy/v1', 'configuration': configuration,
             'sources': [{'role': role, 'path': str(source.path), 'schema': source.schema,
                          'generation': list(source.generation)} for role, source in sources]}
    encoded = _encode_policy(value)
    require_store_sources_unchanged(source for role, source in sources)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    output = directory / 'phase2-policy.json'
    output.write_text(encoded, encoding='utf-8')
    require_store_sources_unchanged(source for role, source in sources)
    return output


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
                   str(sources[0].path), str(policy), str(output)]
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
