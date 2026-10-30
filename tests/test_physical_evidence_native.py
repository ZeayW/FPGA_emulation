from tests.timing_facts import local_timing_facts, logic_timing_facts
"""Physical typed readers validate all records in shared native owners."""
from contextlib import ExitStack
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from emuflow.common.errors import EmuFlowError, ValidationError
from emuflow_tools.document_io import read_json
from emuflow.common.json_io import write_json
from tests.test_physical_store_publication import producer
from tests.logic_identity_fixture import identity_facts

KINDS=('boundary_identity','boundary_timing','logic_segment_timing','local_path_timing','logic_segment_identity')


@pytest.fixture(autouse=True)
def native_runtime(tmp_path,monkeypatch):
    if not os.environ.get('EMUFLOW_NATIVE_ROOT'):
        from tests.test_native_schedule_generation import install_runtime
        install_runtime(tmp_path,monkeypatch)


def module(kind):
    return importlib.import_module('emuflow_tools.timing.'+kind+'_storage')


def read(kind,path):
    return getattr(module(kind),'read_'+kind+'_storage')(path)


def reseal_file(kind,path,sql):
    m=module(kind);selector=read_json(path);old=path.parent/selector['artifact']
    with sqlite3.connect(old) as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.executescript(sql)
    digest=hashlib.sha256(old.read_bytes()).hexdigest()
    new=path.with_name(m._store_name(path,digest))
    old.rename(new)
    selector.update(artifact=new.name,store_bytes=new.stat().st_size,store_sha256=digest)
    write_json(path,selector,compact=True)
    return selector


@pytest.mark.parametrize('kind',KINDS)
def test_reader_uses_native_full_record_hash_and_validation(tmp_path,kind):
    path=tmp_path/'owner.json'
    with producer(kind,path) as v:
        expected=v.selector['logical_sha256']
    m=module(kind)
    poison_names={'boundary_identity':['_iter_records','_normalize_record'],
                  'boundary_timing':['_record_from_row'],
                  'logic_segment_timing':['_segment_from_row'],
                  'logic_segment_identity':['segment_from_row'],
                  'local_path_timing':['_iter_records']}[kind]
    if kind in {'logic_segment_timing', 'local_path_timing', 'boundary_timing'}:
        assert not hasattr(m, '_validate_segment' if kind=='logic_segment_timing' else '_normalize_record')
    with ExitStack() as stack:
        for name in poison_names:stack.enter_context(patch.object(m,name,side_effect=AssertionError('Python full-record validation')))
        with read(kind,path) as view:
            assert view.selector['logical_sha256']==expected
            if hasattr(view,'validate_timing'):
                with patch.object(m,'_connect',side_effect=AssertionError('whole-table summary query')):
                    summary=view.validate_timing()
                    assert summary['maximum_delay_ns']>=0
    assert not hasattr(m,'_exact_sqlite_envelope')


@pytest.mark.parametrize('kind',KINDS)
def test_resealed_file_cannot_bypass_original_logical_seal(tmp_path,kind):
    path=tmp_path/'owner.json'
    with producer(kind,path):pass
    sql={'boundary_identity':"UPDATE endpoints SET peer='changed'",
         'boundary_timing':'UPDATE timings SET delay_ns=delay_ns+1',
         'logic_segment_timing':'UPDATE segments SET delay_ns=delay_ns+1',
         'logic_segment_identity':"UPDATE segments SET end_pin='changed'",
         'local_path_timing':'UPDATE timings SET delay_ns=delay_ns+1'}[kind]
    reseal_file(kind,path,sql)
    with pytest.raises(ValidationError,match='logical seal'):
        read(kind,path)


@pytest.mark.parametrize('kind',KINDS)
def test_native_exact_schema_rejects_extra_objects_after_reseal(tmp_path,kind):
    path=tmp_path/'owner.json'
    with producer(kind,path):pass
    reseal_file(kind,path,'CREATE TABLE surplus(payload TEXT);')
    with pytest.raises(ValidationError,match='schema objects'):
        read(kind,path)


@pytest.mark.parametrize('kind',KINDS)
def test_native_rejects_metadata_change_and_source_change(tmp_path,kind):
    path=tmp_path/'owner.json'
    with producer(kind,path):pass
    reseal_file(kind,path,"UPDATE metadata SET value='{}' WHERE key='counts'")
    with pytest.raises(ValidationError,match='metadata'):
        read(kind,path)


