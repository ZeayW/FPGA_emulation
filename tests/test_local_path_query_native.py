"""Complete local-path ownership/query gates; NVDLA remains terminal acceptance."""
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.local_path_timing import read_local_path_identity, path_id_set_sha256
from emuflow.local_path_query_native import _execute
from emuflow.native_tools import native_file_generation
from emuflow.native_protocol import CheckedRequestWriter
from tests.local_path_query_fixture import circuit, ep, sources, prepare, query
from tests.native_build import emuflow_native


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
    monkeypatch.setenv('EMUFLOW_NATIVE_ROOT', str(emuflow_native().parent.parent))


def port_circuit(*, pure=False):
    value = circuit()
    value['ports'] = [{'id':'address','direction':'input','width':1},
                      {'id':'read_data','direction':'output','width':1}]
    if pure:
        value['instances'] = []
        value['nets'] = [{'id':'input','cut_class':'undriven','drivers':[ep(None,'address')],
                          'sinks':[ep(None,'read_data')]}]
    else:
        value['instances'] = [{'id':name,'type':'LUT1','resources':{'lut':1}}
                              for name in ('logic','unrelated')]
        value['nets'] = [{'id':'input','cut_class':'primary_input','drivers':[ep(None,'address')],
            'sinks':[ep('logic','I0'),ep('unrelated','I0')]},
            {'id':'output','cut_class':'combinational','drivers':[ep('logic','O')],
             'sinks':[ep(None,'read_data')]}]
    return value


def test_prepare_and_query_do_not_construct_python_source_graph(tmp_path):
    value = sources(tmp_path/'input')
    identity_id = value.path_facts[0][0]
    with (patch('emuflow.ir.EmuIR.load', side_effect=AssertionError('whole source graph')),
          patch('emuflow.sta.iter_sta_path_database_paths', side_effect=AssertionError('Python path records'))):
        prepared = prepare(value)
        report = query(value,tmp_path/'output',prepared=prepared)
    assert prepared.original_ir is None
    assert not any(hasattr(prepared,key) for key in ('path_endpoints','local_paths_by_fpga',
        'cross_path_ids','original_nets','assignment','database','routes'))
    assert prepared.source_manifest['original_path_ids_sha256']==path_id_set_sha256([identity_id])
    with read_local_path_identity(Path(report['identity'])) as identity:
        assert identity.validate_identity()['local_paths']==1
        row=identity['paths'][0]
        assert row['id']==identity_id and row['required_time_ns']==10
        assert row['start_pin']=='cell:launch/Q' and row['end_pin']=='cell:capture/D'
    assert Path(report['query']).read_text().splitlines()==[
        'endpoint\tkind\tstart_pin\tend_pin\tpath_pins',
        identity_id+'\tlocal\tcell:launch/Q\tcell:capture/D\t-']


@pytest.mark.parametrize('backend',['xilinx','vivado'])
def test_both_backends_publish_same_original_population_and_current_fallback(tmp_path,backend):
    value=sources(tmp_path/'input')
    result=query(value,tmp_path/'output',backend=backend)
    with read_local_path_identity(Path(result['identity'])) as identity:
        row=identity['paths'][0]
        assert row['measurement']=='endpoint-longest-path-fallback' and row['path_pins']==[]
        assert row['required_time_ns']==10 and row['clock_domain']=='clk'
        prefix='cell:' if backend=='xilinx' else ''
        assert row['start_pin']==prefix+'launch/Q' and row['end_pin']==prefix+'capture/D'
    assert result['measurement_counts']=={'explicit-routed-path-chain':0,'endpoint-longest-path-fallback':1}
    assert result['validation']['status']=='pass'
    assert len(Path(result['query']).read_text().splitlines())==2


def test_selected_driver_ownership_ignores_unrelated_remote_fanout(tmp_path):
    value=sources(tmp_path/'input',value=port_circuit(),owners={'logic':'fpga0','unrelated':'fpga1'},
        paths=[('address->read_data#00000000','clk',10,1,9,['input','output'])])
    prepared=prepare(value)
    with sqlite3.connect(prepared.partition_source.path) as db:
        assert db.execute('SELECT * FROM path_ownership').fetchall()==[(0,0,'fpga0')]
    result=query(value,tmp_path/'output',prepared=prepared)
    with read_local_path_identity(Path(result['identity'])) as identity:
        assert (identity['paths'][0]['start_pin'],identity['paths'][0]['end_pin'])==('top:address','top:read_data')


