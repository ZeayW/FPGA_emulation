"""Native primary cluster contracts, independently compared with Python UF."""
from contextlib import closing
import gc
import json
from pathlib import Path
import sqlite3
import struct
import subprocess
from unittest.mock import patch
import pytest

from emuflow.emuir_compact import write_compact_emuir
from emuflow.errors import ValidationError
from emuflow.partition import build_clusters, CUT_MODE_STATIC_EXACT
from emuflow.partition_ir import load_partition_ir
from emuflow.phase3_clusters_native import build_native_clusters
from tests.native_build import emuflow_native
from tests import test_partition_pressure as reference_fixture


@pytest.fixture
def source(tmp_path,monkeypatch):
    native=tmp_path/'native'; (native/'bin').mkdir(parents=True)
    (native/'bin/emuflow-native').symlink_to(emuflow_native())
    monkeypatch.setenv('EMUFLOW_NATIVE_ROOT',str(native))
    fixture=reference_fixture.PartitionPressureTest(); fixture.setUp()
    path=tmp_path/'source.sqlite'
    write_compact_emuir(path,fixture.ir.value)
    return load_partition_ir(path),fixture.constraints


@pytest.mark.parametrize('mode',['normal','static','groups','fixed','unicode','resource-max','sparse'])
def test_primary_columns_match_independent_reference(source,mode):
    ir,constraints=source
    if mode=='groups':
        constraints=dict(constraints,groups=[{'id':'together','instances':['u0','u3']}])
    if mode=='fixed':
        constraints=dict(constraints,fixed=[{'instance':'u0','fpga':'a'}])
    if mode in {'unicode','resource-max','sparse'}:
        with closing(sqlite3.connect(ir.path)) as db,db:
            if mode=='unicode':
                db.execute("UPDATE cell_records SET id='é雪' WHERE seq=0")
            elif mode=='resource-max':
                row=db.execute('SELECT resources_seq FROM cell_records WHERE seq=0').fetchone()
                db.execute('UPDATE payloads SET value=? WHERE seq=?',(json.dumps({'lut':4294967295}),row[0]))
            else:
                db.execute('UPDATE endpoint_records SET ordinal=ordinal*7+3')
    options={'cut_mode':CUT_MODE_STATIC_EXACT if mode=='static' else 'sequential-only'}
    expected=build_clusters(ir,constraints,**options)
    with patch('emuflow.partition._build_clusters_from_compact_ir',side_effect=AssertionError('Python UF invoked')):
        actual=build_clusters(ir,constraints,_native_compact_clusters=True,**options)
    a,b=actual['clusters'],expected['clusters']
    assert list(a)==list(b)
    for key in ('_name_bytes','_name_offsets','_member_offsets','_member_ordinals','source_cluster_indices'):
        assert bytes(getattr(a,key))==bytes(getattr(b,key))
    assert [list(c) for c in a._resource_columns]==[list(c) for c in b._resource_columns]
    assert a.source_cluster_indices.readonly
    assert a.implementation_id=='native-primary-clusters-v1'
    assert a.native_metrics['wall_seconds']>0
    # A downstream owner may borrow a column after the cluster wrapper dies.
    borrowed=a.source_cluster_indices
    mapping=borrowed.owner
    root=Path(mapping.scratch.name)
    del actual,a
    gc.collect()
    assert root.exists() and len(borrowed)==len(ir.value['instances'])
    del borrowed,mapping
    gc.collect()
    assert not root.exists()


def test_fixed_conflict_is_rejected(source):
    ir,constraints=source
    constraints=dict(constraints,groups=[{'id':'same','instances':['u0','u1']}],
                     fixed=[{'instance':'u0','fpga':'a'},{'instance':'u1','fpga':'b'}])
    with pytest.raises(ValidationError,match='conflicting fixed'):
        build_clusters(ir,constraints,cut_mode="sequential-only", _native_compact_clusters=True)


