"""Native candidate production against independent compact and graph oracles."""
from contextlib import closing
import gc
from pathlib import Path
import sqlite3
import subprocess
from unittest.mock import patch
import pytest

from tests.partition_graph_oracle import candidate_ids
from emuflow.ir.emuir_compact import write_compact_emuir
from emuflow.common.errors import ValidationError
from emuflow.ir.ir import EmuIR
from emuflow.partition.partition_ir import load_partition_ir
from tests.native_partition_core_fixture import build_native_candidate_ids
from tests.phase3_policy_reference import write_candidate_policy
from tests.native_build import emuflow_native
from tests import test_combinational_cut as reference


@pytest.fixture
def installed(tmp_path,monkeypatch):
    root=tmp_path/'native'; (root/'bin').mkdir(parents=True)
    (root/'bin/emuflow-native').symlink_to(emuflow_native())
    monkeypatch.setenv('EMUFLOW_NATIVE_ROOT',str(root))


@pytest.mark.parametrize('mode',['normal','reordered','duplicate-sink','duplicate-driver','top-driver','self-loop','hard-cycle',
                                 'memory-sink','DFF-safe','FDRE-safe','FDPE-unsafe','unicode','wide-integer','sparse'])
def test_candidate_records_match_two_independent_oracles(tmp_path,installed,mode):
    value=reference._chain_ir().to_dict()
    if mode=='reordered':
        value['instances'].reverse();value['nets'].reverse()
    if mode=='duplicate-sink':
        value['nets'][1]['sinks'].append(reference._endpoint('l1','I_extra'))
    if mode=='duplicate-driver':
        value['nets'][1]['drivers'].append(reference._endpoint('l0','O_extra'))
    if mode=='top-driver':
        value['nets'][1]['drivers'].append(reference._endpoint(None,'clk'))
    if mode=='self-loop':
        value['nets'][1]['sinks']=[reference._endpoint('l0','I0')]
    if mode=='hard-cycle':
        value['instances'][2]['resources']={'carry':1}
        value['nets'].append(dict(id='back',cut_class='combinational',drivers=[reference._endpoint('l2','O')],sinks=[reference._endpoint('l0','I0')]))
    if mode in {'memory-sink','DFF-safe','FDRE-safe','FDPE-unsafe'}:
        value['nets'][3]['cut_class']='combinational'
        if mode=='memory-sink':value['instances'][-1].update(type='RAMB18E2',resources={'bram18k':1})
        if mode=='DFF-safe':value['instances'][-1]['type']='$_DFF_P_'
        if mode in {'FDRE-safe','FDPE-unsafe'}:
            value['nets'][3]['sinks'][0]['port']='R'
        if mode=='FDPE-unsafe':value['instances'][-1]['type']='FDPE'
    if mode=='unicode':
        for net in value['nets']:net['id']+='雪é?#%'
    if mode=='wide-integer':value['instances'][1]['resources']['lut']=2**80
    path=tmp_path/'source.sqlite';write_compact_emuir(path,value)
    if mode=='sparse':
        with closing(sqlite3.connect(path)) as db,db:db.execute('UPDATE endpoint_records SET ordinal=ordinal*7+9')
    ir=load_partition_ir(path)
    expected=sorted(candidate_ids(EmuIR(value)))
    graph=candidate_ids(EmuIR(value))
    actual=build_native_candidate_ids(ir)
    assert set(actual)==set(expected)==set(graph)
    assert list(actual)==list(expected)
    assert [actual.contains_ordinal(i) for i in range(len(value['nets']))]==[net['id'] in graph for net in value['nets']]
    assert actual.implementation_id=='native-candidate-ids-v1'
    assert actual.native_metrics['wall_seconds']>0
    borrowed=actual.streaming_ordered_names(); root=Path(actual._ordinal_bits.owner.scratch.name)
    del actual;gc.collect()
    assert root.exists() and list(borrowed)==list(expected)
    del borrowed;gc.collect()
    assert not root.exists()