def test_unreported_cross_partition_path_fails_in_full_owner(tmp_path):
    value=sources(tmp_path/'input',owners={'capture':'fpga0','launch':'fpga0','logic':'fpga1'})
    with pytest.raises(ValidationError,match='cross-partition'):
        prepare(value)


def test_pure_primary_port_passthrough_does_not_invent_an_fpga(tmp_path):
    value=sources(tmp_path/'input',value=port_circuit(pure=True),owners={},
        paths=[('address->read_data#00000000','clk',10,1,9,['input'])])
    with pytest.raises(ValidationError,match='no unique assigned FPGA'):
        prepare(value)


def test_compressed_route_members_and_local_paths_cover_every_original_once(tmp_path):
    value=sources(tmp_path/'input',paths=[
        ('launch/Q->capture/D#00000000','clk',10,1,9,['n0','n1']),
        ('launch/Q->capture/D#00000001','clk',10,1,9,['n0','n1']),
        ('launch/Q->capture/D#00000002','clk',10,1,9,['n0','n1'])],cross=[{
            'path':'launch/Q->capture/D#00000001','compressed_path_ids':[
                'launch/Q->capture/D#00000001','launch/Q->capture/D#00000002']}])
    prepared=prepare(value)
    with sqlite3.connect(prepared.partition_source.path) as db:
        assert db.execute('SELECT * FROM path_ownership ORDER BY path_ordinal').fetchall()==[
            (0,0,'fpga0'),(1,1,None),(2,1,None)]
    result=query(value,tmp_path/'output',prepared=prepared)
    assert result['local_paths']==1
    assert prepared.source_manifest['original_paths']==3
    assert prepared.source_manifest['original_path_ids_sha256']==path_id_set_sha256([p[0] for p in value.path_facts])


@pytest.mark.parametrize('backend',['xilinx','vivado'])
def test_empty_fpga_query_retains_complete_source_seal(tmp_path,backend):
    value=sources(tmp_path/'input')
    result=query(value,tmp_path/'output',backend=backend,fpga='fpga1')
    assert result['local_paths']==0
    with read_local_path_identity(Path(result['identity'])) as identity:
        assert identity.validate_identity()['local_paths']==0
        assert identity['source']['original_paths']==1
    assert len(Path(result['query']).read_text().splitlines())==1


@pytest.mark.parametrize('bad_cross',[
    [{'path':'absent'}],
    [{'path':'launch/Q->capture/D#00000000','compressed_path_ids':[]}],
    [{'path':'launch/Q->capture/D#00000000','compressed_path_ids':['launch/Q->capture/D#00000000']},
     {'path':'other','compressed_path_ids':['launch/Q->capture/D#00000000']}],
])
def test_unknown_empty_or_duplicate_route_member_population_is_rejected(tmp_path,bad_cross):
    value=sources(tmp_path/'input',cross=bad_cross)
    with pytest.raises(ValidationError):prepare(value)


@pytest.mark.parametrize('source_index',[0,1,2,3,4,5,6])
def test_prepare_refuses_replaced_source_generation_before_publication(tmp_path,source_index):
    from emuflow.local_path_query_native import _sources
    value=sources(tmp_path/'input')
    original=_sources(value.original,value.assignment,value.paths,value.routes)
    selected=original[source_index].path
    selected.write_bytes(selected.read_bytes())
    with pytest.raises(ValidationError,match='generation'):
        _execute('prepare-local-paths',original,tmp_path/'published',tmp_path)
    assert not (tmp_path/'published').exists()


def test_query_cannot_borrow_ownership_index_from_another_run(tmp_path):
    from emuflow.local_path_query_native import write_native_local_path_query
    from dataclasses import replace
    a=sources(tmp_path/'a');b=sources(tmp_path/'b')
    prepared_a,prepared_b=prepare(a),prepare(b)
    mixed=replace(prepared_b,partition_source=prepared_a.partition_source)
    with pytest.raises(ValidationError,match='another source generation'):
        write_native_local_path_query(b.merged['fpga0'],'fpga0',tmp_path/'query',tmp_path/'identity',mixed,backend='xilinx')
    assert not list(tmp_path.glob('identity.generation.*'))


def test_retired_python_preparation_and_query_spool_are_absent():
    import emuflow.local_path_timing as local
    for name in ('_LocalPathPreparationWriter','_PreparedLocalPathSequence','_resolve_local_path_fpga','_write_local_path_query'):
        assert not hasattr(local,name)
