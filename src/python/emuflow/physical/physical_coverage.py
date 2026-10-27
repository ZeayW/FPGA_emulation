"""Source-bound native physical coverage owner and lazy typed record views."""
from collections.abc import Mapping, Sequence
from contextlib import closing
import json
import hashlib
from pathlib import Path
import sqlite3
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.timing.global_event_store import _native
from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri
from emuflow.storage.store_publication import bind_store_source, bind_view_sources, require_store_sources_unchanged, validate_store_destination, commit_store_selector, StoreSource

_CERTIFICATE = 'emuflow.cross-layer-physical-binding-certificate/v1'
_MAGIC = 0x31564350464D5545
_COLUMNS = {
    'endpoint_bindings': ('endpoint','kind','schedule_entry','fpga','measured_min_delay_ns','measured_max_delay_ns','measurement'),
    'logic_segment_bindings': ('segment','fpga','measured_min_delay_ns','measured_max_delay_ns','measurement'),
    'board_bindings': ('seq','schedule_entry','link','from','to','delay_bound_ns','measurement'),
    'missing_endpoints': ('endpoint',), 'missing_logic': ('segment',),
}


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',',':'), allow_nan=False)


def _request(path, sources, config):
    if len(sources)>2048:
        raise ValidationError('physical coverage source catalog exceeds bound')
    encoded = _canonical(config)
    if len(encoded.encode()) > 3*1024*1024:
        raise ValidationError('physical coverage bounded request exceeds limit')
    writer = CheckedRequestWriter(path)
    try:
        writer.put('QII', _MAGIC, 1, len(sources))
        for source in sources:writer.text(source.schema)
        for source in sources:writer.put('3Q4q', *source.generation)
        writer.text(encoded)
        writer.finish()
    finally:
        writer.close()


