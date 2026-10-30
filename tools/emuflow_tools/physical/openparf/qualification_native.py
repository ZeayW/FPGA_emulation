"""Source-reference host for the complete native partition placement stage."""
from __future__ import annotations

from emuflow.common.paths import PACKAGE_ROOT
import json
import os
from pathlib import Path
import tempfile
from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, resolve_native_executable, require_native_schema
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.common.control_document import read_small_object
from emuflow_tools.physical.openparf.input_native import atomic_input_sources
from emuflow.physical.openparf.openparf import _runtime_location


def run_qualification(mapped_path, packed_path, architecture_path, output_dir, *, top=None,
        openparf_install=None, openparf_python=None, native_constraints_path=None,
        provider_manifest_path=None, mapped_value=None, packed_value=None,
        allowed_slrs=None, constraints_path=None, expected_part=None):
    require_native_schema('openparf-qualification-request-v1')
    sources,top=atomic_input_sources(mapped_path,packed_path,architecture_path,top=top,
        native_constraints_path=native_constraints_path,provider_manifest_path=provider_manifest_path,
        mapped_value=mapped_value,packed_value=packed_value)
    sources.append(('driver',PACKAGE_ROOT / 'physical/openparf/openparf_native_driver.py','physical-input-document-v1'))
    if constraints_path is not None:sources.append(('constraints',constraints_path,'physical-input-document-v1'))
    rows=[]
    for role,path,schema in sources:
        path=Path(path).resolve()
        rows.append({'role':role,'path':str(path),'schema':schema,'generation':list(native_file_generation(path))})
    installation,python,environment=_runtime_location(openparf_install,openparf_python)
    output_dir=Path(output_dir).absolute();output_dir.parent.mkdir(parents=True,exist_ok=True)
    value={'schema':'emuflow.openparf-qualification-request/v1','sources':rows,'top':top,
        'allowed_slrs':list(allowed_slrs) if allowed_slrs is not None else None,
        'runtime':{'python':str(python),'installation':str(installation),'environment':environment},
        'expected_part':expected_part or '','plan_region':bool(expected_part and allowed_slrs is None and constraints_path is None)}
    with tempfile.TemporaryDirectory(prefix='.openparf-stage-',dir=output_dir.parent) as temporary:
        root=Path(temporary);request,proof=root/'request.json',root/'operation.json'
        text=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
        if len(text.encode())>1024*1024:raise ValidationError('OpenPARF stage control exceeds bound')
        request.write_text(text,encoding='utf-8')
        executable=resolve_native_executable('emuflow-native')
        command=[executable,'--report',str(proof),'physical','qualify-openparf',str(request),str(output_dir)]
        with NativeToolProcess(executable,command,environment=dict(os.environ),scratch_parent=root,termination_grace_ms=5000) as process:
            completed=process.finish()
        if completed.returncode:raise ValidationError('native OpenPARF qualification failed: '+completed.stdout)
        operation=read_small_object(proof,262144);outputs=operation.get('outputs',[])
        report=(output_dir/'qualification-report.json').resolve()
        fields=('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
        if (operation.get('stage')!='physical.qualify-openparf' or operation.get('status')!='pass'
                or len(outputs)!=(12 if value['plan_region'] else 11) or outputs[0].get('path')!=str(report)
                or outputs[0].get('schema')!='openparf-qualification-report-v1'):
            raise ValidationError('native OpenPARF stage proof disagrees')
        for row in outputs:
            if tuple(row.get('generation',{}).get(k) for k in fields)!=native_file_generation(row['path']):
                raise ValidationError('native OpenPARF stage output changed')
        for source in rows:
            if list(native_file_generation(source['path']))!=source['generation']:
                raise ValidationError('OpenPARF stage source changed')
        result=read_small_object(report)
        if (result.get('schema')!='emuflow.openparf-qualification-report/v1' or result.get('status')!='pass'
                or result.get('implementation_id')!='native-openparf-qualification-v1'
                or result.get('handoff_generation')!=list(native_file_generation(result['bridge']['physical_handoff']))):
            raise ValidationError('native OpenPARF stage result disagrees')
        result['native_stage_metrics']=operation['metrics']
        return result
