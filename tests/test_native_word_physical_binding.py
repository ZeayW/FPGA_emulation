"""Canonical indexed owners -> physical word fanins (no Python timing graph)."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from emuflow.boundary_identity_storage import write_boundary_identity_storage
from emuflow.boundary_timing_storage import write_boundary_timing_storage
from emuflow.emuir_compact import write_compact_emuir
from emuflow.logic_segment_timing_storage import write_logic_segment_timing_storage
from emuflow.phase3_exact_binary import write_phase3_exact_binary
from emuflow.sampling_cost import SAMPLING_COST_POLICY
from emuflow.yosys import import_yosys_json
from tests.native_build import ROOT,word_physical_binding_test
from tests.test_phase3_exact_binary import _contract


def database(path,appid,version,ddl,metadata):
    with closing(sqlite3.connect(path))as db,db:
        db.executescript(f'PRAGMA application_id={appid}; PRAGMA user_version={version};'+ddl)
        db.executemany('INSERT INTO metadata VALUES(?,?)',((k,json.dumps(v))for k,v in metadata.items()))


@pytest.fixture
def physical(tmp_path):
    # This fixture intentionally constructs published owner formats directly;
    # it qualifies the join, not partition/schedule search or physical QoR.
    ir=import_yosys_json(ROOT/'examples/yosys/counter.json',top='counter',clocks=['clk'])
    ff=[cell for cell in ir.value['instances']if cell['type']=='FDRE'][:2]
    for cell,name in zip(ff,['positive','negative\\雪/hier']):
        old=cell['id'];cell['id']=name
        for net in ir.value['nets']:
            for endpoint in net['drivers']+net['sinks']:
                if endpoint['instance']==old:endpoint['instance']=name
    source=tmp_path/'source.sqlite';write_compact_emuir(source,ir.value)
    original_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    contract=_contract('counter');contract['platform']='board'
    parts=['fpga0','fpga1','fpga2']
    contract['cut_nodes'][0]['sink_fpgas']=['fpga1','fpga2']
    contract['cut_nodes'][1]['source_fpgas']=['fpga2']
    contract['logic_segments'][1]['fpga']='fpga2'
    contract['logic_segments'][2]['fpga']='fpga0'
    contract['capture_requirements'][0]['fpga']='fpga0'
    semantic=tmp_path/'assignment.exact.bin'
    descriptor=write_phase3_exact_binary(semantic,contract,parts)
    assignment=tmp_path/'assignment.sqlite'
    assignment_ddl=''.join((ROOT/'src/emuflow/data/schema'/name).read_text()for name in
        ['phase3_assignment_core.sql','phase3_assignment_cuts.sql'])
    database(assignment,0x454d5541,4,assignment_ddl,dict(schema='emuflow.phase3-assignment-sqlite/v4',
        semantic_contract_binary=descriptor,semantic_contract_sha256=descriptor['semantic_contract_sha256'],cut_records=2))
    with closing(sqlite3.connect(assignment))as db, db:
        db.executemany('INSERT INTO parts VALUES(?,?)',enumerate(parts))
        db.executemany('INSERT INTO cuts VALUES(?,?,?,?,?,?,?,?)',[
            (0,'net0',0,'combinational',2,None,0,0),(1,'net1',2,'combinational',1,None,1,1)])
    identity=dict(original_ir_sha256=original_hash,path_database_sha256='b'*64,routes_sha256='c'*64,
        schedule_sha256='d'*64,semantic_sha256=descriptor['semantic_contract_sha256'],timing_io_sha256='f'*64)
    schedule=tmp_path/'schedule.sqlite'
    header=dict(design='counter',platform='board',semantic_contract_sha256=identity['semantic_sha256'],
        word_event_policy='periodic-lvds-8bit-word-load-to-rx-visible-v2',transport_semantics='sampled-virtual-wire',
        route_constraints=dict(frame_slots=1024))
    database(schedule,0x454d5554,3,(ROOT/'src/emuflow/data/schema/phase5_schedule.sql').read_text(),
        dict(logical_schema='emuflow.tdm-schedule/v1',header=header,logical_sha256=identity['schedule_sha256'],entry_relation_records=3))
    entries=[('e0','d0','net0',0,'l0','fpga0','fpga1',1,7),
             ('e1','d0','net0',1,'l1','fpga1','fpga2',9,15),
             ('e2','d1','net1',0,'l2','fpga2','fpga0',17,23)]
    with closing(sqlite3.connect(schedule))as db, db:
        db.executemany('INSERT INTO schedule_routes VALUES(?,?,?,?)',[(0,'d0','net0','fpga0'),(1,'d1','net1','fpga2')])
        db.executemany('INSERT INTO schedule_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ((i,e,d,n,h,l,f,t,l,s,0,0,a,16)for i,(e,d,n,h,l,f,t,s,a)in enumerate(entries)))
    clocks=tmp_path/'clocks.sqlite'
    clock_ddl='''CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE sources(source_seq INTEGER PRIMARY KEY,cell_seq INTEGER,port_seq INTEGER,bit INTEGER,UNIQUE(cell_seq,port_seq,bit));
        CREATE TABLE clocks(clock_seq INTEGER PRIMARY KEY,name TEXT,declared_period_ns REAL,engine_period_ns REAL);
        CREATE TABLE facts(source_seq INTEGER,clock_seq INTEGER,phase_ns REAL,PRIMARY KEY(source_seq,clock_seq,phase_ns)) WITHOUT ROWID;'''
    database(clocks,0x454d534c,1,clock_ddl,dict(source=identity))
    with closing(sqlite3.connect(clocks))as db, db,closing(sqlite3.connect(source))as original:
        db.execute("INSERT INTO metadata VALUES('schema','emuflow.source-launch-clocks/v1')")
        period=10.000000222044612
        db.execute('INSERT INTO clocks VALUES(0,?,?,?)',('clk',10.,period))
        for i,name in enumerate(['positive','negative\\雪/hier']):
            cell,port,bit=original.execute("SELECT e.instance_seq,e.port_seq,e.bit FROM endpoint_records e "
                "JOIN cell_records c ON c.seq=e.instance_seq JOIN pin_names p ON p.seq=e.port_seq "
                "WHERE c.id=? AND p.name='Q' AND e.role=0",(name,)).fetchone()
            db.execute('INSERT INTO sources VALUES(?,?,?,?)',(i,cell,port,bit))
        db.executemany('INSERT INTO facts VALUES(?,?,?)',[(0,0,0.),(0,0,period/4),(1,0,period/2)])
    pin_names=['positive/Q','negative\\\\雪/hier/Q']
    stores=[]
    for part in parts:
        boundaries=[];costs=[];segments=[]
        for i,(entry,demand,net,hop,link,frm,to,slot,arrival)in enumerate(entries):
            for kind,endpoint_part in [('tx',frm),('rx',to)]:
                if part!=endpoint_part:continue
                endpoint=f'{kind}{i}'
                shadow=f'shadow{i}'if kind=='rx'else'shadow0'if i==1 else None
                source_class='captured-shadow'if kind=='rx'else'forwarded-shadow'if i==1 else'dut-net'
                boundaries.append(dict(id=endpoint,kind=kind,schedule_entry=entry,demand=demand,logical_net=net,
                    link=link,peer=to if kind=='tx'else frm,lane=0,logical_lane=0,internal_signal=f'signal_{endpoint}',
                    source_class=source_class,merged_ir=dict(external_port=f'port_{endpoint}',external_port_bit=0,
                        external_net=f'wire_{endpoint}',logical_net=net,boundary_register_instances=[shadow]if shadow else[])))
                costs.append(dict(id=endpoint,kind=kind,schedule_entry=entry,delay_ns=10. if kind=='tx'else 2.,
                    sampling_cost_ns=9. if kind=='tx'else 4.9,start_object=f'{shadow}/Q'if shadow else'positive/Q',
                    end_object=f'{endpoint}/D',measurement='endpoint-exact'))
        if part=='fpga0':
            for i,pin in enumerate(pin_names):
                segments.append(dict(id=f'root{i}',kind='launch',system_path='static-exact-contract:segment000000',
                    member_path=f'root{i}',cut_index=i,fpga=part,replace_tx_endpoint='tx0',start_pin=pin,end_pin='port_tx0',
                    delay_ns=2.,sampling_cost_ns=2.,static_exact_segment_id='segment000000',start_object_kind='pin',
                    end_object_kind='port',measurement='endpoint-exact'))
            # An original-path query is NOT a complete-contract support row.
            segments.append({**segments[0],'id':'subset-only','system_path':'original-path','member_path':'member',
                'delay_ns':900.,'sampling_cost_ns':900.})
        if part=='fpga2':
            segments.append(dict(id='transition',kind='transition',system_path='static-exact-contract:segment000001',
                member_path='transition',cut_index=0,fpga=part,replace_tx_endpoint='tx2',start_pin='shadow1/Q',end_pin='port_tx2',
                delay_ns=4.,sampling_cost_ns=-2.,static_exact_segment_id='segment000001',start_object_kind='pin',
                end_object_kind='port',measurement='endpoint-exact'))
        common=dict(status='pass',design='counter',platform='board',fpga=part,provider='fixture',coverage={})
        segments.sort(key=lambda r:(r['system_path'],r['member_path'],r['cut_index'],r['id']))
        with write_boundary_identity_storage(tmp_path/f'{part}-identity.json',
            dict(common,schema='emuflow.boundary-identity/v1'),boundaries)as view:boundary_path=view.store_path
        with write_boundary_timing_storage(tmp_path/f'{part}-boundary.json',dict(common,schema='emuflow.boundary-timing/v1',
            qualification='fixture',measurement_scope='native-word-registered-interface',sampling_cost_policy=SAMPLING_COST_POLICY),costs)as view:cost_path=view.store_path
        with write_logic_segment_timing_storage(tmp_path/f'{part}-logic.json',header=dict(common,
            schema='emuflow.logic-segment-timing/v1',qualification='fixture',system_routes_sha256=identity['routes_sha256'],
            tdm_schedule_sha256=identity['schedule_sha256'],semantic_contract_sha256=identity['semantic_sha256'],
            sampling_cost_policy=SAMPLING_COST_POLICY),segments=segments)as view:logic_path=view.store_path
        stores.append(dict(logic=str(logic_path),identity=str(boundary_path),costs=str(cost_path)))
    return dict(source=str(source),clocks=str(clocks),assignment=str(assignment),semantic=str(semantic),
        schedule=str(schedule),identity=identity,partitions=stores,_fixture_contract=contract),tmp_path/'request.json'


def run(physical,mode=None):
    config,path=physical;path.write_text(json.dumps(config))
    return subprocess.run([str(word_physical_binding_test()),str(path),*([mode]if mode else[])],capture_output=True,text=True)


def test_complete_unselected_negative_root_and_relay_cost_bind_to_actual_owner(physical):
    result=run(physical);assert result.returncode==0,result.stderr
    report=json.loads(result.stdout)
    assert report['events']==[11,19,27]
    assert report['roots']==[[0,0,0],[0,0,25],[1,0,50]]
    assert report['costs']==[[[0,0,2,2],[0,1,2,2],[0,2,2,2]],[[1,0,10,9]],[[1,1,4,-2]]]
    assert report['inputs']==14
    assert report['checks']==[[0,0,0,2,55],[0,1,25,2,55],[0,2,50,2,55],
                              [1,0,85,10,96],[2,0,125,4,141]]


@pytest.mark.parametrize('kind',['schedule','semantic','timing_io','routes','path_database'])
def test_source_clock_context_cannot_cross_runs(physical,kind):
    physical[0]['identity'][kind+'_sha256']='0'*64
    result=run(physical);assert result.returncode!=0
    assert 'identity differs'in result.stderr or'another run'in result.stderr


@pytest.mark.parametrize('damage',[
    'missing-group','subset-only','wrong-shadow','duplicate-group','wrong-source-class','wrong-relay-shadow',
    'wrong-fixed-arc','wrong-route-root','duplicate-arrival','missing-rx','extra-cost','missing-phase',
    'unclocked-source','missing-part','repeated-part','wrong-cost-policy','wrong-phy-scope','rx-stage-failure',
    'eliminated-support','eliminated-boundary','missing-cost','wrong-group','wrong-boundary-entry',
    'text-cost','text-phase','zero-hop-relay',
])
def test_invalid_binding_never_guesses_fanins_or_zero_cost(physical,damage):
    config,_path=physical
    parts=config['partitions'];target=parts[2]['logic'];sql=''
    if damage in {'missing-group','subset-only'}:
        sql="DELETE FROM segments WHERE kind='transition' AND system_path GLOB 'static-exact-contract:*'"
    elif damage=='wrong-shadow':sql="UPDATE segments SET start_pin='shadow0/Q' WHERE kind='transition'"
    elif damage=='duplicate-group':
        sql="INSERT INTO segments SELECT seq+50,segment_id||'copy',kind,system_path,member_path||'copy',cut_index+1,fpga,replace_tx_endpoint,start_pin,end_pin,delay_ns,static_exact_segment_id,start_object_kind,end_object_kind,cone_anchor_object_kind,cone_anchor_pin,measurement,actual_start_object,actual_end_object,optimization_source,sampling_cost_ns FROM segments"
    elif damage=='wrong-source-class':target=parts[0]['identity'];sql="UPDATE endpoints SET source_class='forwarded-shadow' WHERE kind='tx'"
    elif damage=='wrong-relay-shadow':target=parts[1]['identity'];sql="UPDATE boundary_registers SET instance='other' WHERE endpoint_id='tx1'"
    elif damage=='wrong-fixed-arc':target=config['schedule'];sql='UPDATE schedule_entries SET arrival_slot=arrival_slot+1 WHERE seq=0'
    elif damage=='wrong-route-root':target=config['schedule'];sql="UPDATE schedule_routes SET source_fpga='fpga1' WHERE net='net0'"
    elif damage=='duplicate-arrival':target=config['schedule'];sql="UPDATE schedule_entries SET sink_fpga='fpga1' WHERE seq=1"
    elif damage=='missing-rx':target=parts[0]['identity'];sql="DELETE FROM endpoints WHERE kind='rx'"
    elif damage=='extra-cost':target=parts[0]['costs'];sql="INSERT INTO timings SELECT id||'extra',kind,schedule_entry||'extra',delay_ns,start_object,end_object,measurement,optimization_source,sampling_cost_ns FROM timings WHERE kind='rx'"
    elif damage=='missing-phase':target=config['clocks'];sql='DELETE FROM facts WHERE source_seq=1'
    elif damage=='unclocked-source':target=parts[0]['logic'];sql="UPDATE segments SET start_pin='clk' WHERE segment_id='root0'"
    elif damage=='missing-part':config['partitions']=parts[:-1]
    elif damage=='repeated-part':config['partitions']=[*parts,parts[0]]
    elif damage=='wrong-cost-policy':
        sql="UPDATE metadata SET value=json_set(value,'$.sampling_cost_policy','old') WHERE key='header'"
    elif damage=='wrong-phy-scope':target=parts[1]['costs'];sql="UPDATE metadata SET value=json_set(value,'$.measurement_scope','partition-boundary') WHERE key='header'"
    elif damage=='rx-stage-failure':target=parts[1]['costs'];sql="UPDATE timings SET sampling_cost_ns=5.001 WHERE kind='rx'"
    elif damage=='eliminated-support':
        sql="UPDATE segments SET measurement='optimized-away',delay_ns=NULL,sampling_cost_ns=NULL,optimization_source='s0'"
    elif damage=='eliminated-boundary':target=parts[1]['costs'];sql="UPDATE timings SET measurement='optimized-away',delay_ns=NULL,sampling_cost_ns=NULL,start_object=NULL,end_object=NULL,optimization_source='s0' WHERE kind='tx'"
    elif damage=='missing-cost':sql='UPDATE segments SET sampling_cost_ns=NULL'
    elif damage=='wrong-group':sql="UPDATE segments SET static_exact_segment_id='segment000000',system_path='static-exact-contract:segment000000'"
    elif damage=='wrong-boundary-entry':target=parts[1]['costs'];sql="UPDATE timings SET schedule_entry='different' WHERE kind='tx'"
    elif damage=='text-cost':sql="UPDATE segments SET sampling_cost_ns='bad'"
    elif damage=='text-phase':target=config['clocks'];sql="UPDATE facts SET phase_ns='bad' WHERE source_seq=1"
    elif damage=='zero-hop-relay':target=config['schedule'];sql='UPDATE schedule_entries SET hop=0 WHERE seq=1'
    if sql:
        with closing(sqlite3.connect(target))as db, db:db.execute(sql)
    result=run(physical);assert result.returncode!=0,(damage,result.stdout)


def test_generation_lease_survives_binding(physical):
    result=run(physical,'mutate');assert result.returncode!=0 and'changed'in result.stderr


def test_typed_root_identity_rejects_even_byte_identical_other_owner(physical,tmp_path):
    config,_=physical;copy=tmp_path/'other-clocks.sqlite';shutil.copyfile(config['clocks'],copy);config['other_clocks']=str(copy)
    result=run(physical,'mix-clock-owner');assert result.returncode!=0 and'another artifact'in result.stderr


def test_owner_pair_relocation_and_partition_enumeration_are_deterministic(physical,tmp_path):
    config,path=physical
    before=run(physical);assert before.returncode==0,before.stderr
    original=path.parent;moved=tmp_path/'moved';moved.mkdir()
    all_paths=[Path(config[key])for key in ['source','clocks','assignment','semantic','schedule']]
    all_paths.extend(Path(value)for part in config['partitions']for value in part.values())
    for source in all_paths:shutil.copyfile(source,moved/source.name)
    for key in ['source','clocks','assignment','semantic','schedule']:config[key]=str(moved/Path(config[key]).name)
    config['partitions']=[{k:str(moved/Path(v).name)for k,v in part.items()}for part in reversed(config['partitions'])]
    after=run((config,moved/'request.json'));assert after.returncode==0,after.stderr
    assert after.stdout==before.stdout


def replace_semantic(physical):
    config,_=physical
    descriptor=write_phase3_exact_binary(Path(config['semantic']),config['_fixture_contract'],['fpga0','fpga1','fpga2'])
    seal=descriptor['semantic_contract_sha256'];config['identity']['semantic_sha256']=seal
    with closing(sqlite3.connect(config['assignment']))as db, db:
        db.executemany('UPDATE metadata SET value=? WHERE key=?',[
            (json.dumps(descriptor),'semantic_contract_binary'),(json.dumps(seal),'semantic_contract_sha256')])
    with closing(sqlite3.connect(config['schedule']))as db, db:
        db.execute("UPDATE metadata SET value=json_set(value,'$.semantic_contract_sha256',?) WHERE key='header'",(seal,))
    with closing(sqlite3.connect(config['clocks']))as db, db:
        db.execute("UPDATE metadata SET value=json_set(value,'$.semantic_sha256',?) WHERE key='source'",(seal,))
    for part in config['partitions']:
        with closing(sqlite3.connect(part['logic']))as db, db:
            db.execute("UPDATE metadata SET value=json_set(value,'$.semantic_contract_sha256',?) WHERE key='header'",(seal,))


def test_only_explicit_semantic_constant_can_have_no_dynamic_launches(physical):
    config,_=physical
    config['_fixture_contract']['logic_segments'][0]['source_semantics']='configuration-stable-constant'
    replace_semantic(physical)
    with closing(sqlite3.connect(config['partitions'][0]['logic']))as db, db:
        db.execute("DELETE FROM segments WHERE system_path GLOB 'static-exact-contract:*'")
    result=run(physical);assert result.returncode==0,result.stderr
    report=json.loads(result.stdout)
    assert report['roots']==[] and report['events']==[1,9,17]
    assert report['costs'][0]==[]


def test_root_ids_are_canonical_across_multiple_source_partitions(physical):
    config,_=physical;contract=config['_fixture_contract']
    contract['logic_segments'].append(dict(id='segment000003',kind='launch_to_tx',fpga='fpga2',sink_cut_net='net1'))
    contract['cut_nodes'][1]['source_segment_ids'].append('segment000003')
    contract['metrics']['logic_segments']=4
    replace_semantic(physical)
    with closing(sqlite3.connect(config['partitions'][0]['logic']))as src, src,closing(sqlite3.connect(config['partitions'][2]['logic']))as dst, dst:
        row=src.execute("SELECT * FROM segments WHERE segment_id='root1'").fetchone()
        fields=[r[1]for r in src.execute('PRAGMA table_info(segments)')];data=dict(zip(fields,row))
        src.execute("DELETE FROM segments WHERE segment_id='root1'")
        data.update(seq=50,segment_id='second-part-root',static_exact_segment_id='segment000003',
            system_path='static-exact-contract:segment000003',member_path='second-part-root',fpga='fpga2',
            replace_tx_endpoint='tx2',end_pin='port_tx2')
        dst.execute('INSERT INTO segments VALUES('+','.join('?'for _ in fields)+')',[data[k]for k in fields])
    before=run(physical);assert before.returncode==0,before.stderr
    config['partitions'].reverse()
    after=run(physical);assert after.returncode==0,after.stderr
    assert after.stdout==before.stdout
    report=json.loads(after.stdout)
    assert report['roots']==[[0,0,0],[0,0,25],[1,0,50]] and report['events']==[7,15,23]


def fixture_engine_checks(report):
    from emuflow.global_sta import EventCheck
    checks=[];support=iter(report['checks']);current=next(support,None)
    for transfer,word in enumerate(report['events']):
        while current is not None and current[0]==transfer:
            _transfer,fanin,launch,data,required=current
            checks.append(EventCheck(f'word-{transfer}','tx',str(fanin),launch,(data,),required))
            current=next(support,None)
        # These synthetic observations satisfy the generic engine interface;
        # they are not original DUT capture checks or a global coverage proof.
        for role in ['target','runtime']:
            checks.append(EventCheck(f'word-{transfer}',role,'fixture-observation',word*5.,(30.,),(word+6)*5.))
    assert current is None
    return checks


def test_exported_support_and_observations_use_contiguous_engine_bundles(physical):
    from emuflow.global_sta import validate_checks
    result=run(physical);assert result.returncode==0,result.stderr
    checks=fixture_engine_checks(json.loads(result.stdout))
    assert validate_checks(checks)==checks


@pytest.mark.skipif(not os.environ.get('EMUFLOW_TEST_OPENSTA'),reason='explicit qualified OpenSTA required')
def test_bound_model_checks_run_in_independent_opensta_then_validate_first_occurrence(physical):
    from emuflow.global_sta import run_event_checks
    config,path=physical
    def measured(mode):
        result=run(physical);assert result.returncode==0,result.stderr
        report=json.loads(result.stdout);checks=fixture_engine_checks(report)
        rows=run_event_checks(checks,path.parent/mode,os.environ['EMUFLOW_TEST_OPENSTA'])
        by_id={(row['path'],row['role'],row['event']):row['slack_ns']for row in rows}
        assert min(by_id.values())>=-1e-5
        config['slacks']=[by_id[(f'word-{i}','tx',str(j))]for i,j,*_ in report['checks']]
        return run(physical,'validate')
    accepted=measured('on-time');assert accepted.returncode==0,accepted.stderr
    config['selected']=[11,19,29]
    rejected=measured('late')
    assert rejected.returncode!=0 and'earlier word occurrence'in rejected.stderr


def test_repeated_sources_and_twelve_partitions_do_not_require_all_store_attachments(physical):
    config,path=physical;cuts=512;parts=[f'fpga{i:02d}'for i in range(12)]
    contract=config['_fixture_contract'];contract.update(cut_nodes=[],logic_segments=[],capture_requirements=[],dependency_edges=[])
    contract['metrics']=dict(transported_cut_nets=cuts,logic_segments=cuts,capture_requirements=0,dependency_edges=0)
    for cut in range(cuts):
        net=f'net{cut:06d}';group=f'segment{cut:06d}'
        contract['cut_nodes'].append(dict(net=net,cut_class='combinational',source_fpgas=parts[:1],sink_fpgas=parts[1:],
            dependency_level=0,combinational_dependency_depth=0,predecessor_cut_nets=[],source_segment_ids=[group],capture_segment_ids=[]))
        contract['logic_segments'].append(dict(id=group,kind='launch_to_tx',fpga=parts[0],sink_cut_net=net))
    descriptor=write_phase3_exact_binary(Path(config['semantic']),contract,parts)
    config['identity']['semantic_sha256']=descriptor['semantic_contract_sha256']
    with closing(sqlite3.connect(config['assignment']))as db, db:
        db.execute('DELETE FROM parts');db.executemany('INSERT INTO parts VALUES(?,?)',enumerate(parts))
        db.execute('DELETE FROM cuts')
        db.executemany('INSERT INTO cuts VALUES(?,?,?,?,?,?,?,?)',
            ((cut,f'net{cut:06d}',0,'combinational',len(parts)-1,None,0,0)for cut in range(cuts)))
        db.executemany('UPDATE metadata SET value=? WHERE key=?',[
            (json.dumps(descriptor),'semantic_contract_binary'),(json.dumps(descriptor['semantic_contract_sha256']),'semantic_contract_sha256'),
            (json.dumps(cuts),'cut_records')])
    with closing(sqlite3.connect(config['clocks']))as db, db:
        db.execute("UPDATE metadata SET value=json_set(value,'$.semantic_sha256',?) WHERE key='source'",(descriptor['semantic_contract_sha256'],))
    entries=[]
    for cut in range(cuts):
        for sink in range(1,len(parts)):
            seq=len(entries);entries.append((seq,f'e{seq}',f'd{cut}',f'net{cut:06d}',0,f'l{sink}',parts[0],parts[sink],f'l{sink}',1,cut,0,7,16))
    with closing(sqlite3.connect(config['schedule']))as db, db:
        db.execute('DELETE FROM schedule_entries');db.execute('DELETE FROM schedule_routes')
        db.executemany('INSERT INTO schedule_entries VALUES('+','.join('?'for _ in range(14))+')',entries)
        db.executemany('INSERT INTO schedule_routes VALUES(?,?,?,?)',((i,f'd{i}',f'net{i:06d}',parts[0])for i in range(cuts)))
        db.execute("UPDATE metadata SET value=json_set(value,'$.semantic_contract_sha256',?) WHERE key='header'",(descriptor['semantic_contract_sha256'],))
        db.execute("UPDATE metadata SET value=? WHERE key='entry_relation_records'",(json.dumps(len(entries)),))
    config['partitions']=[]
    for part in parts:
        endpoints=[];costs=[];segments=[]
        for seq,entry,demand,net,hop,link,frm,to,key,slot,lane,ready,arrival,ratio in entries:
            if part not in (frm,to):continue
            kind='tx'if part==frm else'rx';endpoint=f'{kind}{seq}';shadow=f'shadow{seq}'
            endpoints.append(dict(id=endpoint,kind=kind,schedule_entry=entry,demand=demand,logical_net=net,link=link,
                peer=to if kind=='tx'else frm,lane=lane,logical_lane=lane,internal_signal=endpoint,
                source_class='dut-net'if kind=='tx'else'captured-shadow',merged_ir=dict(external_port='data_'+link,
                    external_port_bit=lane,external_net=endpoint,logical_net=net,
                    boundary_register_instances=[]if kind=='tx'else[shadow])))
            costs.append(dict(id=endpoint,kind=kind,schedule_entry=entry,delay_ns=2.,sampling_cost_ns=2.,
                start_object='positive/Q'if kind=='tx'else'port',end_object='tx/D'if kind=='tx'else shadow+'/D',measurement='endpoint-exact'))
            if kind=='tx':
                group=f'segment{int(demand[1:]):06d}'
                for root,pin in enumerate(['positive/Q','negative\\\\雪/hier/Q']):
                    name=f'root{seq:06d}-{root}'
                    segments.append(dict(id=name,kind='launch',system_path='static-exact-contract:'+group,member_path=name,
                        cut_index=root,fpga=part,replace_tx_endpoint=endpoint,start_pin=pin,end_pin='port',delay_ns=2.,
                        sampling_cost_ns=2.,static_exact_segment_id=group,start_object_kind='pin',end_object_kind='port',measurement='endpoint-exact'))
        common=dict(status='pass',design='counter',platform='board',fpga=part,provider='fixture',coverage={})
        with write_boundary_identity_storage(path.parent/f'large-{part}-identity.json',dict(common,schema='emuflow.boundary-identity/v1'),endpoints)as view:bi=view.store_path
        with write_boundary_timing_storage(path.parent/f'large-{part}-costs.json',dict(common,schema='emuflow.boundary-timing/v1',
            sampling_cost_policy=SAMPLING_COST_POLICY,qualification='fixture',measurement_scope='native-word-registered-interface'),costs)as view:bc=view.store_path
        segments.sort(key=lambda r:(r['system_path'],r['member_path'],r['cut_index'],r['id']))
        with write_logic_segment_timing_storage(path.parent/f'large-{part}-logic.json',header=dict(common,
            schema='emuflow.logic-segment-timing/v1',qualification='fixture',sampling_cost_policy=SAMPLING_COST_POLICY,
            system_routes_sha256=config['identity']['routes_sha256'],tdm_schedule_sha256=config['identity']['schedule_sha256'],
            semantic_contract_sha256=config['identity']['semantic_sha256']),segments=segments)as view:lc=view.store_path
        config['partitions'].append(dict(logic=str(lc),identity=str(bi),costs=str(bc)))
    result=run(physical,'summary');assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==dict(events=5632,roots=3,checks=16896,first=11,last=11)
    assert len(result.stdout)<100
