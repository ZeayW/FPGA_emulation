"""Selected typed records, explicit chain facts and independent rejection gates."""
import copy
from contextlib import closing
import gc
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from emuflow.emuir_compact import write_compact_emuir
from emuflow.errors import ValidationError
from emuflow.partition import (CUT_MODE_STATIC_EXACT)
from tests.native_partition_facts import (build_clusters, build_partition_assignment, normalize_partition_constraints)
from emuflow.partition_ir import load_partition_ir
from tests.native_partition_kernel_driver import initialize
from tests.phase3_store_fixture import write_assignment_index_fixture
from tests.native_assignment_publication_fixture import publish_native_assignment_descriptor
from emuflow.platform import Platform
from tests.native_build import emuflow_native
from tests.test_combinational_cut import _chain_ir,_reconvergent_ir,_wide_fanout_ir,PLATFORM_PATH


@pytest.fixture(params=['chain','reconvergent','fanout','empty-cuts','reordered','sparse','unicode-wide-bit'])
def selected(request,tmp_path,monkeypatch):
    monkeypatch.setenv('EMUFLOW_NATIVE_ROOT',str(emuflow_native().parent.parent))
    mode=request.param
    value=copy.deepcopy((_reconvergent_ir() if mode=='reconvergent' else
        _wide_fanout_ir(8) if mode=='fanout' else _chain_ir()).value)
    rename={cell['id']:cell['id'] for cell in value['instances']}
    if mode=='unicode-wide-bit':
        rename={name:'实例/'+name+'🙂' for name in rename}
        for cell in value['instances']:cell['id']=rename[cell['id']]
        for net in value['nets']:
            old=net['id'];net['id']='网络/'+old+'λ';net['name']=net['id']
            for endpoint in [*net['drivers'],*net['sinks']]:
                if endpoint['instance'] is not None:endpoint['instance']=rename[endpoint['instance']]
            if old=='d':net['sinks'][0]['bit']=8192
    if mode=='reordered':value['instances'].reverse();value['nets'].reverse()
    path=tmp_path/'source.sqlite';write_compact_emuir(path,value)
    if mode=='sparse':
        with closing(sqlite3.connect(path)) as db,db:db.execute('UPDATE endpoint_records SET ordinal=ordinal*11+7')
    ir=load_partition_ir(path);platform=Platform.load(PLATFORM_PATH)
    constraints=normalize_partition_constraints({'schema':'emuflow.partition-constraints/v1',
        'balance_tolerance':1.0,'min_used_fpgas':1},ir,platform)
    clusters=build_clusters(ir,constraints,cut_mode=CUT_MODE_STATIC_EXACT,
        max_cross_fpga_dependency_depth=0,_native_compact_clusters=True,_stream_eligible_policy_names=True)
    parts=tuple(fpga.id for fpga in platform.fpgas)
    targets={}
    for index,cluster in enumerate(clusters['clusters']):
        if mode=='empty-cuts':part=0
        elif mode in {'chain','reordered','sparse','unicode-wide-bit'}:part=int(rename['l1'] in cluster['instances'])
        else:part=index%len(parts)
        targets[cluster['id']]=parts[part]
    solution=tmp_path/'owner.part';solution.write_text('\n'.join(str(parts.index(targets[c['id']])) for c in clusters['clusters'])+'\n')
    owners,_=initialize(ir,platform,clusters,constraints,seed=0,executable=None,solution_input=solution)
    expected=build_partition_assignment(ir,platform,clusters,constraints,dict(owners),'fixture',0)
    # Algorithm facts are fixed independently of the output-projection checks.
    if mode in {'chain','reordered','sparse'}:
        nodes=expected['semantic_contract']['cut_nodes']
        assert [(n['net'],n['source_fpgas'],n['sink_fpgas'],n['dependency_level'],n['combinational_dependency_depth'],n['predecessor_cut_nets']) for n in nodes]==[
            ('n0',['fpga0'],['fpga1'],1,1,[]),('n1',['fpga1'],['fpga0'],2,2,['n0'])]
        assert [(c['cut_net'],c['fpga'],c['endpoint'],c.get('port'),c.get('bit')) for c in expected['semantic_contract']['capture_requirements']]==[('n1','fpga0','q1','D',0)]
    if mode=='empty-cuts':
        assert not expected['semantic_contract']['cut_nodes']
        assert not expected['semantic_contract']['capture_requirements']
    yield ir,platform,clusters,constraints,owners,expected
    gc.collect()


def build(selected):
    ir,platform,clusters,constraints,owners,_=selected
    return build_partition_assignment(ir,platform,clusters,constraints,owners,'fixture',0,_stream_semantic_records=True)