@pytest.mark.parametrize('damage',['ordinal','owner','wrong-end','fallback-pins','comma'])
def test_native_local_pin_chain_checks_survive_file_reseal(tmp_path,damage):
    path=tmp_path/'owner.json'
    with producer('local_path_timing',path):pass
    sql={'ordinal':"UPDATE path_pins SET ordinal=ordinal+10",
         'owner':"UPDATE path_pins SET path_id='unknown-'||path_id",
         'wrong-end':"UPDATE path_pins SET pin='wrong' WHERE ordinal=0",
         'fallback-pins':"UPDATE timings SET measurement='endpoint-longest-path-fallback'",
         'comma':"UPDATE path_pins SET pin='bad,pin'"}[damage]
    reseal_file('local_path_timing',path,sql)
    with pytest.raises(ValidationError,match='pin|chain|measurement'):
        read('local_path_timing',path)


@pytest.mark.parametrize('damage',['ordinal','owner','order'])
def test_native_boundary_register_checks_survive_file_reseal(tmp_path,damage):
    path=tmp_path/'owner.json'
    with producer('boundary_identity',path):pass
    sql={'ordinal':"UPDATE boundary_registers SET ordinal=5",
         'owner':"UPDATE boundary_registers SET endpoint_id='unknown'",
         'order':"INSERT INTO boundary_registers SELECT endpoint_id,1,'aaa' FROM boundary_registers"}[damage]
    reseal_file('boundary_identity',path,sql)
    with pytest.raises(ValidationError,match='register'):
        read('boundary_identity',path)


def test_native_rejection_has_no_python_fallback_and_cleans_request(tmp_path):
    from emuflow_tools.physical import physical_evidence_native as native
    path=tmp_path/'owner.json'
    with producer('boundary_timing',path):pass
    roots=[]
    temporary=native.tempfile.TemporaryDirectory
    def observe(*args,**kwargs):
        owner=temporary(*args,**kwargs);roots.append(Path(owner.name));return owner
    # Deliberately reject the selected executable; no record converter may run.
    with patch.object(native.tempfile,'TemporaryDirectory',observe), \
         patch.object(native,'resolve_native_executable',return_value='/usr/bin/false'),\
         patch.object(module('boundary_timing'),'_record_from_row',side_effect=AssertionError('fallback')):
        with pytest.raises(EmuFlowError,match='native tool supervisor'):
            read('boundary_timing',path)
    assert roots and all(not root.exists() for root in roots)


@pytest.mark.parametrize('kind',KINDS)
def test_utf8_control_strings_and_fractional_numbers_keep_original_logical_hash(tmp_path,kind):
    m=module(kind)
    if kind.startswith('boundary_'):
        fixture=importlib.import_module('tests.test_'+kind+'_storage');header=fixture._header()
        records=[fixture._record('端点-雪-😀\t"\\','tx',0)]
        if kind=='boundary_timing':records[0]['delay_ns']=1.2345678901234567e-5
        from tests.timing_facts import physical_facts_writer
        with physical_facts_writer(kind)(tmp_path/'owner.json',header,records) as v:
            digest=v.selector['logical_sha256']
    elif kind=='local_path_timing':
        from tests.test_local_path_timing_storage import _header,_record
        header=_header();header['coverage']={'local_paths':1};records=[_record('路径-😀\n"',1.2345678901234567e-5)]
        with local_timing_facts(tmp_path/'owner.json',header,records) as v:
            digest=v.selector['logical_sha256']
    elif kind=='logic_segment_identity':
        with producer(kind,tmp_path/'source.json') as original:header=dict(original.selector['header'])
        records=[{'id':'片段-😀\t\"','kind':'capture','system_path':'系统路径','member_path':'原始成员','cut_index':0,'fpga':'fpga0','replace_tx_endpoint':None,'start_pin':'start','end_pin':'end'}]
        with identity_facts(tmp_path/'owner.json',header=header,segments=records) as v:
            digest=v.selector['logical_sha256']
    else:
        header={'schema':m.LOGIC_SEGMENT_TIMING_SCHEMA,'status':'pass','design':'设计','platform':'板卡','fpga':'fpga0',
                'provider':'fixture','qualification':'fixture','coverage':{},'system_routes_sha256':'a'*64,'tdm_schedule_sha256':'b'*64}
        records=[{'id':'片段-😀\t"','kind':'capture','system_path':'系统路径','member_path':'原始成员','cut_index':0,
                  'fpga':'fpga0','replace_tx_endpoint':None,'start_pin':'start','end_pin':'end','delay_ns':1.2345678901234567e-5}]
        with logic_timing_facts(tmp_path/'owner.json',header=header,segments=records) as v:
            digest=v.selector['logical_sha256']
    with read(kind,tmp_path/'owner.json') as view:
        assert view.selector['logical_sha256']==digest


