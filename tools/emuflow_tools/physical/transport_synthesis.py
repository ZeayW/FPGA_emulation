"""Bounded file locators for the complete native transport synthesis owner."""
import json
import os
from pathlib import Path
import tempfile
from emuflow.common.errors import ValidationError
from emuflow.common.json_io import file_sha256
from emuflow.common.paths import REPOSITORY_ROOT
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable, native_install_roots
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.common.control_document import read_small_object


def synthesize_transport(runtime_rtl,transport_rtl,output_dir,*,executable=None,maximum_fanout=512,timeout_ms=0):
    require_native_schema('transport-synthesis-request-v1')
    sources=[]
    for role,path,schema in [('runtime',runtime_rtl,'yosys-source-document-v1'),('rtl',transport_rtl,'yosys-source-document-v1'),
            ('yosys',resolve_native_executable('yosys',executable),'external-tool-executable-v1')]:
        path=Path(path).resolve()
        sources.append({'role':role,'path':str(path),'schema':schema,'generation':list(native_file_generation(path))})
    launcher=next((p for p in (REPOSITORY_ROOT/'scripts/yosys/container_runtime.sh',
        *(r/'share/emuflow/scripts/yosys/container_runtime.sh' for r in native_install_roots())) if p.is_file()),None)
    if launcher is not None and file_sha256(Path(sources[2]['path']))==file_sha256(launcher):
        binary=os.environ.get('EMUFLOW_YOSYS_CONTAINER_BINARY')
        if not binary:raise ValidationError('Yosys container engine identity lacks an explicit binary')
        binary=Path(binary).resolve()
        sources.append({'role':'container','path':str(binary),'schema':'external-tool-executable-v1','generation':list(native_file_generation(binary))})
    value={'schema':'emuflow.transport-synthesis-request/v1','sources':sources,'maximum_fanout':maximum_fanout,'timeout_ms':timeout_ms}
    output=Path(output_dir).absolute();output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.transport-synthesis-host-',dir=output.parent) as temporary:
        root=Path(temporary);request,operation=root/'request.json',root/'operation.json'
        text=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
        if len(text.encode())>1024*1024:raise ValidationError('transport synthesis control exceeds bound')
        request.write_text(text,encoding='utf-8')
        native=resolve_native_executable('emuflow-native')
        with NativeToolProcess(native,[native,'--report',str(operation),'physical','synthesize-transport',str(request),str(output)],
                environment=dict(os.environ),scratch_parent=root,termination_grace_ms=5000) as process:
            completed=process.finish()
        if completed.returncode:raise ValidationError('native transport synthesis failed: '+completed.stdout)
        proof=read_small_object(operation,262144);rows=proof.get('outputs',[])
        expected=[(output/'transport-synthesis-report.json','transport-synthesis-report-v1'),
            (output/'transport.emuir.sqlite','emuir-compact-v2'),(output/'transport-fanout-buffering.json','physical-input-document-v1'),
            (output/'transport-yosys.log','exact-sha256-text-v1')]
        if proof.get('status')!='pass' or proof.get('stage')!='physical.synthesize-transport' or len(rows)!=4:
            raise ValidationError('native transport synthesis proof disagrees')
        fields=('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
        def unchanged():
            for row,(path,schema) in zip(rows,expected):
                if (row.get('path')!=str(path.resolve()) or row.get('schema')!=schema or
                        tuple(row.get('generation',{}).get(key) for key in fields)!=native_file_generation(path)):
                    raise ValidationError('transport synthesis output generation changed')
            for row in sources:
                if row['generation']!=list(native_file_generation(row['path'])):
                    raise ValidationError('transport synthesis source generation changed')
        unchanged();result=read_small_object(expected[0][0])
        if (result.get('schema')!='emuflow.transport-synthesis-report/v1' or result.get('status')!='pass'
                or result.get('implementation_id')!='native-transport-synthesis-v1' or result.get('output')!=str(expected[1][0].resolve())):
            raise ValidationError('native transport synthesis report disagrees')
        unchanged();result['native_owner']={'implementation_id':result['implementation_id'],'metrics':proof['metrics']}
        return result
