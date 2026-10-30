"""Bounded configuration and adoption for the native transport stage."""
from contextlib import closing
import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation, require_native_schema, resolve_native_executable
from emuflow.storage.store_publication import bind_store_source, bind_view_sources, require_store_sources_unchanged, publish_sqlite_store, validate_store_destination, VerifiedStoreFile, StoreSource
from emuflow.common.tool_runtime import NativeToolProcess


def original_sources(metadata):
    """Retain the original typed authorities through physical publication."""
    configuration=metadata.get('configuration')
    if type(configuration) is not dict:
        raise ValidationError('typed transport has no original configuration')
    rows=configuration.get('sources')
    if type(rows) is not list or len(rows) not in (2,3):
        raise ValidationError('typed transport original-source coverage disagrees')
    result=[]
    for index,row in enumerate(rows):
        schema=('assignment-index-v4','tdm-schedule-store-v4','pin-plan-store-v1')[index]
        if (type(row) is not dict or set(row)!={'path','schema','generation'}
            or row.get('schema')!=schema or type(row.get('path')) is not str
            or not Path(row['path']).is_absolute() or type(row.get('generation')) is not list
            or len(row['generation'])!=7 or any(type(value) is not int for value in row['generation'])):
            raise ValidationError('typed transport original source is malformed')
        result.append(StoreSource(Path(row['path']),schema,tuple(row['generation'])))
    require_store_sources_unchanged(result)
    return tuple(result)


def audit_transport_result(view):
    sources=view.bound_store_sources()
    with tempfile.TemporaryDirectory(prefix='transport-audit-') as directory:
        path=Path(directory)/'audit.json';tool=resolve_native_executable('emuflow-native')
        with NativeToolProcess(tool,[tool,'phase6','audit-transport',str(view.store_path),str(path)],
                               environment=dict(os.environ),scratch_parent=Path(directory)) as process:
            result=process.finish()
        if result.returncode:raise ValidationError('native transport audit failed: '+result.stdout)
        if path.stat().st_size>32*1024*1024:raise ValidationError('physical-port audit exceeds bounded device inventory')
        value=json.loads(path.read_text())
    require_store_sources_unchanged(sources)
    if value.get('status')!='pass' or type(value.get('ports')) is not list:
        raise ValidationError('native transport audit is malformed')
    return value


def audit_transport(view):
    return audit_transport_result(view)['ports']


def transport_request(assignment,schedule,platform,*,design,clocks,
                      pin_plan=None,virtual_board=None,runtime=None,emit_rtl=False):
    """Prepare bounded configuration; record populations stay in their owners."""
    schedule_path=getattr(schedule,'store_path',None)
    pin_path=getattr(pin_plan,'store_path',None) if pin_plan is not None else None
    if schedule_path is None or (pin_plan is not None and pin_path is None):
        raise ValidationError('native transport requires typed schedule and pin-plan owners')
    original=[bind_store_source(assignment,'assignment-index-v4'),
              bind_store_source(schedule_path,'tdm-schedule-store-v4')]
    if pin_path is not None:original.append(bind_store_source(pin_path,'pin-plan-store-v1'))
    sources=tuple(original)+bind_view_sources((schedule,'tdm-schedule-store-v4'),(pin_plan,'pin-plan-store-v1'))
    request={
        'schema':'emuflow.transport-stage/v1',
        'sources':[{'path':str(ref.path),'schema':ref.schema,'generation':list(ref.generation)} for ref in original],
        'design':dict(design),'platform':platform.name,'clocks':list(clocks),
        'fpgas':[{'id':fpga.id,'part':fpga.part} for fpga in platform.fpgas],
        'links':[{'id':link.id,'mode':link.mode,'width':link.transport_bits_per_cycle_per_direction,
                  'payload':link.payload_bits_per_lane_per_cycle} for link in platform.links],
        'virtual_board':virtual_board,'emit_rtl':emit_rtl,'runtime':runtime,
    }
    return request,sources


