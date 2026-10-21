"""A native result resumes validation, never substitutes for a Phase3 pass."""
import copy
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.io import write_json
from emuflow.patron_result import (file_record, recover_initial_mapping,
    seal_completed_result, validate_completed_result, validate_context)
from emuflow.partition_pressure import _write_patron_native_input, run_partition_pressure_native
from tests.native_build import patron_refiner
from tests import test_partition_pressure as fixtures

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def result(tmp_path):
    f = fixtures.PartitionPressureTest(); f.setUp()
    source, output = tmp_path/'patron.in', tmp_path/'patron.out'
    indexes = _write_patron_native_input(source,f.platform,f.clusters,f.constraints,
        f.route_constraints,f.model,f.initial,len(f.model['clusters']),9)
    binary = patron_refiner().resolve()
    command = [str(binary),'--summary-output',str(source),str(output)]
    subprocess.run(command,check=True,capture_output=True)
    receipt = tmp_path/'process.json'
    write_json(receipt,{'status':'pass','exit_code':0,'command':command})
    commit = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    native_source = ROOT/'src/native/patron_refiner.cpp'
    manifest = seal_completed_result(native_input=source,native_output=output,binary=binary,
        native_source=native_source,receipt=receipt,producer_commit=commit,
        clusters=indexes['clusters'],parts=indexes['parts'],context={'fixture':source})
    path = tmp_path/'completed.json';write_json(path,manifest)
    return f,path,manifest


def test_matching_result_skips_native_and_preserves_assignment(result):
    f,path,value=result
    actual_run = subprocess.run
    def no_native(command,*args,**kwargs):
        assert command[0] == 'git', 'native computation must not run'
        return actual_run(command,*args,**kwargs)
    expected,_=run_partition_pressure_native(f.ir,f.platform,f.clusters,f.constraints,
        f.route_constraints,f.model,f.initial,executable=str(patron_refiner()),
        algorithm_version=9,retain_trace_seals=False)
    with patch('emuflow.partition_pressure.subprocess.run',side_effect=no_native):
        assignment,trace=run_partition_pressure_native(f.ir,f.platform,f.clusters,f.constraints,
            f.route_constraints,f.model,f.initial,completed_result_path=path,
            algorithm_version=9,retain_trace_seals=False)
    assert assignment==expected
    assert trace['native_result_provenance']['producer_commit']==value['producer_commit']
    assert trace['trace_storage']=='summary-only'
    assert recover_initial_mapping(value,[c['id'] for c in f.clusters['clusters']],
                                   [p.id for p in f.platform.fpgas])==f.initial['cluster_assignment']


@pytest.mark.parametrize('change',['exit','deadline','output','binary','source','mapping','command'])
def test_rejects_unfinished_or_wrong_identity(result,change):
    f,path,value=result
    value=copy.deepcopy(value)
    if change=='exit':value['execution']['exit_code']=-15
    elif change=='deadline':value['execution']['termination_reason']='deadline'
    elif change in {'output','binary','source'}:
        value[{'source':'native_source'}.get(change,change)]['sha256']='0'*64
    elif change=='mapping':value['mapping']['clusters_sha256']='0'*64
    else:value['execution']['command'][-1]='/wrong/output'
    write_json(path,value)
    with pytest.raises(ValidationError):
        run_partition_pressure_native(f.ir,f.platform,f.clusters,f.constraints,
            f.route_constraints,f.model,f.initial,completed_result_path=path,
            algorithm_version=9,retain_trace_seals=False)


def test_changed_full_input_and_context_fail_closed(result):
    f,path,value=result
    changed=copy.deepcopy(f.route_constraints);changed['frame_slots']+=1
    with pytest.raises(ValidationError,match='regenerated native input differs'):
        run_partition_pressure_native(f.ir,f.platform,f.clusters,f.constraints,
            changed,f.model,f.initial,completed_result_path=path,
            algorithm_version=9,retain_trace_seals=False,max_moves=1)
    with pytest.raises(ValidationError,match='context differs'):
        validate_context(value,fixture=path)