def test_existing_64_seed_graph_differential_uses_native_compact_branch(installed):
    original=reference._build_combinational_cut_candidate_index
    calls=[]
    def dispatch(ir,**kwargs):
        if not isinstance(ir,EmuIR):
            kwargs['_native_candidate_ids']=True
            calls.append(True)
        return original(ir,**kwargs)
    with patch.object(reference,'_build_combinational_cut_candidate_index',side_effect=dispatch):
        reference.CombinationalCutCharacterizationTest().test_compact_v2_candidate_index_randomized_differential()
    assert len(calls)==64


@pytest.mark.parametrize('sql',[
    "UPDATE endpoint_records SET instance_seq=999 WHERE net_seq=0 AND role=0",
    "UPDATE endpoint_records SET role=2 WHERE net_seq=0 AND role=0",
    "UPDATE endpoint_records SET bit=-1 WHERE net_seq=0 AND role=0",
    "UPDATE endpoint_records SET ordinal=-1 WHERE net_seq=0 AND role=0",
    "UPDATE pin_names SET name='' WHERE name='O'",
    "UPDATE pin_names SET name='missing_top' WHERE name='clk'",
    "UPDATE cell_records SET type='' WHERE seq=0",
    "UPDATE net_records SET cut_class='invalid' WHERE seq=0",
    "UPDATE net_records SET seq=999 WHERE seq=0",
    "UPDATE metadata SET value='emuflow.emuir/v999' WHERE key='logical_schema'",
    "UPDATE payloads SET value='[987654321]' WHERE seq=(SELECT resources_seq FROM cell_records LIMIT 1)",
    "UPDATE payloads SET value='{\"ff\":true}' WHERE seq=(SELECT resources_seq FROM cell_records LIMIT 1)",
    "UPDATE payloads SET value='{\"ff\":-1}' WHERE seq=(SELECT resources_seq FROM cell_records LIMIT 1)",
    "UPDATE payloads SET value='{\"mystery\":1}' WHERE seq=(SELECT resources_seq FROM cell_records LIMIT 1)",
    "ALTER TABLE cell_records RENAME TO original_cells; CREATE TABLE cell_records AS SELECT * FROM original_cells",
])
def test_malformed_sources_fail_closed(tmp_path,installed,sql):
    path=tmp_path/'source.sqlite';write_compact_emuir(path,reference._chain_ir().to_dict())
    ir=load_partition_ir(path)
    with closing(sqlite3.connect(path)) as db,db:db.executescript(sql)
    with pytest.raises(ValidationError,match='native candidate construction failed'):
        build_native_candidate_ids(ir)


@pytest.mark.parametrize('failure',['source-generation','truncated','version','oversize','alias'])
def test_request_failures_preserve_previous_output(tmp_path,installed,failure):
    path=tmp_path/'source.sqlite';write_compact_emuir(path,reference._chain_ir().to_dict())
    ir=load_partition_ir(path);request=tmp_path/'policy.bin';write_candidate_policy(request,ir)
    output=tmp_path/'output.bin';output.write_bytes(b'previous')
    if failure=='source-generation':
        with closing(sqlite3.connect(path)) as db,db:db.execute("UPDATE cell_records SET id=id||'x' WHERE seq=0")
    if failure=='truncated':request.write_bytes(request.read_bytes()[:-1])
    if failure=='version':
        data=bytearray(request.read_bytes());data[8]=9;request.write_bytes(data)
    if failure=='oversize':request.write_bytes(request.read_bytes()+bytes(65536))
    if failure=='alias':output=request
    previous=output.read_bytes()
    result=subprocess.run([str(emuflow_native()),'phase3','build-candidate-ids',str(path),str(request),str(output)],capture_output=True,text=True)
    assert result.returncode!=0
    assert output.read_bytes()==previous


def test_native_candidate_borrowing_cleans_exception_cycle(tmp_path,installed):
    path=tmp_path/'source.sqlite';write_compact_emuir(path,reference._chain_ir().to_dict());ir=load_partition_ir(path)
    errors=[]
    with patch('sys.unraisablehook',side_effect=lambda event:errors.append(event.exc_value)):
        def fail():
            ids=build_native_candidate_ids(ir);root=Path(ids._ordinal_bits.owner.scratch.name)
            try:raise ValueError(root)
            except ValueError as error:
                cycle=[error,ids];cycle.append(cycle);return root
        root=fail();gc.collect()
    assert not root.exists(),(errors,list(root.iterdir()) if root.exists() else [])
    assert not errors
