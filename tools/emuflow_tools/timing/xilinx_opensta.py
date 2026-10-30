"""Bounded artifact locators for complete native routed OpenSTA execution."""
from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.json_io import file_sha256
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.common.control_document import read_small_object
from emuflow.timing.engine_runtime import DEFAULT_TIMING_MODEL, OPENSTA_EXPORT_SCRIPT, OPENSTA_RUNTIME_CHECK_SCRIPT, _runtime_data_path

XILINX_ROUTED_OPENSTA_SCHEMA = 'emuflow.xilinx-routed-opensta-summary/v1'
_FIELDS = ('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')


def _invoke(mode, sources, configuration, bindings=None):
    require_native_schema('routed-opensta-stage-request-v1')
    output=Path(configuration['output_path']).absolute()
    output.parent.mkdir(parents=True,exist_ok=True)
    rows=[]
    for role,path,schema in sources:
        path=Path(path).resolve()
        bound=(bindings or {}).get(role)
        generation=list(native_file_generation(path))
        if bound is not None:
            if bound.path!=path or bound.schema!=schema or list(bound.generation)!=generation:
                raise ValidationError('routed OpenSTA source generation changed')
            generation=list(bound.generation)
        rows.append({'role':role,'path':str(path),'schema':schema,'generation':generation})
    value={'schema':'emuflow.routed-opensta-stage-request/v1','mode':mode,
        'sources':rows,'configuration':configuration}
    with tempfile.TemporaryDirectory(prefix='.routed-opensta-host-',dir=output.parent if mode=='run' else None) as temporary:
        root=Path(temporary);request,operation,accepted=root/'request.json',root/'operation.json',root/'accepted.json'
        text=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
        if len(text.encode())>1024*1024:raise ValidationError('routed OpenSTA control exceeds bound')
        request.write_text(text,encoding='utf-8')
        executable=resolve_native_executable('emuflow-native')
        command=[executable,'--report',str(operation),'physical','routed-opensta',str(request),str(accepted)]
        with NativeToolProcess(executable,command,environment=dict(os.environ),scratch_parent=root,termination_grace_ms=10000) as process:
            completed=process.finish()
        if completed.returncode:raise ValidationError('native routed OpenSTA failed: '+completed.stdout)
        proof=read_small_object(operation,262144);outputs=proof.get('outputs',[])
        if (proof.get('stage')!='physical.routed-opensta' or proof.get('status')!='pass' or len(outputs)!=(5 if mode=='run' else 4)
                or outputs[0].get('path')!=str(accepted.resolve()) or outputs[0].get('schema')!='routed-opensta-stage-report-v1'):
            raise ValidationError('native routed OpenSTA proof disagrees')
        def unchanged():
            for row in outputs:
                if tuple(row.get('generation',{}).get(k) for k in _FIELDS)!=native_file_generation(row['path']):
                    raise ValidationError('native routed OpenSTA output generation changed')
            for row in rows:
                if list(native_file_generation(row['path']))!=row['generation']:
                    raise ValidationError('routed OpenSTA source generation changed')
        unchanged()
        result=read_small_object(accepted)
        if (result.get('schema')!='emuflow.routed-opensta-stage-report/v1' or result.get('status')!='pass'
                or result.get('implementation_id')!='native-routed-opensta-stage-v1' or result.get('mode')!=mode
                or result.get('validation',{}).get('schema')!='emuflow.xilinx-routed-opensta-validation/v1'
                or result['validation'].get('status')!='pass'):
            raise ValidationError('native routed OpenSTA result disagrees')
        selector=read_small_object(output)
        payload=selector.get('payloads',{}).get('paths',{}).get('path')
        if not isinstance(payload,str) or Path(payload).name!=payload:
            raise ValidationError('native routed OpenSTA payload path disagrees')
        expected=[(output,'sta-path-selector-v1'),(output.parent/payload,'timing-path-store-v1'),
                  (Path(configuration['summary_path']),'physical-input-document-v1')]
        if mode=='run':expected.append((Path(configuration['log_path']),'physical-input-document-v1'))
        for row,(path,schema) in zip(outputs[1:],expected):
            if row.get('path')!=str(path.resolve()) or row.get('schema')!=schema:
                raise ValidationError('native routed OpenSTA ownership disagrees')
        unchanged()
        check=result['validation']
        check['native_owner']={'implementation_id':result['implementation_id'],'metrics':proof['metrics']}
        return check


def run_xilinx_routed_opensta(mapped_path,timing_path,output_path,summary_path,*,clocks,
        executable=None,max_paths=200000,log_path=None,timing_validation=None,source_sha256=None,timing_sources=None):
    sta=resolve_native_executable('sta',executable)
    output_path=Path(output_path).absolute();summary_path=Path(summary_path).absolute()
    log_path=Path(log_path).absolute() if log_path is not None else output_path.with_suffix('.log')
    sources=[('mapped',mapped_path,'xilinx-mapped-index-v2'),('timing',timing_path,'xilinx-routed-timing-v2'),
        ('base-model',DEFAULT_TIMING_MODEL,'phase2-source-v1'),('sta',sta,'external-tool-executable-v1'),
        ('export-script',OPENSTA_EXPORT_SCRIPT,'phase2-source-v1'),('runtime-script',OPENSTA_RUNTIME_CHECK_SCRIPT,'phase2-source-v1')]
    bindings={}
    if timing_sources is not None:
        if len(timing_sources)!=2:raise ValidationError('routed timing requires its original manifest/payload bindings')
        bindings=dict(zip(('timing','endpoints'),timing_sources))
        sources.append(('endpoints',timing_sources[1].path,'xilinx-routed-endpoints-v1'))
    launcher=_runtime_data_path(Path('scripts/opensta/container_runtime.sh'))
    wrapped=launcher.is_file() and file_sha256(Path(sta))==file_sha256(launcher)
    if wrapped:
        binary=os.environ.get('EMUFLOW_OPENSTA_CONTAINER_BINARY')
        if not binary:raise ValidationError('OpenSTA container engine identity lacks an explicit binary')
        sources.append(('container',binary,'external-tool-executable-v1'))
    return _invoke('run',sources,{'clocks':dict(sorted(clocks.items())),'max_paths':max_paths,
        'output_path':str(output_path),'summary_path':str(summary_path),'log_path':str(log_path),
        'source_sha256':dict(source_sha256) if source_sha256 is not None else None,
        'timing_validation':timing_validation},bindings=bindings)


def validate_xilinx_routed_opensta_summary(summary_path,*,output_path,mapped_path=None,timing_path=None,source_sha256=None):
    sources=[('paths',output_path,'sta-path-selector-v1'),('summary',summary_path,'physical-input-document-v1')]
    for role,path,schema in [('mapped',mapped_path,'xilinx-mapped-index-v2'),('timing',timing_path,'xilinx-routed-timing-v2')]:
        if path is not None:sources.append((role,path,schema))
    return _invoke('validate',sources,{'clocks':None,'max_paths':None,'log_path':None,
        'output_path':str(Path(output_path).absolute()),'summary_path':str(Path(summary_path).absolute()),
        'source_sha256':dict(source_sha256) if source_sha256 is not None else None,'timing_validation':None})
