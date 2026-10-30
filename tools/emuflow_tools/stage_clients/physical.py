"""Bounded file locators for the sole native multi-FPGA physical owner."""
from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, Mapping, Optional, Sequence
from emuflow.common.errors import ValidationError
from emuflow.common.json_io import file_sha256
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable, native_install_roots
from emuflow.common.paths import DATA_ROOT, REPOSITORY_ROOT
from emuflow.common.tool_runtime import NativeToolProcess
from emuflow.common.control_document import read_small_object
from emuflow.physical.xilinx_primitives import DEFAULT_XILINX_PRIMITIVE_LIBRARY

MULTI_FPGA_PHYSICAL_SCHEMA = "emuflow.multi-fpga-physical-flow/v1"
_FIELDS=('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')


def _source(role,path,schema):
    path=Path(path).resolve()
    return {'role':role,'path':str(path),'schema':schema,'generation':list(native_file_generation(path))}


def _installed(relative):
    return next((p for p in (REPOSITORY_ROOT/relative,*(r/'share/emuflow'/relative for r in native_install_roots())) if p.is_file()),None)


def _container(sources,role,engine,kind):
    launcher=_installed(Path('scripts')/kind/'container_runtime.sh')
    if launcher and file_sha256(engine)==file_sha256(launcher):
        value=os.environ.get('EMUFLOW_'+kind.upper()+'_CONTAINER_BINARY')
        if not value:raise ValidationError(kind+' container engine identity lacks an explicit binary')
        sources.append(_source(role,value,'external-tool-executable-v1'))


def _invoke(request,output):
    require_native_schema('physical-flow-request-v1')
    output=Path(output).absolute();output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.physical-flow-host-',dir=output.parent) as temporary:
        root=Path(temporary);source=root/'request.json';proof=root/'operation.json'
        text=json.dumps(request,sort_keys=True,separators=(',',':'),allow_nan=False)
        if len(text.encode())>8*1024*1024:raise ValidationError('physical flow control exceeds bound')
        source.write_text(text,encoding='utf-8')
        native=resolve_native_executable('emuflow-native')
        with NativeToolProcess(native,[native,'--report',str(proof),'physical','run-flow',str(source),str(output)],
                environment=dict(os.environ),scratch_parent=root,termination_grace_ms=15000) as process:
            completed=process.finish()
        if completed.returncode:raise ValidationError('native multi-FPGA physical flow failed: '+completed.stdout)
        operation=read_small_object(proof,1048576);rows=operation.get('outputs',[])
        partial=request['selected_fpgas'] is not None
        names=[('physical-partition-batch.json','physical-partition-batch-v1')] if partial else [
            ('multi-fpga-physical-flow-report.json','physical-flow-report-v1'),('physical-summary.json','physical-summary-v1')]
        if operation.get('stage')!='physical.run-flow' or operation.get('status')!='pass' or len(rows)!=len(names):
            raise ValidationError('native physical flow proof disagrees')
        def unchanged():
            for row,(name,schema) in zip(rows,names):
                path=(output/name).resolve()
                if (row.get('path')!=str(path) or row.get('schema')!=schema or
                        tuple(row.get('generation',{}).get(k) for k in _FIELDS)!=native_file_generation(path)):
                    raise ValidationError('native physical flow output generation changed')
            for row in request['sources']:
                if list(native_file_generation(row['path']))!=row['generation']:
                    raise ValidationError('native physical flow source generation changed')
        unchanged();report=read_small_object(output/names[0][0]);unchanged()
        schema='emuflow.physical-partition-batch/v1' if partial else MULTI_FPGA_PHYSICAL_SCHEMA
        if (report.get('schema')!=schema or report.get('status')!='pass' or
                report.get('implementation_id')!='native-multi-fpga-physical-v1'):
            raise ValidationError('native physical flow report disagrees')
        return report