def _inputs(contract, schedule, physical, platform, assignment_index_path, *, fixture=False):
    from emuflow.scheduling.tdm_schedule_storage import TDMScheduleView, pretty_tdm_schedule_sha256
    from emuflow.timing.boundary_identity_storage import BoundaryIdentityView
    from emuflow.timing.boundary_timing_storage import BoundaryTimingView
    from emuflow.timing.logic_segment_timing_storage import LogicSegmentTimingView
    from emuflow.platform.board_link_timing import build_board_link_timing_model, validate_board_link_timing
    from emuflow.scheduling.cross_layer_timing import _canonical_sha256
    if not isinstance(schedule, TDMScheduleView):
        raise ValidationError('physical coverage requires current typed ScheduleStore; inline production is retired')
    if contract.get('platform') != platform.name or schedule.get('platform') != platform.name:
        raise ValidationError('physical coverage platform namespace disagrees')
    sources = list(bind_view_sources((schedule, 'tdm-schedule-store-v4')))
    def add(source):
        for i, existing in enumerate(sources):
            if existing.path==source.path and existing.schema==source.schema:
                if existing.generation!=source.generation:raise ValidationError('mixed physical coverage generation')
                return i
        sources.append(source)
        return len(sources)-1
    def view_ref(view, schema):
        refs=bind_view_sources((view,schema))
        selected=None
        for source in refs:
            index=add(source)
            if source.path==Path(view.store_path).resolve() and source.schema==schema:selected=index
        if selected is None:raise ValidationError('physical coverage typed source is missing')
        return selected
    config={'schema':'emuflow.physical-coverage-request/v1', 'contract':contract,
        'contract_sha256':_canonical_sha256(contract),
        'schedule_sha256':pretty_tdm_schedule_sha256(schedule),
        'schedule_logical_sha256':schedule.manifest['logical_sha256'],
        'schedule_sections':schedule.manifest['sections'],
        'schedule':view_ref(schedule,'tdm-schedule-store-v4'),
        'assignment':None,'exact':None,'fixture':fixture,
        'physical_source':{key:physical.get(key) for key in ('provider','qualification')},'fpgas':[]}
    indexed=contract.get('logic_segment_binding')
    if assignment_index_path is not None:
        from emuflow.partition.phase3_assignment_index import open_phase3_assignment_index, _exact_binary_reference
        assignment_source=bind_store_source(Path(assignment_index_path),'assignment-index-v4')
        with closing(open_phase3_assignment_index(assignment_source.path)) as connection:
            exact, descriptor=_exact_binary_reference(connection,assignment_source.path)
        require_store_sources_unchanged((assignment_source,))
        config['assignment']=add(assignment_source)
        config['exact']=add(bind_store_source(exact,'phase3-exact-data-v2'))
    elif indexed is not None:
        raise ValidationError('indexed physical coverage requires the original assignment owner')
    groups=[]
    for key, kind, schema in (
        ('boundary_identities',BoundaryIdentityView,'boundary-identity-store-v1'),
        ('boundary_timing',BoundaryTimingView,'boundary-timing-store-v1'),
        ('logic_segment_timing',LogicSegmentTimingView,'logic-segment-timing-store-v1'),
    ):
        group=physical.get(key,{})
        if not isinstance(group,Mapping) or len(group)>256 or any(not isinstance(row,kind) for row in group.values()):
            raise ValidationError('physical coverage requires complete typed physical owners')
        groups.append((group,schema))
    identities,timing,logic=(group for group,_ in groups)
    if set(identities)!=set(timing):
        raise ValidationError('physical boundary evidence coverage disagrees')
    for fpga in sorted(set(identities)|set(logic)):
        config['fpgas'].append({'fpga':fpga,
            'identity':view_ref(identities[fpga],groups[0][1]) if fpga in identities else None,
            'timing':view_ref(timing[fpga],groups[1][1]) if fpga in timing else None,
            'logic':view_ref(logic[fpga],groups[2][1]) if fpga in logic else None})
    board=physical.get('board_link_timing')
    if board is None:board=build_board_link_timing_model(platform)
    report=validate_board_link_timing(board,platform)
    config['board_links']=[{key:row[key] for key in ('link','from','to','delay_bound_ns','qualification')} for row in board['links']]
    config['board_signoff']=report['final_link_timing_signoff']
    require_store_sources_unchanged(sources)
    return sources, config


def _metadata(path):
    with closing(sqlite3.connect(immutable_sqlite_uri(path),uri=True)) as connection:
        result={}
        for key,value in connection.execute('SELECT key,CASE WHEN length(value)<=4194304 THEN value END FROM metadata'):
            if value is None:raise ValidationError('physical coverage metadata exceeds bound')
            result[key]=json.loads(value)
    if set(result)!={'header','sections','sources','logical_sha256'}:
        raise ValidationError('physical coverage metadata disagrees')
    return result


class _Records(Sequence):
    def __init__(self,owner,table):self.owner,self.table=owner,table
    def __len__(self):return self.owner.metadata['sections'][self.table]['records']
    def _value(self,row):
        if self.table.startswith('missing_'):return row[0]
        value=dict(zip(_COLUMNS[self.table],row))
        value.pop('seq',None)
        return value
    def __iter__(self):
        self.owner.require()
        cursor=self.owner.connection.execute('SELECT * FROM '+self.table+' ORDER BY 1')
        try:
            for row in cursor:
                self.owner.require()
                yield self._value(row)
        finally:cursor.close()
    def __getitem__(self,index):
        if isinstance(index,slice):return [self[i] for i in range(*index.indices(len(self)))]
        if index<0:index+=len(self)
        if not 0<=index<len(self):raise IndexError(index)
        self.owner.require()
        row=self.owner.connection.execute('SELECT * FROM '+self.table+' ORDER BY 1 LIMIT 1 OFFSET ?',(index,)).fetchone()
        if row is None:raise ValidationError('physical coverage record is missing')
        return self._value(row)
    def __eq__(self,other):return isinstance(other,Sequence) and len(self)==len(other) and all(a==b for a,b in zip(self,other))