@pytest.mark.parametrize('kind',['unknown-resource','negative-resource','boolean-resource','truncated-request','stale-generation','unknown-group','bad-padding'])
def test_invalid_source_or_request_preserves_existing_output(source,tmp_path,kind):
    ir,constraints=source
    if kind.endswith('resource'):
        values={'unknown-resource':{'mystery':1},'negative-resource':{'lut':-1},'boolean-resource':{'lut':True}}
        with closing(sqlite3.connect(ir.path)) as db,db:
            row=db.execute('SELECT resources_seq FROM cell_records LIMIT 1').fetchone()
            db.execute('UPDATE payloads SET value=? WHERE seq=?',(json.dumps(values[kind]),row[0]))
        with pytest.raises(ValidationError,match='native primary cluster construction failed'):
            build_native_clusters(ir,constraints,set())
        return
    from emuflow.native_tools import native_file_generation
    request=tmp_path/'request.bin'; output=tmp_path/'output.bin';output.write_bytes(b'old')
    cells,nets=len(ir.value['instances']),len(ir.value['nets'])
    bits=bytearray((nets+7)//8)
    if kind=='bad-padding':
        assert nets%8
        bits[-1]=128
    groups=struct.pack('<Q',0)
    if kind=='unknown-group':
        groups=struct.pack('<QQI',1,1,cells)
    payload=struct.pack('<QIQQQqqqqQQQ',0x31515243464D5545,1,*native_file_generation(ir.path),cells,nets,len(bits))+bits+groups
    request.write_bytes(payload[:-1] if kind=='truncated-request' else payload)
    if kind=='stale-generation':
        with closing(sqlite3.connect(ir.path)) as db,db:
            db.execute("UPDATE cell_records SET id=id||'x' WHERE seq=0")
    result=subprocess.run([str(emuflow_native()),'phase3','build-clusters',str(ir.path),str(request),str(output)],capture_output=True,text=True)
    assert result.returncode!=0
    assert output.read_bytes()==b'old'
    assert not list(tmp_path.glob('*.tmp*'))


def test_cyclic_exception_lifetime_releases_views_before_mapping(source):
    ir,constraints=source
    errors=[]
    with patch('sys.unraisablehook',side_effect=lambda event:errors.append(event.exc_value)):
        def fail():
            columns=build_native_clusters(ir,constraints,set())
            root=Path(columns.source_cluster_indices.owner.scratch.name)
            try:
                raise ValueError(root)
            except ValueError as error:
                cycle=[error,columns]
                cycle.append(cycle)
                return root
        root=fail()
        gc.collect()
    assert not root.exists(), ([(type(e).__name__,str(e)) for e in errors],
                               [p.name for p in root.iterdir()] if root.exists() else [])
    assert not errors


def test_large_fanout_primary_union_matches_reference(tmp_path,monkeypatch):
    # Contract scale fixture only; real acceptance continues to use NVDLA.
    native=tmp_path/'native'; (native/'bin').mkdir(parents=True)
    (native/'bin/emuflow-native').symlink_to(emuflow_native())
    monkeypatch.setenv('EMUFLOW_NATIVE_ROOT',str(native))
    fixture=reference_fixture.PartitionPressureTest(); fixture.setUp()
    value=fixture.ir.to_dict()
    import copy
    prototype=value['instances'][0]
    value['instances']=[dict(copy.deepcopy(prototype),id=f'cell-{i:05d}') for i in reversed(range(5000))]
    value['nets']=[dict(value['nets'][0],id='fanout',cut_class='combinational',
        drivers=[{'instance':'cell-00000','port':'O','bit':0}],
        sinks=[{'instance':f'cell-{i:05d}','port':'I','bit':0} for i in range(1,5000)])]
    path=tmp_path/'fanout.sqlite'; write_compact_emuir(path,value)
    ir=load_partition_ir(path)
    expected=build_clusters(ir,fixture.constraints, cut_mode="sequential-only")
    with patch('emuflow.partition._UnionFind',side_effect=AssertionError('Python UF invoked')):
        actual=build_clusters(ir,fixture.constraints,cut_mode="sequential-only", _native_compact_clusters=True)
    assert list(actual['clusters'])==list(expected['clusters'])
    assert len(actual['clusters'])==1


def test_source_setup_failure_retires_unmapped_scratch(source,tmp_path):
    ir,constraints=source
    scratch=tmp_path/'setup-scratch'; scratch.mkdir()
    with patch('emuflow.phase3_clusters_native.tempfile.mkdtemp',return_value=str(scratch)), \
         patch('emuflow.phase3_clusters_native.native_file_generation',side_effect=FileNotFoundError('missing source')):
        with pytest.raises(FileNotFoundError,match='missing source'):
            build_native_clusters(ir,constraints,set())
    assert not scratch.exists()


def test_native_cluster_totals_match_independent_resources(source, tmp_path, monkeypatch):
    ir, constraints = source
    columns = build_clusters(ir, constraints, cut_mode="sequential-only", _native_compact_clusters=True)['clusters']
    expected = [sum(column) for column in columns._resource_columns]
    with patch.object(type(columns._resource_columns[0]), '__iter__', side_effect=AssertionError('summary scanned native columns')):
        assert list(columns.native_resource_totals) == [len(columns.source_cluster_indices), *expected]