def build_transport(output,assignment,schedule,platform,*,design,clocks,
                    pin_plan=None,virtual_board=None,runtime=None,emit_rtl=False):
    require_native_schema('transport-stage-request-v1')
    request,sources=transport_request(assignment,schedule,platform,design=design,clocks=clocks,
        pin_plan=pin_plan,virtual_board=virtual_board,runtime=runtime,emit_rtl=emit_rtl)
    schedule_path=getattr(schedule,'store_path',None)
    pin_path=getattr(pin_plan,'store_path',None) if pin_plan is not None else None
    output=Path(output);store=output/'transport.sqlite'
    sources=validate_store_destination(store,sources);output.mkdir(parents=True,exist_ok=True)
    encoded=json.dumps(request,ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False)
    if len(encoded.encode())>8*1024*1024:
        raise ValidationError('transport control request exceeds bound')
    with tempfile.TemporaryDirectory(prefix='.native-transport-',dir=output) as directory:
        root=Path(directory);configuration=root/'request.json';configuration.write_text(encoded+'\n')
        staged=root/'artifacts';operation=root/'operation.json';tool=resolve_native_executable('emuflow-native')
        command=[tool,'--report',str(operation),'phase6','build-transport',str(assignment),str(schedule_path),str(configuration),str(staged)]
        if pin_path is not None:command.append(str(pin_path))
        require_store_sources_unchanged(sources)
        with NativeToolProcess(tool,command,environment=dict(os.environ),scratch_parent=root) as process:
            result=process.finish()
        if result.returncode:raise ValidationError('native transport failed: '+result.stdout)
        if operation.stat().st_size>256*1024:raise ValidationError('native transport operation exceeds bound')
        proof=json.loads(operation.read_text())
        if proof.get('stage')!='phase6.transport' or proof.get('status')!='pass' or len(proof.get('outputs',[]))!=2:
            raise ValidationError('native transport producer proof disagrees')
        fields=('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
        for row,(name,schema) in zip(proof['outputs'],(('transport.sqlite','transport-store-v2'),('transport-summary.json','transport-stage-summary-v1'))):
            path=staged/name
            if row.get('path')!=str(path.resolve()) or row.get('schema')!=schema or tuple(row.get('generation',{}).get(key) for key in fields)!=native_file_generation(path):
                raise ValidationError('native transport original output generation changed')
        if (staged/'transport-summary.json').stat().st_size>256*1024:
            raise ValidationError('native transport summary exceeds bound')
        summary=json.loads((staged/'transport-summary.json').read_text())
        require_store_sources_unchanged(sources)
        fd,name=tempfile.mkstemp(prefix='.transport.sqlite.',suffix='.tmp',dir=output);os.close(fd);temporary=Path(name)
        try:
            os.replace(staged/'transport.sqlite',temporary)
            publish_sqlite_store(temporary,store,'transport-store-v2',sources=sources)
        finally:
            temporary.unlink(missing_ok=True)
        # All remaining files are bounded selectors or streamed engine inputs.
        # Their stage owns publication; no record is decoded in the host.
        for path in sorted(staged.rglob('*')):
            if path.is_file() and path.name!='transport-summary.json':
                relative=path.relative_to(staged);target=output/relative
                target.parent.mkdir(parents=True,exist_ok=True);os.replace(path,target)
        require_store_sources_unchanged(sources)
    from emuflow_tools.transport.transport_storage import TransportView, AnchorView, LaneMapView, VirtualBindingView
    owner=VerifiedStoreFile(store,'transport-store-v2')
    order=summary.pop('fpga_order')
    summary.pop('status',None);summary.pop('implementation_id',None)
    transports={fpga:TransportView(output/fpga/'transport.json',store_owner=owner) for fpga in order}
    anchors={fpga:AnchorView(output/fpga/'virtual_anchors.json',store_owner=owner) for fpga in order}
    return {'lane_map':LaneMapView(output/'lane_map.json',store_owner=owner),
            'transports':transports,'anchors':anchors,
            'binding':VirtualBindingView(output/'virtual_board_binding.json',store_owner=owner) if virtual_board is not None else None,
            'summary':summary}