def _certificate(metadata):
    return {'schema':_CERTIFICATE, **metadata}


def read_physical_coverage_certificate(path):
    """Read bounded evidence and source references; no graph payload is retained."""
    path=Path(path)
    if path.is_symlink() or path.stat().st_size > 4*1024*1024:
        raise ValidationError('physical coverage certificate exceeds bound or is a symlink')
    value=read_json(path)
    if not isinstance(value,dict) or set(value)!={'schema','header','sections','sources','logical_sha256'} or value['schema']!=_CERTIFICATE:
        raise ValidationError('physical coverage certificate schema disagrees')
    if not isinstance(value['sources'],list) or len(value['sources'])>2048:
        raise ValidationError('physical coverage source catalog exceeds bound')
    for source in value['sources']:
        if (not isinstance(source,dict) or set(source)!={'path','schema','generation'}
                or not isinstance(source['path'],str) or not Path(source['path']).is_absolute()
                or not isinstance(source['schema'],str) or len(source['schema'])>256
                or not isinstance(source['generation'],list) or len(source['generation'])!=7
                or any(type(number) is not int for number in source['generation'])):
            raise ValidationError('physical coverage source reference disagrees')
    if not isinstance(value['header'],dict) or value['header'].get('schema')!='emuflow.cross-layer-physical-binding/v1' or value['header'].get('status') not in {'pass','incomplete'}:
        raise ValidationError('physical coverage certificate header disagrees')
    if not isinstance(value['sections'],dict) or set(value['sections'])!=set(_COLUMNS):
        raise ValidationError('physical coverage section catalog disagrees')
    for section in value['sections'].values():
        if (not isinstance(section,dict) or set(section)!={'records','sha256'}
                or type(section['records']) is not int or section['records']<0
                or not isinstance(section['sha256'],str) or len(section['sha256'])!=64
                or any(c not in '0123456789abcdef' for c in section['sha256'])):
            raise ValidationError('physical coverage section seal disagrees')
    canonical=lambda row:json.dumps(row,ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False)
    digest=hashlib.sha256((canonical(value['header'])+'\n'+canonical(value['sections'])).encode()).hexdigest()
    if value['logical_sha256']!=digest:
        raise ValidationError('physical coverage certificate logical seal disagrees')
    return value


class PhysicalCoverageView(Mapping):
    """Invocation-owned diagnostic cursors; the permanent artifact is a certificate."""
    def __init__(self,selector,*,store_path,temporary):
        self.path=Path(selector)
        self.selector=read_physical_coverage_certificate(self.path)
        self.store_path=Path(store_path)
        self.sources=(bind_store_source(self.path,'physical-coverage-selector-v1'),
                      bind_store_source(self.store_path,'physical-coverage-store-v1'),
                      *(StoreSource(Path(row['path']),row['schema'],tuple(row['generation'])) for row in self.selector['sources']))
        self.metadata=_metadata(self.store_path)
        if _certificate(self.metadata)!=self.selector:
            raise ValidationError('private physical coverage records disagree with certificate')
        self.connection=sqlite3.connect(immutable_sqlite_uri(self.store_path),uri=True)
        self._temporary=temporary
        self.closed=False
        try:
            self.require()
        except BaseException:
            # A traceback retains the partially constructed view. Close before
            # its scratch owner unlinks the database, including on NFS.
            self.close()
            raise
    def require(self):
        if self.closed:raise ValidationError('physical coverage owner is closed; replay original sources for record queries')
        require_store_sources_unchanged(self.sources)
    def __getitem__(self,key):
        self.require()
        if key=='missing':return {'endpoints':_Records(self,'missing_endpoints'),'logic_segments':_Records(self,'missing_logic')}
        if key in _COLUMNS:return _Records(self,key)
        return self.metadata['header'][key]
    def __iter__(self):return iter((*self.metadata['header'],'endpoint_bindings','logic_segment_bindings','board_bindings','missing'))
    def __len__(self):return len(self.metadata['header'])+4
    def close(self):
        if getattr(self,'closed',True):return
        self.closed=True
        self.connection.close()
        if self._temporary is not None:self._temporary.cleanup();self._temporary=None
    def __del__(self):
        try:self.close()
        except BaseException:pass
    def __enter__(self):return self
    def __exit__(self,*_):self.close()