def test_phase3_continues_static_exact_without_repeating_initializer(result,tmp_path):
    from emuflow.partition import build_clusters, build_partition_assignment
    from emuflow.partition_pressure import build_partition_pressure_model
    from emuflow.phase3 import run_phase3
    f,path,value=result
    f.clusters=build_clusters(f.ir,f.constraints,cut_mode='static-exact-combinational')
    f.initial=build_partition_assignment(f.ir,f.platform,f.clusters,f.constraints,
        {c['id']:('a' if c['instances'][0] in {'u0','u2'} else 'b') for c in f.clusters['clusters']},
        provider='fixture',seed=7)
    f.model=build_partition_pressure_model(f.ir,f.platform,f.clusters,f.constraints,f.timing,f.route_constraints)
    source,output=Path(value['input']['path']),Path(value['output']['path'])
    indexes=_write_patron_native_input(source,f.platform,f.clusters,f.constraints,
        f.route_constraints,f.model,f.initial,len(f.model['clusters']),14)
    subprocess.run(value['execution']['command'],capture_output=True,check=True)
    paths={key:tmp_path/(key+'.json') for key in ('ir','platform','timing','constraints','routes')}
    for key,data in [('ir',f.ir.value),('platform',f.platform.to_dict()),('timing',f.timing),
                     ('constraints',f.constraints),('routes',f.route_constraints)]:
        write_json(paths[key],data)
    value=seal_completed_result(native_input=source,native_output=output,
        binary=Path(value['binary']['path']),native_source=Path(value['native_source']['path']),
        receipt=Path(value['receipt']['path']),producer_commit=value['producer_commit'],
        clusters=indexes['clusters'],parts=indexes['parts'],
        context={key:paths[key] for key in ('ir','platform','timing')})
    from emuflow.phase3_storage import compress_phase3_clusters
    recovered = tmp_path/'recovered-clusters.json'
    write_json(recovered,compress_phase3_clusters(f.clusters))
    value['context']['clusters']=file_record(recovered)
    write_json(path,value)
    with patch('emuflow.phase3.build_clusters',side_effect=AssertionError('clusters rebuilt twice')), \
         patch('emuflow.phase3.run_tritonpart',side_effect=AssertionError('seed repeated')), \
         patch('emuflow.phase3.refine_partition_hops',side_effect=AssertionError('hop seed repeated')):
        report=run_phase3(paths['ir'],paths['platform'],tmp_path/'phase3',
            constraints_path=paths['constraints'],route_constraints_path=paths['routes'],
            timing_database_path=paths['timing'],provider='patron',patron_algorithm_version=14,
            patron_completed_result=path,patron_initial_clusters_path=recovered,
            cut_mode='static-exact-combinational',seed=7)
    assert report['status']=='pass'
    assert report['qualification']=='structural-partition-legality'
    assert report['algorithm_validation']['native_result_provenance']['producer_commit']==value['producer_commit']
    assert report['artifacts']['semantic_contract']=='assignment.json#/semantic_contract'


def test_changed_clustering_dependency_cannot_reinterpret_native_ids(result):
    _,path,_=result
    original=Path.read_bytes
    def changed(file):
        data=original(file)
        return data+b'\nraise RuntimeError("changed clustering")\n' if file==ROOT/'src/emuflow/partition.py' else data
    with patch.object(Path,'read_bytes',changed), pytest.raises(ValidationError,match='mapping producer dependency changed'):
        validate_completed_result(path)


def test_receipt_and_truncated_output_are_not_phase3_results(result):
    f,path,value=result
    receipt=Path(value['receipt']['path'])
    data=json.loads(receipt.read_text());data['status']='running';write_json(receipt,data)
    value['receipt']=file_record(receipt);write_json(path,value)
    with pytest.raises(ValidationError,match='partial or terminated'):
        validate_completed_result(path)
    data['status']='pass';write_json(receipt,data);value['receipt']=file_record(receipt)
    output=Path(value['output']['path']);output.write_text('EMUFLOW_PATRON_OUTPUT_V9\n')
    value['output']=file_record(output);write_json(path,value)
    with pytest.raises(ValidationError):
        run_partition_pressure_native(f.ir,f.platform,f.clusters,f.constraints,
            f.route_constraints,f.model,f.initial,completed_result_path=path,
            algorithm_version=9,retain_trace_seals=False)
