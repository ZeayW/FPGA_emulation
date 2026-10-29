"""Exact native projection and independent small-record differential checks."""
import hashlib
import json
from pathlib import Path
import random
from unittest import mock

import pytest
from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json,write_json
from emuflow.timing.sta_projection import project_sta_path_database
from emuflow.partition.phase3_storage import read_phase3_cut_timing_view
from tests.test_sta import _write_assignment_facts,_write_projection_database
from tests.sta_projection_reference import project_sta_path_database_value as oracle


def facts(seed=0):
    randomizer=random.Random(seed)
    nets=['a','a0','a1','cut','cut0','cut雪','cut🌐']
    cuts=[{'net':net,'source_fpgas':['fpga0'],'sink_fpgas':['fpga1','fpga2']} for net in nets]
    paths=[]
    for index in range(48):
        # Repeated signatures, equal required times and dominance tie-breaks.
        group=index%8
        selected=random.Random(seed*100+group).sample(nets,1+group%3)
        period=10. if group%2 else 20.
        delay=randomizer.choice([1.,2.,3.,4.,5.])
        required=4. if group%3 else 8.
        paths.append({'id':randomizer.choice(['a','a0','雪','🌐'])+f'-{index:03d}',
            'clock_domain':'clk' if group%2 else '时钟','clock_period_ns':period,
            'fixed_delay_ns':delay,'slack_ns':required-delay,'path_nets':['local']+selected})
    normalization={'positive_slack_scale_ns':max(row['slack_ns'] for row in paths),
        'negative_slack_scale_ns':max(1.,-min(row['slack_ns'] for row in paths)),
        'max_clock_period_ns':max(row['clock_period_ns'] for row in paths)}
    database={'schema':'emuflow.sta-path-database/v1','design':'projection',
        'source':{'provider':'opensta-fpga-path-database-v1'},'normalization':normalization,'paths':paths}
    assignment={'schema':'emuflow.partition-assignment/v1','design':'projection',
        'platform':'fixture','cut_nets':cuts}
    return database,assignment


def sources(root,seed=0):
    root.mkdir(parents=True,exist_ok=True)
    database,assignment=facts(seed)
    path,selected=root/'paths.json',root/'assignment.json'
    _write_projection_database(path,database);_write_assignment_facts(selected,assignment)
    return path,selected


@pytest.mark.parametrize('seed',range(16))
def test_original_members_order_dominance_and_ties_match_independent_oracle(tmp_path,seed):
    database,assignment=sources(tmp_path/'input',seed)
    expected,summary=oracle(read_json(database),read_phase3_cut_timing_view(assignment,include_instance_assignment=True),
        source_input_sha256=hashlib.sha256(database.read_bytes()).hexdigest(),
        output_path=str(tmp_path/'projected.json'),database_path=database)
    with mock.patch('emuflow.timing.sta.iter_sta_path_database_paths',side_effect=AssertionError('host path population')):
        observed=project_sta_path_database(database,assignment,tmp_path/'projected.json')
    assert observed==summary
    assert read_json(tmp_path/'projected.json')==expected
    assert sum(len(row['compressed_path_ids']) for row in expected['paths'])==48
    assert not list(tmp_path.glob('.sta-projection-*'))


