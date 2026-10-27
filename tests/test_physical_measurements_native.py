from tests.timing_facts import boundary_timing_facts
from tests.timing_facts import logic_timing_facts, local_timing_facts
"""Current physical measurement producers emit sealed typed records natively."""
from contextlib import ExitStack
import importlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json
from emuflow.physical.physical_measurements_native import import_physical_measurements
from emuflow.physical.vivado.vivado_physical_timing import import_indexed_vivado_boundary, import_vivado_local_timing
from emuflow.timing.logic_segment_timing import import_vivado_logic_segment_timing, VIVADO_LOGIC_SEGMENT_TIMING_HEADER
from emuflow.timing.local_path_timing import LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION
from tests import test_logic_segment_timing as logic_fixture
from tests.test_physical_store_publication import producer


@pytest.fixture(autouse=True)
def native_runtime(tmp_path,monkeypatch):
    if not os.environ.get('EMUFLOW_NATIVE_ROOT'):
        from tests.test_native_schedule_generation import install_runtime
        install_runtime(tmp_path,monkeypatch)


def _module(kind):
    return importlib.import_module('emuflow.timing.'+{2:'boundary_timing',3:'logic_segment_timing',4:'local_path_timing'}[kind]+'_storage')


def _case(root,kind,count=5):
    root.mkdir(parents=True,exist_ok=True)
    identity=root/'identity.json';rows=[]
    if kind==2:
        with producer('boundary_identity',identity) as view:
            header=dict(view.selector['header']);expected=list(view['endpoints'])
        for i,row in enumerate(reversed(expected)):
            rows.append([row['id'],row['kind'],i+0.25,'start-'+row['id'],'end-'+row['id']])
    elif kind==3:
        identity,_=logic_fixture.LogicSegmentTimingTest()._typed_identity(root,count)
        from emuflow.timing.logic_segment_identity_storage import read_logic_segment_identity_storage
        with read_logic_segment_identity_storage(identity) as view:
            header=dict(view.selector['header']);expected=list(view['segments'])
        rows=[[row['id'],row['kind'],i+0.25,row['start_pin'],row['end_pin'],'endpoint-exact',row['start_pin'],row['end_pin']] for i,row in enumerate(expected)]
    else:
        from tests.timing_facts import local_timing_facts
        from tests.test_local_path_timing_storage import _header,_record
        header=_header();header.update(qualification=LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION)
        header['coverage']={'local_paths':count};header['source']['original_paths']=count
        expected=[{**_record(f'path-{i:05d}-雪',0.0),'measurement':'endpoint-longest-path-fallback','path_pins':[]} for i in reversed(range(count))]
        with local_timing_facts(identity,header,expected) as view:header=dict(view.selector['header'])
        rows=[[r['id'],'local',i+0.25,r['start_pin'],r['end_pin'],'endpoint-exact',r['start_pin'],r['end_pin']] for i,r in enumerate(sorted(expected,key=lambda r:r['id']))]
    raw=root/'measurements.tsv'
    _tsv(raw,rows,kind)
    return identity,raw,header,expected,rows


def _tsv(path,rows,kind):
    header='endpoint_hex\tkind\tdelay_ns\tstart_object_hex\tend_object_hex' if kind==2 else VIVADO_LOGIC_SEGMENT_TIMING_HEADER
    with path.open('w') as f:
        f.write(header+'\n')
        for row in rows:
            fields=[str(v) for v in row]
            for i in [0,3,4]+([] if kind==2 else [6,7]):fields[i]=fields[i].encode().hex()
            f.write('\t'.join(fields)+'\n')


def _run(kind,raw,identity,output,**kwargs):
    if kind==2:return import_indexed_vivado_boundary(raw,identity,output,**kwargs)
    if kind==3:return import_vivado_logic_segment_timing(raw,identity,output,**kwargs)
    return import_vivado_local_timing(raw,identity,output)


