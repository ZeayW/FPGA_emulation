"""Bounded configuration and report bridge for the native routing stage."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.platform.platform import Platform
from emuflow.routing.routing import load_route_constraints
from emuflow.storage.store_publication import StoreSource, bind_store_source, require_store_sources_unchanged
from emuflow.common.tool_runtime import NativeToolProcess


def prepare_policy(directory, assignment, board, *, timing_paths=None,
                   constraints_path=None, frame_slots=None, max_iterations=None,
                   candidate_workers=1, timing_driven=True):
    require_native_schema('phase4-policy-v1')
    if type(candidate_workers) is not int or candidate_workers < 1:
        raise ValidationError('candidate_workers must be a positive integer')
    if type(timing_driven) is not bool:
        raise ValidationError('timing_driven must be a boolean')
    assignment = Path(assignment)
    if assignment.suffix != '.sqlite':
        raise ValidationError('Phase 4 requires an explicit assignment SQLite artifact; JSON assignment input is retired')
    sources = [bind_store_source(assignment, 'assignment-index-v4'),
               bind_store_source(Path(board), 'boarddb-v1')]
    if timing_paths is not None:
        sources.append(bind_store_source(Path(timing_paths), 'system-routing-paths-v1'))
    platform = Platform.load(sources[1].path)
    constraints = load_route_constraints(constraints_path, platform,
        frame_slots=frame_slots, max_iterations=max_iterations)
    value = {'schema': 'emuflow.phase4-policy/v1', 'constraints': constraints,
             'candidate_workers': candidate_workers, 'timing_driven': timing_driven,
             'sources': [{'path': str(source.path), 'schema': source.schema,
                          'generation': list(source.generation)} for source in sources]}
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True,
                         separators=(',', ':'), allow_nan=False)
    if len(encoded.encode()) > 8 * 1024 * 1024:
        raise ValidationError('Phase 4 configuration exceeds bound')
    require_store_sources_unchanged(sources)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    output = directory / 'phase4-policy.json'
    output.write_text(encoded + '\n', encoding='utf-8')
    require_store_sources_unchanged(sources)
    return output


def run_native_phase4(assignment, board, output, *, timing_paths=None,
                      constraints_path=None, frame_slots=None, max_iterations=None,
                      candidate_workers=1, timing_driven=True, executable=None):
    output = Path(output).absolute()
    if output.exists():
        raise ValidationError('native Phase 4 requires a fresh stage directory')
    output.parent.mkdir(parents=True, exist_ok=True)
    tool = resolve_native_executable('emuflow-native', executable)
    if Path(tool).name != 'emuflow-native':
        raise ValidationError('Phase 4 requires the unified native entry')
    with tempfile.TemporaryDirectory(prefix='.native-phase4-', dir=output.parent) as temporary:
        root = Path(temporary)
        policy = prepare_policy(root / 'policy', assignment, board,
            timing_paths=timing_paths, constraints_path=constraints_path,
            frame_slots=frame_slots, max_iterations=max_iterations,
            candidate_workers=candidate_workers, timing_driven=timing_driven)
        sources = json.loads(policy.read_text())['sources']
        operation = root / 'operation.json'
        command = [tool, '--report', str(operation), 'phase4', 'run',
                   sources[0]['path'], sources[1]['path'], str(policy), str(output)]
        if timing_paths is not None:
            command.append(sources[2]['path'])
        with NativeToolProcess(tool, command, environment=dict(os.environ),
                               scratch_parent=root) as process:
            result = process.finish()
        if result.returncode:
            raise ValidationError('native Phase 4 failed: ' + result.stdout)
        if operation.stat().st_size > 262144:
            raise ValidationError('native Phase 4 operation exceeds bound')
        proof = json.loads(operation.read_text())
        fields = ('device', 'inode', 'bytes', 'mtime_seconds', 'mtime_nanoseconds',
                  'ctime_seconds', 'ctime_nanoseconds')
        if proof.get('schema') != 'emuflow.native-stage-result/v1' or proof.get('stage') != 'phase4.run' or proof.get('status') != 'pass' or len(proof.get('outputs', [])) != 2:
            raise ValidationError('native Phase 4 operation identity disagrees')
        outputs = []
        for row, (name, schema) in zip(proof['outputs'], (
                ('phase4_report.json', 'phase4-report-v1'),
                ('routes.json', 'system-route-selector-v1'))):
            path = output / name
            if (path.is_symlink() or row.get('path') != str(path.resolve()) or row.get('schema') != schema or
                    tuple(row.get('generation', {}).get(key) for key in fields) != native_file_generation(path)):
                raise ValidationError('native Phase 4 output generation changed')
            outputs.append(StoreSource(path, schema, tuple(row["generation"][key] for key in fields)))
        report = output / 'phase4_report.json'
        if report.stat().st_size > 262144:
            raise ValidationError('native Phase 4 report exceeds bound')
        value = json.loads(report.read_text())
        if value.get('status') != 'pass' or value.get('stage_implementation_id') != 'native-phase4-v1':
            raise ValidationError('native Phase 4 report identity disagrees')
        require_store_sources_unchanged(outputs)
        return value