def test_native_logic_unsupported_unmeasured_and_collection_counts(tmp_path):
    m=module('logic_segment_timing')
    header={'schema':m.LOGIC_SEGMENT_TIMING_SCHEMA,'status':'pass','design':'dut','platform':'board','fpga':'fpga0',
            'provider':'fixture','qualification':'fixture','coverage':{},'system_routes_sha256':'a'*64,'tdm_schedule_sha256':'b'*64}
    row={'id':'measured','kind':'capture','system_path':'system','member_path':'member','cut_index':0,'fpga':'fpga0',
         'replace_tx_endpoint':None,'start_pin':'start','end_pin':'end','delay_ns':2.25,
         'measurement':'optimized-endpoint-collection-upper-bound','actual_start_object':'actual-start','actual_end_object':'actual-end'}
    path=tmp_path/'owner.json'
    with logic_timing_facts(path,header=header,segments=[row],
        unsupported_member_paths=[{'path':'unsupported','reason':'reason'}],
        unmeasured_segments=[{'id':'unmeasured','kind':'launch','system_path':'system','member_path':'missing','reason':'reason'}]) as v:
        expected=v.selector['counts'];digest=v.selector['logical_sha256']
    with read('logic_segment_timing',path) as v:
        assert v.selector['counts']==expected
        assert v.selector['logical_sha256']==digest
        assert v.validate_timing()['maximum_delay_ns']==2.25
    reseal_file('logic_segment_timing',path,"UPDATE unmeasured_segments SET segment_id='measured'")
    with pytest.raises(ValidationError,match='both measured and unmeasured'):
        read('logic_segment_timing',path)


@pytest.mark.parametrize('kind',KINDS)
def test_native_empty_store_counts_hash_and_summary(tmp_path,kind):
    with producer(kind,tmp_path/'original.json') as v:
        header=dict(v.selector['header'])
    if kind=='local_path_timing':header['coverage']={'local_paths':0}
    from tests.timing_facts import physical_facts_writer
    m=module(kind);writer=identity_facts if kind=='logic_segment_identity' else physical_facts_writer(kind);path=tmp_path/'empty.json'
    if kind.startswith('logic_'):
        owner=writer(path,header=header,segments=[])
    else:owner=writer(path,header,[])
    with owner as view:expected=view.selector['logical_sha256']
    with read(kind,path) as view:
        assert view.selector['logical_sha256']==expected
        assert all(value==0 for value in view.selector['counts'].values())
        if hasattr(view,'validate_timing'):assert view.validate_timing()['maximum_delay_ns']==0


@pytest.mark.parametrize("kind", ["boundary_identity"])
def test_publication_reuses_producer_validation_without_reader_replay(tmp_path,kind):
    m=module(kind)
    names={"boundary_identity":"BoundaryIdentityView","boundary_timing":"BoundaryTimingView",
           "logic_segment_timing":"LogicSegmentTimingView","local_path_timing":"LocalPathTimingView","logic_segment_identity":"LogicSegmentIdentityView"}
    with patch.object(getattr(m,names[kind]),"_validate_store",side_effect=AssertionError("published population replay")):
        with producer(kind,tmp_path/"owner.json") as view:
            if hasattr(view,"validate_timing"):
                assert view.validate_timing()["maximum_delay_ns"]>=0
    with read(kind,tmp_path/"owner.json") as view:
        if hasattr(view,"validate_timing"):
            assert view.validate_timing()["maximum_delay_ns"]>=0