def _reference(kind,output,identity_header,expected,rows):
    m=_module(kind);measurements={row[0]:row for row in rows}
    if kind==2:
        from emuflow.timing.boundary_timing import boundary_timing_header
        header=boundary_timing_header(identity_header,provider='vivado-indexed-routed-boundary-v1',qualification='routed-vendor-device-boundary')
        records=[{'id':row['id'],'kind':row['kind'],'schedule_entry':row['schedule_entry'],
                  'delay_ns':measurements[row['id']][2],'start_object':measurements[row['id']][3],
                  'end_object':measurements[row['id']][4],
                  'measurement':'logical-source-to-tx-port' if row['kind']=='tx' else 'rx-port-to-shadow-capture'}
                 for row in expected]
        return boundary_timing_facts(output,header,records)
    if kind==3:
        header={**identity_header,'schema':m.LOGIC_SEGMENT_TIMING_SCHEMA,'provider':'vivado-routed-logic-segment-datapath-v1','qualification':'routed-vendor-device-endpoint-chain'}
        records=[{**row,'delay_ns':measurements[row['id']][2],'measurement':measurements[row['id']][5],
                  'actual_start_object':measurements[row['id']][6],'actual_end_object':measurements[row['id']][7]} for row in expected]
        return logic_timing_facts(output,header=header,segments=sorted(records,key=lambda r:(r['system_path'],r['member_path'],r['cut_index'],r['id'])))
    header={**identity_header,'provider':'vivado-source-bound-local-endpoint-delay-v1','qualification':'routed-vendor-device-local-endpoint-longest-path'}
    records=[{**row,'delay_ns':measurements[row['id']][2]} for row in expected]
    return local_timing_facts(output,header,records)


@pytest.mark.parametrize('kind',[2,3,4])
def test_native_import_matches_complete_independent_writer_records_and_seals(tmp_path,kind):
    identity,raw,header,expected,rows=_case(tmp_path,kind)
    output=tmp_path/'timing.json';reference=tmp_path/'reference.json'
    with _reference(kind,reference,header,expected,rows) as view:
        digest=view.selector['logical_sha256'];records=list(view['endpoints' if kind==2 else 'segments' if kind==3 else 'paths'])
    m=_module(kind);open_path=Path.open
    def no_python_tsv(path,*a,**kw):
        if path.resolve()==raw.resolve():raise AssertionError('Python measurement population')
        return open_path(path,*a,**kw)
    with ExitStack() as stack:
        stack.enter_context(patch.object(Path,'open',no_python_tsv))
        assert not hasattr(m, 'write_'+m.__name__.split('.')[-1].replace('_storage','')+'_storage')
        if kind==3:
            from emuflow.timing import logic_segment_identity_storage as identities
            stack.enter_context(patch.object(identities,'segment_from_row',side_effect=AssertionError('Python source identity population')))
        report=_run(kind,raw,identity,output)
    reader=getattr(m,'read_'+m.__name__.split('.')[-1].replace('_storage','')+'_storage')
    with reader(output) as view:
        assert view.selector['logical_sha256']==digest
        assert list(view['endpoints' if kind==2 else 'segments' if kind==3 else 'paths'])==records
    assert report['maximum_delay_ns']==max(row[2] for row in rows)
    assert not list(tmp_path.glob('*.native.sqlite'))
    assert not list(tmp_path.glob('.physical-measurements.*'))


@pytest.mark.parametrize('kind',[2,3,4])
@pytest.mark.parametrize('damage',['missing','duplicate','unknown','negative','nonfinite','kind','truncated'])
def test_native_import_failure_preserves_prior_owner_and_retires_scratch(tmp_path,kind,damage):
    identity,raw,header,expected,rows=_case(tmp_path,kind)
    output=tmp_path/'timing.json';_run(kind,raw,identity,output)
    old=output.read_bytes();selector=read_json(output);store=tmp_path/selector['artifact'];old_bytes=store.read_bytes()
    rows=[list(row) for row in rows]
    if damage=='missing':rows=rows[:-1]
    elif damage=='duplicate':rows.append(rows[0])
    elif damage=='unknown':rows[0][0]='unknown'
    elif damage=='negative':rows[0][2]=-1
    elif damage=='nonfinite':rows[0][2]='nan'
    elif damage=='kind':rows[0][1]='wrong'
    _tsv(raw,rows,kind)
    if damage=='truncated':raw.write_bytes(raw.read_bytes()[:-1]+b'0')
    with pytest.raises(ValidationError):_run(kind,raw,identity,output)
    assert output.read_bytes()==old and store.read_bytes()==old_bytes
    assert not list(tmp_path.glob('*.native.sqlite'))
    assert not list(tmp_path.glob('.physical-measurements.*'))


@pytest.mark.parametrize('kind',[2,3,4])
def test_source_mutation_after_native_import_cannot_publish(tmp_path,kind):
    from emuflow.physical import physical_measurements_native as native
    identity,raw,header,expected,rows=_case(tmp_path,kind)
    output=tmp_path/'timing.json';_run(kind,raw,identity,output);prior=output.read_bytes()
    finish=native.NativeToolProcess.finish
    def change(process):
        result=finish(process)
        raw.chmod(raw.stat().st_mode ^ 0o100)
        return result
    with patch.object(native.NativeToolProcess,'finish',change),pytest.raises(ValidationError,match='generation changed'):
        _run(kind,raw,identity,output)
    assert output.read_bytes()==prior