def _backend_sources(backend, *, architecture=None, yosys=None, openparf_install=None, openparf_python=None, vivado=None, vivado_place_directive="Default", vivado_route_directive="Default", rapidwright_jar=None, rapidwright_java=None, rapidwright_java_source=None, rapidwright_device_data=None, rapidwright_timing_data=None, rapidwright_opensta=None, rapidwright_native_constraints=None, rapidwright_provider_manifest=None):
    engine=Path(resolve_native_executable("yosys",yosys))
    sources=[_source("library",DEFAULT_XILINX_PRIMITIVE_LIBRARY,"physical-input-document-v1"),
        _source("yosys",engine,"external-tool-executable-v1")]
    _container(sources,"yosys-container",engine,"yosys")
    if backend=='vivado':
        from emuflow_tools.physical.vivado.vivado_physical import _vivado_script
        sources.append(_source('backend.tool',resolve_native_executable('vivado',vivado),'external-tool-executable-v1'))
        for path in sorted(_vivado_script('implement_indexed_partition.tcl').parent.glob('*.tcl')):
            sources.append(_source('backend.script.'+path.name,path,'exact-sha256-text-v1'))
        settings={'place_directive':vivado_place_directive,'route_directive':vivado_route_directive,
            'threads':int(os.environ.get('EMUFLOW_VIVADO_THREADS','2'))}
    else:
        from emuflow.physical.openparf.openparf import _runtime_location
        from emuflow.physical.openparf import openparf_native_driver
        from emuflow.timing.rapidwright_contract import RAPIDWRIGHT_TIMING_CONTRACT
        from emuflow_tools.stage_clients.opensta import DEFAULT_TIMING_MODEL, OPENSTA_EXPORT_SCRIPT, OPENSTA_RUNTIME_CHECK_SCRIPT
        required=(architecture,rapidwright_jar,rapidwright_java,rapidwright_device_data,rapidwright_timing_data)
        if any(p is None for p in required):raise ValidationError('RapidWright backend runtime is incomplete')
        installation,python,environment=_runtime_location(openparf_install,openparf_python)
        java=Path(rapidwright_java).absolute();device=Path(rapidwright_device_data).resolve();timing=Path(rapidwright_timing_data).resolve()
        source=rapidwright_java_source or _installed(Path('scripts/rapidwright/EmuFlowRWRoute.java'))
        if source is None:raise ValidationError('RapidWright Java adapter source is missing')
        sta=Path(resolve_native_executable('sta',rapidwright_opensta))
        for role,path,schema in [('architecture',architecture,'physical-input-document-v1'),
                ('policy',DATA_ROOT/'openparf/input-policy.json','physical-input-document-v1'),
                ('driver',openparf_native_driver.__file__,'physical-input-document-v1'),
                ('sta',sta,'external-tool-executable-v1'),('base-model',DEFAULT_TIMING_MODEL,'phase2-source-v1'),
                ('export-script',OPENSTA_EXPORT_SCRIPT,'phase2-source-v1'),('runtime-script',OPENSTA_RUNTIME_CHECK_SCRIPT,'phase2-source-v1'),
                ('java',java,'external-tool-executable-v1'),('javac',java.with_name('javac'),'external-tool-executable-v1'),
                ('jar',rapidwright_jar,'physical-input-document-v1'),('java_source',source,'physical-input-document-v1'),
                ('contract',RAPIDWRIGHT_TIMING_CONTRACT,'physical-input-document-v1'),
                ('parts',device/'data/parts.db','physical-input-document-v1'),
                ('device',device/'data/devices/virtexuplus/xcvu19p_db.dat','physical-input-document-v1'),
                ('intersite',timing/'intersite_delay_terms.txt','physical-input-document-v1'),
                ('intrasite',timing/'intrasite_delay_terms.txt','physical-input-document-v1'),
                ('native',rapidwright_native_constraints,'physical-input-document-v1'),
                ('provider',rapidwright_provider_manifest,'physical-input-document-v1')]:
            sources.append(_source('backend.'+role,path,schema))
        _container(sources,'backend.container',sta,'opensta')
        settings={'openparf_runtime':{'python':str(python),'installation':str(installation),'environment':environment},
            'max_heap':os.environ.get('EMUFLOW_RAPIDWRIGHT_MAX_HEAP','32g'),'device_data_root':str(device)}
    return sources,settings