@pytest.mark.parametrize('damage',['count','seal','normalization','duplicate-net','path-order','assignment-owner','assignment-seal','path-owner'])
def test_invalid_inputs_preserve_the_previous_public_generation(tmp_path,damage):
    import sqlite3
    database,assignment=sources(tmp_path/'input')
    manifest=read_json(database);store=database.parent/manifest['payloads']['paths']['path']
    if damage in {'duplicate-net','path-order'}:
        with sqlite3.connect(store) as db:
            if damage=='duplicate-net':db.execute("UPDATE path_nets SET net_id='local' WHERE path_ordinal=0 AND net_ordinal=1")
            else:db.execute('UPDATE path_nets SET net_ordinal=99 WHERE path_ordinal=0 AND net_ordinal=0')
        manifest['payloads']['paths']['sha256']=hashlib.sha256(store.read_bytes()).hexdigest()
    elif damage=='count':manifest['payloads']['paths']['records']+=1
    elif damage=='seal':manifest['payloads']['paths']['sha256']='0'*64
    elif damage=='normalization':manifest['normalization']['positive_slack_scale_ns']+=1
    elif damage=='path-owner':manifest['payloads']['paths']['path']='../'+store.name
    else:
        selected=json.loads(assignment.read_text())
        if damage=='assignment-owner':selected['cut_nets_index']['artifact']='../assignment.sqlite'
        else:selected['cut_nets_index']['sha256']='0'*64
        write_json(assignment,selected)
    write_json(database,manifest)
    out=tmp_path/'projected.json';out.write_text('previous-public-generation')
    with pytest.raises((ValidationError,FileNotFoundError)):
        project_sta_path_database(database,assignment,out)
    assert out.read_text()=='previous-public-generation'
    assert not list(tmp_path.glob('.sta-projection-*'))


def test_sealed_linked_path_owner_and_direct_assignment_store(tmp_path):
    database,assignment=sources(tmp_path/'input')
    manifest=read_json(database);store=database.parent/manifest['payloads']['paths']['path']
    original=tmp_path/'published-path-store.sqlite';store.rename(original);store.symlink_to(original)
    direct=assignment.with_name(json.loads(assignment.read_text())['cut_nets_index']['artifact'])
    selected=project_sta_path_database(database,assignment,tmp_path/'selected.json')
    raw=project_sta_path_database(database,direct,tmp_path/'direct.json')
    assert {k:v for k,v in selected.items() if k!='output'}=={k:v for k,v in raw.items() if k!='output'}
    assert read_json(tmp_path/'selected.json')==read_json(tmp_path/'direct.json')


def test_path_record_is_not_limited_by_control_document_node_budget(tmp_path):
    count=12000
    names=[f'cut{i:05d}' for i in range(count)]
    assignment={'schema':'emuflow.partition-assignment/v1','design':'long-path','platform':'fixture',
        'instance_assignment':{'launch':'fpga0','capture':'fpga0'},
        'cut_nets':[{'net':name,'source_fpgas':['fpga'+str(i%2)],'sink_fpgas':['fpga'+str(1-i%2)]}
            for i,name in enumerate(names)]}
    path={'id':'launch/Q->capture/D#00000000','clock_domain':'clk','clock_period_ns':10.,
        'fixed_delay_ns':4.,'slack_ns':1.,'path_nets':names,
        'startpoint':{'object':'launch/Q','instance':'launch','port':'Q','bit':0},
        'endpoint':{'object':'capture/D','instance':'capture','port':'D','bit':0}}
    database={'schema':'emuflow.sta-path-database/v1','design':'long-path','paths':[path],
        'source':{'provider':'opensta-fpga-path-database-v1'},
        'normalization':{'positive_slack_scale_ns':1.,'negative_slack_scale_ns':1.,'max_clock_period_ns':10.}}
    selected,source=tmp_path/'assignment.json',tmp_path/'paths.json'
    _write_assignment_facts(selected,assignment);_write_projection_database(source,database)
    report=project_sta_path_database(source,selected,tmp_path/'projected.json')
    result=read_json(tmp_path/'projected.json')['paths'][0]
    assert report['covered_cut_nets']==count and report['uncovered_cut_nets']==0
    assert result['cut_nets']==names and len(result['cut_transitions'])==count
    assert result['cut_transitions'][0]=={'net':names[0],'from':'fpga0','to':'fpga1'}
    assert result['cut_transitions'][-1]=={'net':names[-1],'from':'fpga1','to':'fpga0'}
    assert result['compressed_path_ids']==[path['id']]
