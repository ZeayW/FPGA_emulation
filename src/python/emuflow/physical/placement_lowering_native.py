"""Bounded input/output bridge for the complete native lowering stage."""
import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.storage.store_publication import bind_store_source, require_store_sources_unchanged
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.transport.transport_storage import read_transport


def lower_placement(split_path,fpga,transport_path,transport_ir_path,output_path,
                    report_path=None,boundary_identity_path=None):
    require_native_schema('placement-lowering-request-v1')
    output=Path(output_path).absolute()
    boundary=Path(boundary_identity_path).absolute() if boundary_identity_path is not None else output.with_name('boundary-identities.json')
    report=Path(report_path).absolute() if report_path is not None else output.with_name('placement-ir-report.json')
    if boundary.parent!=output.parent or report.parent!=output.parent:
        raise ValidationError('native lowering outputs must share one fresh stage directory')
    if output.parent.exists():
        raise ValidationError('native lowering stage directory already exists')
    transport=read_transport(Path(transport_path))
    sources=(bind_store_source(Path(split_path),'split-netlists-v3'),
             bind_store_source(transport.store_path,'transport-store-v2'),
             bind_store_source(Path(transport_ir_path),'emuir-compact-v2'))
    if transport['fpga']!=fpga:
        raise ValidationError('split and transport target different FPGAs')
    def reference(source):
        return {'path':str(source.path),'schema':source.schema,'generation':list(source.generation)}
    value={'schema':'emuflow.placement-lowering/v1','fpga':fpga,
           'sources':[reference(source) for source in sources],
           'output_name':output.name,'boundary_name':boundary.name,'report_name':report.name}
    output.parent.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.native-lowering-',dir=output.parent.parent) as temporary:
        root=Path(temporary);request=root/'request.json';operation=root/'operation.json'
        request.write_text(json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':'))+'\n')
        tool=resolve_native_executable('emuflow-native')
        argv=[tool,'--report',str(operation),'phase7','lower',
              *(str(source.path) for source in sources),str(request),str(output.parent)]
        require_store_sources_unchanged(sources)
        with NativeToolProcess(tool,argv,environment=dict(os.environ),scratch_parent=root) as process:
            result=process.finish()
        if result.returncode:
            raise ValidationError('native placement lowering failed: '+result.stdout)
        if operation.stat().st_size>262144:
            raise ValidationError('native lowering result exceeds bound')
        proof=json.loads(operation.read_text());outputs=proof.get('outputs',[])
        if proof.get('stage')!='phase7.lower' or proof.get('status')!='pass' or len(outputs)!=3:
            raise ValidationError('native lowering producer proof disagrees')
        fields=('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
        for row,(path,schema) in zip(outputs,((output,'emuir-compact-v2'),(boundary,'boundary-identity-selector-v1'),(report,'placement-lowering-report-v1'))):
            if (row.get('path')!=str(path.resolve()) or row.get('schema')!=schema or
                tuple(row.get('generation',{}).get(key) for key in fields)!=native_file_generation(path)):
                raise ValidationError('native lowering output generation changed')
        require_store_sources_unchanged(sources)
        if report.stat().st_size>262144:
            raise ValidationError('native lowering report exceeds bound')
        value=json.loads(report.read_text())
        if value.get('status')!='pass' or value.get('implementation_id')!='native-placement-lowering-v1':
            raise ValidationError('native lowering report identity disagrees')
        return value