def run_multi_fpga_physical_flow(
    split_root: Path,
    platform_path: Path,
    schedule_path: Path,
    output_dir: Path,
    *,
    backend: str = "rapidwright",
    architecture: Optional[Path] = None,
    yosys: Optional[str] = None,
    openparf_install: Optional[Path] = None,
    openparf_python: Optional[Path] = None,
    seed: int = 1,
    vivado: Optional[str] = None,
    vivado_place_directive: str = "Default",
    vivado_route_directive: str = "Default",
    rapidwright_jar: Optional[Path] = None,
    rapidwright_java: Optional[Path] = None,
    rapidwright_java_source: Optional[Path] = None,
    rapidwright_device_data: Optional[Path] = None,
    rapidwright_timing_data: Optional[Path] = None,
    rapidwright_opensta: Optional[str] = None,
    rapidwright_native_constraints: Optional[Path] = None,
    rapidwright_provider_manifest: Optional[Path] = None,
    original_ir_path: Optional[Path] = None,
    assignment_path: Optional[Path] = None,
    routes_path: Optional[Path] = None,
    path_database_path: Optional[Path] = None,
    logic_path_database_path: Optional[Path] = None,
    workers: int = 1,
    resume: bool = False,
    fpga_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    if backend not in {'vivado','rapidwright'}:raise ValidationError('unsupported physical backend')
    if type(workers) is not int or not 1<=workers<=256:raise ValidationError('physical workers must be between 1 and 256')
    if type(seed) is not int or not 0<=seed<=0xffffffff:raise ValidationError('invalid physical seed')
    if fpga_ids is not None and (isinstance(fpga_ids,(str,bytes)) or not fpga_ids or
            any(type(f) is not str for f in fpga_ids) or len(set(fpga_ids))!=len(fpga_ids)):
        raise ValidationError('physical FPGA selection is empty, duplicated, or invalid')
    context=(original_ir_path,assignment_path,routes_path,path_database_path)
    if any(p is not None for p in context) and not all(p is not None for p in context):
        raise ValidationError('physical timing requires original IR, assignment, routes, and STA path database together')
    if logic_path_database_path is not None and path_database_path is None:
        raise ValidationError('physical logic timing database requires the complete original STA path database')
    if backend=='rapidwright' and (rapidwright_native_constraints is None or rapidwright_provider_manifest is None):
        raise ValidationError('openparf-native requires source-sealed RapidWright native constraints and provider manifest')
    if (rapidwright_native_constraints is None)!=(rapidwright_provider_manifest is None):
        raise ValidationError('RapidWright native placement requires both native constraints and provider manifest')
    sources,settings=_backend_sources(backend,
        architecture=architecture,
        yosys=yosys,
        openparf_install=openparf_install,
        openparf_python=openparf_python,
        vivado=vivado,
        vivado_place_directive=vivado_place_directive,
        vivado_route_directive=vivado_route_directive,
        rapidwright_jar=rapidwright_jar,
        rapidwright_java=rapidwright_java,
        rapidwright_java_source=rapidwright_java_source,
        rapidwright_device_data=rapidwright_device_data,
        rapidwright_timing_data=rapidwright_timing_data,
        rapidwright_opensta=rapidwright_opensta,
        rapidwright_native_constraints=rapidwright_native_constraints,
        rapidwright_provider_manifest=rapidwright_provider_manifest,
)
    sources += [_source("split-manifest",Path(split_root)/"manifest.json","physical-input-document-v1"),
        _source("board",platform_path,"boarddb-v1"),_source("schedule-selector",schedule_path,"tdm-schedule-selector-v4")]
    if all(p is not None for p in context):
        for role,path,schema in [('original',original_ir_path,'emuir-compact-v2'),
                ('assignment-selector',assignment_path,'physical-input-document-v1'),
                ('route-selector',routes_path,'system-route-selector-v1'),('path-selector',path_database_path,'sta-path-selector-v1'),
                ('schedule-board',Path(schedule_path).with_name('schedule-board.bin'),'exact-schedule-board-request-v1'),
                ('phase5-report',Path(schedule_path).with_name('phase5_report.json'),'phase5-report-v1')]:
            sources.append(_source(role,path,schema))
    if logic_path_database_path is not None:
        sources.append(_source('logic-path-selector',logic_path_database_path,'sta-path-selector-v1'))
    return _invoke({'schema':'emuflow.physical-flow-request/v1','sources':sources,'backend':backend,
        'backend_options':settings,'workers':workers,'seed':seed,'resume':bool(resume),
        'selected_fpgas':list(fpga_ids) if fpga_ids is not None else None},output_dir)


def validate_multi_fpga_physical_report(report,physical_summary=None,*,physical_summary_path=None):
    """Validate bounded reports through the independent native report validator."""
    with tempfile.TemporaryDirectory(prefix='emuflow-physical-report-') as temporary:
        root=Path(temporary);source=root/'report.json';output=root/'facts.json'
        text=json.dumps(report,allow_nan=False,separators=(',',':'))
        if len(text.encode())>8*1024*1024:raise ValidationError('physical report exceeds bound')
        source.write_text(text)
        native=resolve_native_executable('emuflow-native')
        argv=[native,'physical','validate-flow-report',str(source),str(output)]
        from emuflow.storage.store_publication import bind_store_source, require_store_sources_unchanged
        sources = []
        if physical_summary_path is not None:
            if physical_summary is not None:
                raise ValidationError('physical summary must have one explicit owner')
            source_ref = bind_store_source(Path(physical_summary_path).resolve(), 'physical-summary-v1')
            if source_ref.generation[2] > 8*1024*1024:
                raise ValidationError('physical summary exceeds bound')
            sources.append(source_ref)
            argv.append(str(source_ref.path))
        elif physical_summary is not None:
            # This API checks board-sized namespace/census facts. Evidence stays
            # in its typed owner, including for explicit small caller mappings.
            rows = physical_summary.get('fpgas')
            if not isinstance(rows, (list, tuple)) or len(rows) > 256 or any(not isinstance(row, Mapping) for row in rows):
                raise ValidationError('physical summary FPGA census is not bounded')
            facts = {key: physical_summary.get(key) for key in ('schema','status','design','platform')}
            facts['fpgas'] = [{'fpga': row.get('fpga'), 'original_cells': row.get('original_cells')} for row in rows]
            summary=root/'summary.json';text=json.dumps(facts,allow_nan=False,separators=(',',':'))
            if len(text.encode())>8*1024*1024:raise ValidationError('physical summary exceeds bound')
            summary.write_text(text);argv.append(str(summary))
        require_store_sources_unchanged(sources)
        with NativeToolProcess(native,argv,environment=dict(os.environ),scratch_parent=root) as process:
            result=process.finish()
        if result.returncode:raise ValidationError('native physical report validation failed: '+result.stdout)
        require_store_sources_unchanged(sources)
        return read_small_object(output)['summary']