def test_complete_selected_semantic_generation_and_publication_match_explicit_projection(selected,tmp_path):
    from tests.partition_views import InstanceOwners as _CompactInstanceAssignment
    import emuflow.combinational_cut as retired_semantics
    assert not hasattr(retired_semantics,'build_static_exact_semantic_contract')
    with (patch('tests.native_partition_facts.compute_cut_nets',side_effect=AssertionError('host endpoint projection')),
          patch.object(_CompactInstanceAssignment,'canonical_sha256',side_effect=AssertionError('host instance hash'))):
        actual=build(selected)
    ir,_,_,_,_,expected=selected
    assert actual['metrics']==expected['metrics']
    assert list(actual['cut_nets'])==list(expected['cut_nets'])
    arrays={'cut_nodes','dependency_edges','logic_segments','capture_requirements'}
    assert {key:value for key,value in actual['semantic_contract'].items() if key not in arrays}=={key:value for key,value in expected['semantic_contract'].items() if key not in arrays}
    for key in arrays:assert list(actual['semantic_contract'][key])==list(expected['semantic_contract'][key])
    assert actual['semantic_contract_sha256']==expected['semantic_contract_sha256']
    old=tmp_path/'reference/assignment.sqlite';new=tmp_path/'native/assignment.sqlite'
    write_assignment_index_fixture(old,ir,expected)
    from emuflow.native_protocol import CheckedRequestWriter
    original_bytes=CheckedRequestWriter.bytes
    def bounded_bytes(writer,value):
        if value is actual['instance_assignment']._cluster_parts or value is actual['cut_nets']._cuts._sink_endpoints:
            raise AssertionError('host column serialization')
        return original_bytes(writer,value)
    with (patch('emuflow.phase3_assignment_index._normalized_cut_record',side_effect=AssertionError('host cut serialization')),
          patch.object(CheckedRequestWriter,'bytes',bounded_bytes)):
        summary=write_assignment_index_fixture(new,ir,actual)
    assert summary['native_writer_metrics']['host_ownership_records']==0
    assert old.with_name('assignment.exact.bin').read_bytes()==new.with_name('assignment.exact.bin').read_bytes()
    with closing(sqlite3.connect(old)) as a,closing(sqlite3.connect(new)) as b:
        for table in ('copies','cuts','cut_sinks','cut_predecessors'):
            columns=len(a.execute('PRAGMA table_info('+table+')').fetchall())
            order=','.join(str(index+1) for index in range(columns))
            assert a.execute('SELECT * FROM '+table+' ORDER BY '+order).fetchall()==b.execute('SELECT * FROM '+table+' ORDER BY '+order).fetchall()
    from tests.phase3_store_fixture import assignment_selector_fixture
    from emuflow.io import write_json
    expected_selector=assignment_selector_fixture(expected,selected[2],cut_nets_index={
        'artifact':'assignment.sqlite','schema':'emuflow.phase3-cut-records/v1',
        'records':summary['cut_records'],'sha256':summary['cut_records_sha256']})
    reference=old.with_name('assignment.json');write_json(reference,expected_selector,compact=True)
    with (patch('tests.partition_views.InstanceOwners.__iter__',side_effect=AssertionError('host instance population'))):
        metrics=publish_native_assignment_descriptor(new.with_name('assignment.json'),ir,actual,selected[2],summary)
    assert metrics['request_bytes']<32768
    assert reference.read_bytes()==new.with_name('assignment.json').read_bytes()


@pytest.mark.parametrize('invalid',['cut-seal','source-copy','partition-count'])
def test_invalid_final_descriptor_preserves_previous_selector(selected,tmp_path,invalid):
    actual=build(selected);index=tmp_path/'assignment.sqlite'
    summary=write_assignment_index_fixture(index,selected[0],actual)
    selector=tmp_path/'assignment.json';selector.write_bytes(b'previous-selector')
    if invalid=='cut-seal':summary['cut_records_sha256']='0'*64
    elif invalid=='partition-count':actual['partitions'][0]['instance_count']+=1
    else:
        with closing(sqlite3.connect(index)) as db,db:
            db.execute("UPDATE copies SET copy_id='wrong-source-instance' WHERE source_instance_seq=0")
    with pytest.raises(ValidationError):
        publish_native_assignment_descriptor(selector,selected[0],actual,selected[2],summary)
    assert selector.read_bytes()==b'previous-selector'


def test_native_fact_leases_retire_scratch_and_reject_later_publication(selected):
    actual=build(selected);nodes=actual['semantic_contract']['cut_nodes'];captures=actual['semantic_contract']['capture_requirements']
    counts=actual['cut_nets']._cuts._sink_endpoints;root=counts.native_source.path.parent.parent
    assert root.exists();nodes.close();assert root.exists();captures.close();assert root.exists()
    counts.owner.close();assert not root.exists()
    from tests.native_assignment_publication_fixture import native_assignment_cut_source
    with pytest.raises((ValidationError, OSError)): native_assignment_cut_source(actual)


def test_changed_count_generation_preserves_the_previous_index_and_binary(selected,tmp_path):
    actual=build(selected);counts=actual['cut_nets']._cuts._sink_endpoints
    path=counts.native_source.path;path.chmod(path.stat().st_mode^0o100)
    target=tmp_path/'preserved/assignment.sqlite';target.parent.mkdir()
    target.write_bytes(b'previous-index');binary=target.with_name('assignment.exact.bin');binary.write_bytes(b'previous-binary')
    with pytest.raises(ValidationError):write_assignment_index_fixture(target,selected[0],actual)
    assert target.read_bytes()==b'previous-index' and binary.read_bytes()==b'previous-binary'
    assert not list(target.parent.glob('.*.tmp*'))


def test_depth_policy_is_enforced_by_the_native_final_owner(selected):
    from tests.partition_views import InstanceOwners as _CompactInstanceAssignment
    from tests.native_partition_facts import finalize_selected_semantics
    ir,platform,clusters,_,owners,expected=selected
    if expected['semantic_contract']['metrics']['maximum_combinational_dependency_depth']<=1:return
    instance=_CompactInstanceAssignment(ir.path,clusters['clusters'],owners)
    with pytest.raises(ValidationError,match='depth exceeds'):
        finalize_selected_semantics(ir,platform,clusters['clusters'],owners,instance,
            max_dependency_depth=1,candidate_selection_policy=clusters['policy']['candidate_selection_policy'])