def test_typed_vivado_query_uses_shared_local_owner_and_rejects_retired_private_schema(tmp_path, monkeypatch):
    from emuflow.timing.local_path_timing import read_local_path_identity
    from emuflow.physical.vivado.vivado_physical_timing import write_vivado_local_query
    from tests.local_path_query_fixture import sources, prepare
    from tests.native_build import emuflow_native
    monkeypatch.setenv('EMUFLOW_NATIVE_ROOT',str(emuflow_native().parent.parent))
    case=sources(tmp_path/'sources')
    report=write_vivado_local_query(case.merged['fpga0'],'fpga0',tmp_path/'query.tsv',
        tmp_path/'identity.json',prepare(case))
    identity=Path(report['identity'])
    assert read_json(identity)['schema']=='emuflow.local-path-timing-storage/v1'
    with read_local_path_identity(identity) as view:
        assert view.validate_identity()['local_paths']==1


@pytest.mark.parametrize('count,missing',[(17,False),(17,True),(0,False)])
def test_native_logic_members_missing_order_and_canonical_records(tmp_path,count,missing):
    from emuflow.timing.logic_segment_timing_storage import read_logic_segment_timing_storage
    identity,raw,header,expected,rows=_case(tmp_path,3,count=count)
    if missing:rows=rows[::2];_tsv(raw,rows,3)
    output=tmp_path/'timing.json';report=_run(3,raw,identity,output,allow_missing=missing)
    with read_logic_segment_timing_storage(output) as view:
        assert view.selector['counts']['segments']==len(rows)
        assert view.selector['counts']['unmeasured_segments']==count-len(rows)
        assert report['missing_segments']==count-len(rows)
    if len(rows)>1:
        _tsv(raw,list(reversed(rows)),3)
        with pytest.raises(ValidationError,match='order'):_run(3,raw,identity,tmp_path/'bad.json')


@pytest.mark.parametrize('kind',[2,3,4])
def test_native_import_rejects_bad_crc_and_preserves_previous_publication(tmp_path,kind):
    from emuflow.physical import physical_measurements_native as native
    identity,raw,header,expected,rows=_case(tmp_path,kind)
    output=tmp_path/'timing.json';_run(kind,raw,identity,output);old=output.read_bytes()
    finish=native.CheckedRequestWriter.finish
    def corrupt(writer):
        path=Path(writer.stream.name);finish(writer);data=bytearray(path.read_bytes());data[-1]^=1;path.write_bytes(data)
    with patch.object(native.CheckedRequestWriter,'finish',corrupt),pytest.raises(ValidationError):
        _run(kind,raw,identity,output)
    assert output.read_bytes()==old


def test_shared_local_identity_query_rejects_destination_aliases(tmp_path):
    from emuflow.physical.vivado.vivado_physical_timing import write_vivado_local_query
    from tests.test_vivado_indexed_physical import fixture
    from types import SimpleNamespace
    ir=fixture(tmp_path);old=ir.read_bytes()
    with pytest.raises(ValidationError,match='alias'):
        write_vivado_local_query(ir,'fpga0',ir,tmp_path/'identity.json',SimpleNamespace())
    assert ir.read_bytes()==old


def test_native_preserves_explicit_null_semantic_header_in_logical_namespace(tmp_path):
    from tests.logic_identity_fixture import identity_facts
    with producer('logic_segment_identity',tmp_path/'source.json') as view:
        header=dict(view.selector['header']);expected=list(view['segments'])
    header['semantic_contract_sha256']=None
    identity=tmp_path/'identity.json'
    with identity_facts(identity,header=header,segments=expected):pass
    rows=[[r['id'],r['kind'],.25,r['start_pin'],r['end_pin'],'endpoint-exact',r['start_pin'],r['end_pin']] for r in expected]
    raw=tmp_path/'measured.tsv';_tsv(raw,rows,3)
    with _reference(3,tmp_path/'reference.json',header,expected,rows) as view:digest=view.selector['logical_sha256']
    output=tmp_path/'timing.json';_run(3,raw,identity,output)
    assert read_json(output)['header']['semantic_contract_sha256'] is None
    assert read_json(output)['logical_sha256']==digest
