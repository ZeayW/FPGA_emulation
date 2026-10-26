"""Complete initializer parity and rejection; real-design acceptance is separate."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
from unittest.mock import patch

import pytest

from emuflow.partition import build_clusters, CUT_MODE_STATIC_EXACT
from emuflow.partition_init_native import initialize
from emuflow.tritonpart import export_tritonpart_inputs, run_tritonpart, _repair_min_used_fpgas, _stream_assignment_cut_summary
from tests.test_phase3_clusters_native import source
from tests import test_partition_pressure as fixture


def prepare(source,**overrides):
    ir,constraints=source
    f=fixture.PartitionPressureTest();f.setUp()
    constraints=dict(constraints,balance_tolerance=1.0)
    constraints.update(overrides)
    clusters=build_clusters(ir,constraints,cut_mode=CUT_MODE_STATIC_EXACT,_native_compact_clusters=True)
    return ir,f.platform,constraints,clusters


def solution(tmp_path,count,parts):
    path=tmp_path/'solution.txt';path.write_text('\n'.join(str(parts[i%len(parts)]) for i in range(count))+'\n');return path


def weights(tmp_path,rows):
    path=tmp_path/'weights.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA user_version=1')
        db.executescript((Path(__file__).parents[1]/'src/emuflow/data/schema/sta_net_weights.sql').read_text())
        db.executemany('INSERT INTO net_weights VALUES(?,?,?,?)',rows)
    selector=tmp_path/'weights.json';selector.write_text(json.dumps({'schema':'emuflow.partition-net-weights/v2','payloads':{'weights':{'format':'sqlite-net-weights/v1','path':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'records':len(rows)}}}))
    return selector


def engine(tmp_path,assignments,*,fail=False,mutation=None):
    target=tmp_path/'capture';target.mkdir()
    script=tmp_path/'openroad-fixture'
    script.write_text('#!'+sys.executable+'\n'+'''import pathlib,re,sys,json,shutil
s=pathlib.Path(sys.argv[-1]).read_text();seed=int(re.search(r'-seed (\\d+)',s)[1]);g=pathlib.Path(re.search(r'-hypergraph_file \\{([^}]+)\\}',s)[1]);baseline='unweighted' in g.name
for name in ['partition.hgr','partition.unweighted.hgr','partition.fix','partition.resource-bounds','run_tritonpart.tcl']:
 p=g.parent/name
 if p.exists():shutil.copyfile(p,CAPTURE+'/'+str(seed)+'-'+('b' if baseline else 'w')+'-'+name)
values=ASSIGNMENTS[(1 if baseline else seed)%len(ASSIGNMENTS)]
count=int(g.read_text().splitlines()[0].split()[1]);pathlib.Path(str(g)+'.part.2').write_text('\\n'.join(str(values[i%len(values)]) for i in range(count))+'\\n')
MUTATION
raise SystemExit(7 if FAIL else 0)
'''.replace('CAPTURE',repr(str(target))).replace('ASSIGNMENTS',repr(assignments)).replace('MUTATION',mutation or '').replace('FAIL',repr(fail)))
    script.chmod(script.stat().st_mode|stat.S_IXUSR)
    return script,target


def call(args,**kwargs):
    ir,platform,constraints,clusters=args
    return initialize(ir,platform,clusters,constraints,seed=0,**dict({'executable':None},**kwargs))


@pytest.mark.parametrize('parts',[(0,1),(1,0),(0,)])
def test_imported_initial_owner_matches_reference_objective_repair_and_bounds(source,tmp_path,parts):
    args=prepare(source);ir,platform,constraints,clusters=args
    imported=solution(tmp_path,len(clusters['clusters']),parts)
    ref=export_tritonpart_inputs(ir,platform,clusters,constraints,tmp_path/'reference',write_manifest=False)
    raw={f'c{i:06d}':ref['fpga_order'][parts[i%len(parts)]] for i in range(len(clusters['clusters']))}
    expected,moves=_repair_min_used_fpgas(raw,clusters,platform,constraints,ref['hyperedges']) if len(set(raw.values()))<constraints['min_used_fpgas'] else (raw,[])
    with patch('emuflow.tritonpart._compact_legal_hyperedges',side_effect=AssertionError('Python graph reconstructed')),patch('emuflow.tritonpart.parse_tritonpart_solution',side_effect=AssertionError('Python parsed owner')):
        actual,summary=call(args,solution_input=imported)
    assert dict(actual)==expected
    assert actual.part_indices.readonly
    assert summary['balance_allowed_loads']==ref['balance_allowed_loads']
    assert summary['provider_metadata']['min_used_fpgas_repair']['moves']==moves
    score=_stream_assignment_cut_summary(ref['hyperedges'],actual,set())
    assert summary['provider_metadata']['seed_attempts'][-1]['cut_weight']==score['cut_weight']


@pytest.mark.parametrize('weighted,baseline',[(False,False),(True,False),(True,True)])
def test_full_engine_handoff_matches_reference_input_bytes_and_selected_sweep(source,tmp_path,weighted,baseline):
    args=prepare(source);ir,platform,constraints,clusters=args
    rows=[('critical',.5,1,7.123456789123),('relaxed',0.0,1,1.0)]
    selector=weights(tmp_path,rows) if weighted else None
    ref=export_tritonpart_inputs(ir,platform,clusters,constraints,tmp_path/'reference',net_weights={'critical':rows[0][3]} if weighted else None,write_manifest=False)
    tool,capture=engine(tmp_path,[(0,1),(0,0,1,1)])
    actual,summary=call(args,weights_path=selector,executable=str(tool),seed_attempts=2,run_unweighted_baseline=baseline)
    for name in ['partition.hgr','partition.fix','partition.resource-bounds']:
        assert (capture/('0-w-'+name)).read_bytes()==(tmp_path/'reference'/name).read_bytes()
    script=(capture/'0-w-run_tritonpart.tcl').read_text();reference_script=(tmp_path/'reference/run_tritonpart.tcl').read_text()
    # Only the invocation-owned absolute artifact directory differs.
    import re
    assert re.sub(r'\{[^{}]*partition\.', '{ROOT/partition.',script)==re.sub(r'\{[^{}]*partition\.', '{ROOT/partition.',reference_script)
    if baseline:assert (capture/'0-b-partition.unweighted.hgr').read_bytes()==(tmp_path/'reference/partition.unweighted.hgr').read_bytes()
    else:assert not list(capture.glob('*partition.unweighted.hgr'))
    objectives=[]
    for seed,indices in enumerate([(0,1),(0,0,1,1)]):
        candidate={f'c{i:06d}':ref['fpga_order'][indices[i%len(indices)]] for i in range(len(clusters['clusters']))}
        score=_stream_assignment_cut_summary(ref['hyperedges'],candidate,{'critical'} if weighted else set())
        objectives.append(((score['cut_weight'],score['cut_hyperedges'],seed),candidate))
    if baseline:objectives.append((objectives[1][0][0:2]+(0,),objectives[1][1]))
    expected=min(objectives,key=lambda row:row[0])
    assert dict(actual)==expected[1]
    assert summary['selected_seed']==expected[0][2]


@pytest.mark.parametrize('damage',['invalid-token','missing-owner','unknown-part','engine-exit','changed-source','unknown-weight','weight-count','weight-seal'])
def test_invalid_sources_or_results_never_produce_a_selected_owner(source,tmp_path,damage):
    args=prepare(source);ir,platform,constraints,clusters=args
    imported=solution(tmp_path,len(clusters['clusters']),(0,1));options={'solution_input':imported}
    if damage=='invalid-token':imported.write_text('bad\n')
    elif damage=='missing-owner':imported.write_text('0\n')
    elif damage=='unknown-part':imported.write_text('9\n'*len(clusters['clusters']))
    elif damage in {'engine-exit','changed-source'}:
        mutation='pathlib.Path('+repr(str(ir.path))+').touch()' if damage=='changed-source' else None
        tool,_=engine(tmp_path,[(0,1)],fail=damage=='engine-exit',mutation=mutation)
        options={'executable':str(tool)}
    else:
        selector=weights(tmp_path,[('missing' if damage=='unknown-weight' else 'critical',.5,1,2.0)])
        body=json.loads(selector.read_text())
        if damage=='weight-count':body['payloads']['weights']['records']+=1
        if damage=='weight-seal':body['payloads']['weights']['sha256']='0'*64
        selector.write_text(json.dumps(body));options['weights_path']=selector
    with pytest.raises(Exception):call(args,**options)


def test_production_tritonpart_never_loads_the_weight_population_or_expands_primary(source,tmp_path):
    args=prepare(source);ir,platform,constraints,clusters=args
    imported=solution(tmp_path,len(clusters['clusters']),(0,1));selector=weights(tmp_path,[('critical',.5,1,3.0)])
    with patch('emuflow.tritonpart.load_partition_net_weights',side_effect=AssertionError('Python net-weight map loaded')),patch('emuflow.tritonpart.export_tritonpart_inputs',side_effect=AssertionError('Python initializer entered')),patch.object(type(clusters['clusters']),'_record',side_effect=AssertionError('primary records expanded')):
        actual=run_tritonpart(ir,platform,clusters,constraints,tmp_path/'unused',seed=0,solution_input=imported,net_weights_path=selector,persist_input_manifest=False,defer_semantic_contract=True,_defer_derived_records=True)
    assert actual['provider_metadata']['initializer_implementation_id']=='native-tritonpart-initialization-v1'
    assert actual['cluster_assignment'].part_indices.readonly


def test_occupancy_repair_never_rescues_a_strictly_unbalanced_raw_solution(source,tmp_path):
    args=prepare(source,balance_tolerance=0.0,balance_dimensions=['cells','lut'])
    imported=solution(tmp_path,len(args[3]['clusters']),(0,))
    with pytest.raises(Exception,match='no independently acceptable assignment'):
        call(args,solution_input=imported,repair_min_used_fpgas=True)


from tests.test_phase3_pressure_native import inputs as pressure_inputs


def test_pressure_model_borrows_the_native_initial_owner_without_republishing_it(pressure_inputs,tmp_path):
    from emuflow.partition import build_partition_assignment
    from emuflow.partition_pressure import build_partition_pressure_model
    from emuflow.phase3_pressure_native import NativePressureModel
    args,reference_initial,timing=pressure_inputs
    ir,platform,clusters,constraints,manifest,routes=args
    imported=solution(tmp_path,len(clusters['clusters']),(0,1))
    actual,summary=initialize(ir,platform,clusters,constraints,seed=0,executable=None,solution_input=imported)
    initial=build_partition_assignment(ir,platform,clusters,constraints,actual,'fixture',0,_include_semantic_contract=False,_stream_semantic_records=True,_defer_derived_records=True)
    model=build_partition_pressure_model(*args,independent_validation=False,timing_database_path=timing,native_store_model=True)
    request_root=tmp_path/'requests';request_root.mkdir()
    with patch.object(NativePressureModel,'_write_initial_ownership',side_effect=AssertionError('initial ownership copied back through Python')):
        columns,board=model.write_requests(request_root,initial,0)
    assert columns==actual.native_initial_source.path
    assert not (request_root/'initial.bin').exists()
    assert board.is_file()
    # The same original file remains live after its returned summary is gone.
    del summary
    assert columns.is_file()


def test_borrowed_initializer_rejects_replaced_python_part_columns(pressure_inputs,tmp_path):
    from array import array
    from emuflow.partition import build_partition_assignment
    from emuflow.partition_pressure import build_partition_pressure_model
    args,_,timing=pressure_inputs;ir,platform,clusters,constraints,_,_=args
    actual,_=initialize(ir,platform,clusters,constraints,seed=0,executable=None,solution_input=solution(tmp_path,len(clusters['clusters']),(0,1)))
    initial=build_partition_assignment(ir,platform,clusters,constraints,actual,'fixture',0,_include_semantic_contract=False,_stream_semantic_records=True,_defer_derived_records=True)
    actual.part_indices=array('I',actual.part_indices)
    model=build_partition_pressure_model(*args,independent_validation=False,timing_database_path=timing,native_store_model=True)
    request_root=tmp_path/'requests';request_root.mkdir()
    with pytest.raises(Exception,match='another pressure model'):model.write_requests(request_root,initial,0)


def test_native_initializer_runs_through_patron_without_host_ownership_rewrite(pressure_inputs,tmp_path):
    from emuflow.partition import build_partition_assignment, _PackedClusterAssignment
    from emuflow.partition_pressure import build_partition_pressure_model
    from emuflow.phase3_pressure_native import NativePressureModel
    from tests.test_phase3_pressure_native import run
    args,_,timing=pressure_inputs
    ir,platform,clusters,constraints,_,_=args
    owners,_=initialize(ir,platform,clusters,constraints,seed=0,executable=None,
        solution_input=solution(tmp_path,len(clusters['clusters']),(0,1)))
    copied=_PackedClusterAssignment(owners.part_ids,owners.part_indices)
    assert owners==copied and copied==owners
    changed=_PackedClusterAssignment(owners.part_ids,((p+1)%len(owners.part_ids) for p in owners.part_indices))
    assert owners!=changed and changed!=owners
    def initial(column):
        return build_partition_assignment(ir,platform,clusters,constraints,column,'fixture',0,
            _include_semantic_contract=False,_stream_semantic_records=True,_defer_derived_records=True)
    def model():
        return build_partition_pressure_model(*args,independent_validation=False,
            timing_database_path=timing,native_store_model=True)
    expected,expected_trace=run(args,model(),initial(copied),max_moves=2)
    with patch.object(NativePressureModel,'_write_initial_ownership',side_effect=AssertionError('native ownership rewritten in host')):
        actual,trace=run(args,model(),initial(owners),max_moves=2)
    assert actual['cluster_assignment']==expected['cluster_assignment']
    assert trace['initial_metrics']==expected_trace['initial_metrics']
    assert trace['final_metrics']==expected_trace['final_metrics']
    assert owners.native_initial_source.path.is_file()


@pytest.mark.skipif(not os.environ.get('EMUFLOW_TEST_OPENROAD'),reason='qualified OpenROAD runtime is not configured')
def test_real_tritonpart_initializer_reaches_final_phase3_publication(pressure_inputs,tmp_path):
    from emuflow.io import write_json
    from emuflow.phase3 import run_phase3, validate_phase3_indexed
    from emuflow.phase3_pressure_native import NativePressureModel
    args,_,timing=pressure_inputs
    platform=tmp_path/'platform.json';write_json(platform,args[1].to_dict())
    routes=tmp_path/'routes.json';write_json(routes,args[5])
    with patch('emuflow.tritonpart.export_tritonpart_inputs',side_effect=AssertionError('Python initializer')),patch.object(NativePressureModel,'_write_initial_ownership',side_effect=AssertionError('Python ownership rewrite')):
        report=run_phase3(args[0].path,platform,tmp_path/'phase3-real-engine',provider='patron',
            min_used_fpgas=2,balance_tolerance=1.0,patron_max_moves=2,
            openroad=os.environ['EMUFLOW_TEST_OPENROAD'],tritonpart_timeout_seconds=120,
            tritonpart_num_initial_solutions=3,tritonpart_num_best_initial_solutions=1,
            route_constraints_path=routes,timing_database_path=timing)
    assert report['status']=='pass'
    assert report['algorithm_validation']['native_input_transport']=='native-primary-store-model-v2'
    assert (tmp_path/'phase3-real-engine/assignment.sqlite').is_file()
    assert not (tmp_path/'phase3-real-engine/patron').exists()
    assert validate_phase3_indexed(args[0].path,platform,
        tmp_path/'phase3-real-engine/phase3_report.json',
        tmp_path/'phase3-real-engine/assignment.sqlite')['status']=='pass'
