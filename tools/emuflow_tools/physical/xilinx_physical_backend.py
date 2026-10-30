"""Bounded file locators for the complete native RapidWright partition."""
from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.json_io import file_sha256
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.common.paths import DATA_ROOT
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.common.control_document import read_small_object
from emuflow.physical.openparf.openparf import _runtime_location
from emuflow.physical.openparf import openparf_native_driver
from emuflow.timing.rapidwright_contract import RAPIDWRIGHT_TIMING_CONTRACT
from emuflow.physical.xilinx_primitives import DEFAULT_XILINX_PRIMITIVE_LIBRARY
from emuflow.timing.engine_runtime import DEFAULT_TIMING_MODEL, OPENSTA_EXPORT_SCRIPT, OPENSTA_RUNTIME_CHECK_SCRIPT, _runtime_data_path

_FIELDS=('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')


def run_rapidwright_openparf_native_candidate_backend(*,fpga,part,merged_ir_path,
        architecture_path,runtime,original_cells,transport_cells,output_dir,boundary_identity_path,
        rapidwright_jar,java,java_source,device_data_root,timing_data_dir,openparf_install=None,
        openparf_python=None,openparf_native_constraints=None,openparf_provider_manifest=None,
        opensta=None,logic_identity_path=None,local_identity_path=None,virtual_io_contract=None,resume=False):
    require_native_schema('rapidwright-partition-request-v1')
    installation,python,environment=_runtime_location(openparf_install,openparf_python)
    sta=Path(resolve_native_executable('sta',opensta))
    device_data_root=Path(device_data_root).resolve();timing_data_dir=Path(timing_data_dir).resolve()
    java=Path(java).absolute()
    sources=[('ir',merged_ir_path,'emuir-compact-v2'),('library',DEFAULT_XILINX_PRIMITIVE_LIBRARY,'physical-input-document-v1'),
        ('architecture',architecture_path,'physical-input-document-v1'),('boundary',boundary_identity_path,'boundary-identity-selector-v1'),
        ('policy',DATA_ROOT/'openparf/input-policy.json','physical-input-document-v1'),
        ('driver',Path(openparf_native_driver.__file__),'physical-input-document-v1'),
        ('sta',sta,'external-tool-executable-v1'),('base-model',DEFAULT_TIMING_MODEL,'phase2-source-v1'),
        ('export-script',OPENSTA_EXPORT_SCRIPT,'phase2-source-v1'),('runtime-script',OPENSTA_RUNTIME_CHECK_SCRIPT,'phase2-source-v1')]
    for role,path in [('java',java),('javac',java.with_name('javac')),('jar',rapidwright_jar),('java_source',java_source),
            ('contract',RAPIDWRIGHT_TIMING_CONTRACT),('parts',device_data_root/'data/parts.db'),
            ('device',device_data_root/'data/devices/virtexuplus/xcvu19p_db.dat'),
            ('intersite',timing_data_dir/'intersite_delay_terms.txt'),('intrasite',timing_data_dir/'intrasite_delay_terms.txt')]:
        sources.append((role,path,'external-tool-executable-v1' if role in ('java','javac') else 'physical-input-document-v1'))
    for role,path,schema in [('logic',logic_identity_path,'logic-segment-identity-selector-v1'),
            ('local',local_identity_path,'local-path-timing-selector-v1'),
            ('native',openparf_native_constraints,'physical-input-document-v1'),
            ('provider',openparf_provider_manifest,'physical-input-document-v1')]:
        if path is not None:sources.append((role,path,schema))
    launcher=_runtime_data_path(Path('scripts/opensta/container_runtime.sh'))
    if launcher.is_file() and file_sha256(sta)==file_sha256(launcher):
        binary=os.environ.get('EMUFLOW_OPENSTA_CONTAINER_BINARY')
        if not binary:raise ValidationError('OpenSTA container engine identity lacks an explicit binary')
        sources.append(('container',binary,'external-tool-executable-v1'))
    rows=[]
    for role,path,schema in sources:
        path=Path(path).resolve()
        rows.append({'role':role,'path':str(path),'schema':schema,'generation':list(native_file_generation(path))})
    value={'schema':'emuflow.rapidwright-partition-request/v1','sources':rows,'fpga':fpga,'part':part,
        'runtime':runtime,'original_cells':original_cells,'transport_cells':transport_cells,
        'openparf_runtime':{'python':str(python),'installation':str(installation),'environment':environment},
        'virtual_io':virtual_io_contract,'max_heap':os.environ.get('EMUFLOW_RAPIDWRIGHT_MAX_HEAP','32g'),
        'device_data_root':str(device_data_root),'resume':bool(resume)}
    text=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
    if len(text.encode())>8*1024*1024:raise ValidationError('RapidWright partition control exceeds bound')
    output_dir=Path(output_dir).absolute();output_dir.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.rapidwright-host-',dir=output_dir.parent) as temporary:
        root=Path(temporary);request,operation=root/'request.json',root/'operation.json'
        request.write_text(text,encoding='utf-8')
        native=resolve_native_executable('emuflow-native')
        with NativeToolProcess(native,[native,'--report',str(operation),'physical','run-rapidwright-partition',
                str(request),str(output_dir)],environment=dict(os.environ),scratch_parent=root,termination_grace_ms=10000) as process:
            completed=process.finish()
        if completed.returncode:raise ValidationError('native RapidWright partition failed: '+completed.stdout)
        proof=read_small_object(operation,1048576);outputs=proof.get('outputs',[])
        report=(output_dir/'partition-report.json').resolve()
        if (proof.get('status')!='pass' or proof.get('stage')!='physical.run-rapidwright-partition' or not outputs
                or outputs[0].get('schema')!='rapidwright-partition-report-v1' or outputs[0].get('path')!=str(report)):
            raise ValidationError('native RapidWright partition proof disagrees')
        def unchanged():
            for row in outputs:
                if tuple(row.get('generation',{}).get(k) for k in _FIELDS)!=native_file_generation(row['path']):
                    raise ValidationError('RapidWright partition output generation changed')
            for row in rows:
                if list(native_file_generation(row['path']))!=row['generation']:
                    raise ValidationError('RapidWright partition source generation changed')
        unchanged();result=read_small_object(report)
        if (result.get('schema')!='emuflow.rapidwright-partition-report/v1' or result.get('status')!='pass'
                or result.get('implementation_id')!='native-rapidwright-partition-v1'
                or result.get('result',{}).get('identity')!={'backend':'rapidwright','fpga':fpga,'part':part}):
            raise ValidationError('native RapidWright partition report disagrees')
        unchanged()
        result['native_owner']={'implementation_id':result['implementation_id'],'metrics':proof['metrics']}
        return result