def _audit(sources,config,store,root):
    audit_sources=[*sources,bind_store_source(store,'physical-coverage-store-v1')]
    request=root/'audit.bin';summary=root/'audit.json'
    _request(request,audit_sources,config)
    _native(['audit-physical-coverage',*(source.path for source in audit_sources),request,summary],root)
    require_store_sources_unchanged(audit_sources)
    return read_json(summary)


def _produce(sources,config,root):
    request=root/'request.bin';store=root/'coverage.sqlite'
    _request(request,sources,config)
    _native(['build-physical-coverage',*(source.path for source in sources),request,store],root)
    header=_audit(sources,config,store,root)
    metadata=_metadata(store)
    if metadata['header']!=header:raise ValidationError('independent physical coverage summary disagrees')
    return store,metadata


def build_physical_coverage(contract,schedule,physical,platform,assignment_index_path=None,*,output_path=None,fixture=False):
    sources,config=_inputs(contract,schedule,physical,platform,assignment_index_path,fixture=fixture)
    if output_path is not None:
        output_path=Path(output_path).absolute()
        output_path.parent.mkdir(parents=True,exist_ok=True)
        if output_path.is_symlink():raise ValidationError('physical coverage certificate output is a symlink')
        validate_store_destination(output_path,sources)
    temporary=tempfile.TemporaryDirectory(prefix='.coverage-work-',dir=output_path.parent if output_path is not None else None)
    root=Path(temporary.name)
    if output_path is None:output_path=root/'cross_layer_physical_binding.json'
    try:
        store,metadata=_produce(sources,config,root)
        # The SQLite relation set is private audit scratch. Commit only this
        # bounded certificate; source changes roll back the previous selector.
        view=commit_store_selector(output_path,_certificate(metadata),sources,store,False,
            validate=lambda:PhysicalCoverageView(output_path,store_path=store,temporary=None))
        view._temporary=temporary
        return view
    except BaseException:
        temporary.cleanup()
        raise


def audit_physical_coverage(contract,schedule,physical,platform,binding,assignment_index_path=None,*,fixture=False):
    """Replay original typed owners and independently compare the terminal certificate."""
    if isinstance(binding,PhysicalCoverageView):
        certificate=read_physical_coverage_certificate(binding.path)
    elif isinstance(binding,(str,Path)):
        certificate=read_physical_coverage_certificate(binding)
    else:
        raise ValidationError('physical coverage validation requires its bounded certificate')
    sources,config=_inputs(contract,schedule,physical,platform,assignment_index_path,fixture=fixture)
    with tempfile.TemporaryDirectory(prefix='coverage-terminal-audit-') as scratch:
        store,metadata=_produce(sources,config,Path(scratch))
        if _certificate(metadata)!=certificate:
            raise ValidationError('physical coverage certificate does not match canonical source facts')
    header=metadata['header']
    return {'status':header['status'],**header['metrics']}


def replay_physical_coverage(contract,schedule,physical,platform,certificate,assignment_index_path=None,*,fixture=False):
    """Explicitly recreate transient diagnostic records from the original typed owners."""
    expected=read_physical_coverage_certificate(certificate)
    view=build_physical_coverage(contract,schedule,physical,platform,assignment_index_path,fixture=fixture)
    if view.selector!=expected:
        view.close()
        raise ValidationError('physical coverage replay does not match certificate source facts')
    return view
