"""Bounded configuration and report bridge for the native routing stage."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.platform.platform import Platform
from emuflow.storage.store_publication import StoreSource, bind_store_source, require_store_sources_unchanged
from emuflow.common.tool_runtime import NativeToolProcess


def prepare_configuration_request(path, board, *, assignment=None, timing_paths=None,
                   constraints_path=None, frame_slots=None, max_iterations=None,
                   candidate_workers=1, timing_driven=True, external_timing=None,
                   timing_model_path=None, architecture_timing_db_path=None):
    """Pass finite BoardDB/settings and original references to the native owner."""
    require_native_schema('phase4-configuration-v1')
    if type(candidate_workers) is not int or candidate_workers<1:
        raise ValidationError('candidate_workers must be a positive integer')
    if type(timing_driven) is not bool:
        raise ValidationError('timing_driven must be a boolean')
    if assignment is not None and Path(assignment).suffix!='.sqlite':
        raise ValidationError('Phase 4 requires an explicit assignment SQLite artifact; JSON assignment input is retired')
    sources=[('board',bind_store_source(Path(board),'boarddb-v1'))]
    for role,value,schema in [('assignment',assignment,'assignment-index-v4'),
            ('timing',timing_paths,'system-routing-paths-v1'),('constraints',constraints_path,'system-route-constraints-input-v1')]:
        if value is not None:sources.append((role,bind_store_source(Path(value),schema)))
    if external_timing is not None:
        sources.append(('external-timing',bind_store_source(Path(external_timing),'system-routing-paths-v1')))
    if timing_model_path is not None:
        from emuflow.timing.opensta import DEFAULT_TIMING_MODEL
        from emuflow.common.paths import DATA_ROOT
        sources.append(('timing-model',bind_store_source(DEFAULT_TIMING_MODEL if architecture_timing_db_path is not None else Path(timing_model_path),'phase2-source-v1')))
        if architecture_timing_db_path is not None:
            sources.extend([('architecture-timing-db',bind_store_source(Path(architecture_timing_db_path),'phase2-source-v1')),
                ('architecture-contract',bind_store_source(DATA_ROOT/'rapidwright/xcvu19p-timing-contract.json','phase2-source-v1'))])
    value={'schema':'emuflow.phase4-configuration/v1','board':Platform.load(sources[0][1].path).to_dict(),
        'options':{'frame_slots':frame_slots,'max_iterations':max_iterations,
            'candidate_workers':candidate_workers,'timing_driven':timing_driven},
        'sources':[{'role':role,'path':str(ref.path),'schema':ref.schema,'generation':list(ref.generation)} for role,ref in sources]}
    encoded=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n'
    if len(encoded.encode())>8*1024*1024:
        raise ValidationError('Phase 4 configuration exceeds bound')
    require_store_sources_unchanged(ref for role,ref in sources)
    path=Path(path);path.write_text(encoded)
    require_store_sources_unchanged(ref for role,ref in sources)
    return path


def prepare_policy(directory, assignment, board, **options):
    directory=Path(directory).resolve();directory.mkdir(parents=True,exist_ok=False)
    try:
        with tempfile.TemporaryDirectory(prefix='.phase4-policy-host-',dir=directory) as temporary:
            root=Path(temporary)
            request=prepare_configuration_request(root/'request.json',board,assignment=assignment,**options)
            output,operation=directory/'phase4-policy.json',root/'operation.json'
            native=resolve_native_executable('emuflow-native')
            with NativeToolProcess(native,[native,'--report',str(operation),'phase4','prepare-policy',str(request),str(output)],environment=dict(os.environ),scratch_parent=root) as process:
                result=process.finish()
            if result.returncode:
                raise ValidationError('native Phase 4 preparation failed: '+result.stdout)
            if operation.stat().st_size>256*1024:
                raise ValidationError('native Phase 4 preparation result exceeds bound')
            proof=json.loads(operation.read_text());outputs=proof.get('outputs',[])
            fields=('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
            if (proof.get('schema')!='emuflow.native-stage-result/v1' or proof.get('stage')!='phase4.prepare-policy'
                    or proof.get('status')!='pass' or len(outputs)!=1 or outputs[0].get('path')!=str(output)
                    or outputs[0].get('schema')!='phase4-policy-v1'
                    or tuple(outputs[0].get('generation',{}).get(k) for k in fields)!=native_file_generation(output)):
                raise ValidationError('native Phase 4 policy producer binding disagrees')
            return output
    except BaseException:
        import shutil
        shutil.rmtree(directory)
        raise


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
