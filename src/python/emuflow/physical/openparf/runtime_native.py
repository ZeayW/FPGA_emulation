"""Bounded locator host for native OpenPARF execution and acceptance."""
from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, resolve_native_executable, require_native_schema
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.physical.handoff import _small


def invoke_runtime(mode, report_path, *, sources=(), python=None, installation=None,
                   environment=None, detailed=False, checkpoint=False):
    require_native_schema('openparf-runtime-request-v1')
    report_path=Path(report_path).absolute()
    report_path.parent.mkdir(parents=True,exist_ok=True)
    rows=[]
    for role,path in sources:
        path=Path(path).resolve()
        rows.append({'role':role,'path':str(path),'generation':list(native_file_generation(path))})
    value={'schema':'emuflow.openparf-runtime-request/v1','mode':mode,'sources':rows,
        'python':str(python) if python is not None else '',
        'installation':str(installation) if installation is not None else '',
        'environment':environment or {},'allow_detailed_legalization_handoff':detailed,'use_checkpoint':checkpoint}
    with tempfile.TemporaryDirectory(prefix='.openparf-runtime-',dir=report_path.parent) as temporary:
        root=Path(temporary);request,proof=root/'request.json',root/'operation.json'
        text=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
        if len(text.encode())>1024*1024:
            raise ValidationError('OpenPARF runtime control exceeds bound')
        request.write_text(text,encoding='utf-8')
        executable=resolve_native_executable('emuflow-native')
        command=[executable,'--report',str(proof),'physical','openparf-runtime',str(request),str(report_path)]
        with NativeToolProcess(executable,command,environment=dict(os.environ),scratch_parent=root,
                              termination_grace_ms=5000) as process:
            completed=process.finish()
        if completed.returncode:
            raise ValidationError('native OpenPARF runtime failed: '+completed.stdout)
        operation=_small(proof,262144)
        outputs=operation.get('outputs',[])
        fields=('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
        expected=1 if mode=='preflight' else 2 if mode=='metrics' else 4 if checkpoint else 3
        if (operation.get('stage')!='physical.openparf-runtime' or operation.get('status')!='pass'
                or len(outputs)!=expected or outputs[0].get('path')!=str(report_path.resolve())
                or outputs[0].get('schema')!='openparf-runtime-report-v1'):
            raise ValidationError('native OpenPARF runtime proof disagrees')
        for output in outputs:
            if tuple(output.get('generation',{}).get(k) for k in fields)!=native_file_generation(output['path']):
                raise ValidationError('native OpenPARF runtime output changed')
        for source in rows:
            if list(native_file_generation(source['path']))!=source['generation']:
                raise ValidationError('OpenPARF runtime input changed')
        result=_small(report_path)
        if (result.get('schema')!='emuflow.openparf-runtime-report/v1' or result.get('status')!='pass'
                or result.get('implementation_id')!='native-openparf-runtime-v1' or result.get('mode')!=mode):
            raise ValidationError('native OpenPARF runtime result disagrees')
        if mode=='run':
            paths=[result['placement'],result['metrics_path']]
            if checkpoint:paths.append(result['checkpoint'])
            if [output['path'] for output in outputs[1:]]!=[str(Path(p).resolve()) for p in paths]:
                raise ValidationError('native OpenPARF runtime result ownership disagrees')
        elif mode=='metrics' and outputs[1]['path']!=rows[0]['path']:
            raise ValidationError('native OpenPARF metric ownership disagrees')
        result['stage_metrics']=operation['metrics']
        return result


