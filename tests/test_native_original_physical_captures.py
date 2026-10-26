"""Native original-member joins keep half windows and final RX identity."""
from contextlib import closing

import hashlib
import csv
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from tests.test_native_word_physical_binding import physical
from tests.test_native_original_timing import original
from tests.native_build import native_original_physical_captures_test
from emuflow.local_path_timing_storage import write_local_path_timing_storage


@pytest.fixture
def bound(physical,original):
    config,path=physical;selector,payload,control=original
    # Three source members: two local and one crossing the last canonical RX.
    with closing(sqlite3.connect(payload))as db, db:
        db.execute('DELETE FROM paths WHERE ordinal>=3');db.execute('DELETE FROM clock_budgets WHERE path_ordinal>=3')
        db.execute("UPDATE paths SET start_instance='positive',start_object='positive/Q',end_instance=?,end_object=?,end_port='D'",('negative\\雪/hier','negative\\\\雪/hier/D'))
    manifest=json.loads(selector.read_text());manifest['payloads']['paths'].update(records=3,sha256=hashlib.sha256(payload.read_bytes()).hexdigest());manifest['summary'].update(paths=3,clock_budget_paths=3);selector.write_text(json.dumps(manifest))
    seal=hashlib.sha256(selector.read_bytes()).hexdigest();config['identity']['path_database_sha256']=seal
    with closing(sqlite3.connect(config['clocks']))as db, db:db.execute("UPDATE metadata SET value=json_set(value,'$.path_database_sha256',?) WHERE key='source'",(seal,))
    names=['full','half-launch','half-capture']
    for i,part in enumerate(config['partitions']):
        header=dict(schema='emuflow.local-path-timing/v1',status='pass',design='counter',fpga=f'fpga{i}',provider='fixture',qualification='endpoint-exact',identity_schema='emuflow.local-path-identity/v2',coverage={'local_paths':2 if i==0 else 0},source=dict(path_database_sha256=seal,original_ir_sha256=config['identity']['original_ir_sha256'],assignment_sha256='a'*64,routes_sha256=config['identity']['routes_sha256'],original_paths=3,original_path_ids_sha256='f'*64),sampling_cost_policy='vivado-clock-aware-reference-v1')
        rows=[dict(id=name,kind='local',fpga=f'fpga{i}',clock_domain='clk',clock_period_ns=10.,start_pin='positive/Q',end_pin='negative\\\\雪/hier/D',measurement='explicit-routed-path-chain',required_time_ns=10.,path_pins=['positive/Q','negative\\\\雪/hier/D'],delay_ns=3.,sampling_cost_ns=5.)for name in names[:2]]if i==0 else[]
        local=path.parent/f'capture-local-{i}.json'
        with write_local_path_timing_storage(local,header,rows)as view:part['local']=str(view.store_path)
    with closing(sqlite3.connect(config['partitions'][0]['logic']))as db, db:
        db.execute("INSERT INTO segments VALUES(100,'original-capture','capture','original-group','half-capture',1,'fpga0',NULL,'shadow2/Q',? ,3.,'segment000002','pin','pin',NULL,NULL,'endpoint-exact',NULL,NULL,NULL,4.)",('negative\\\\雪/hier/D',))
    config.update(original_selector=str(selector),original_payload=str(payload))
    return config,path


def run(bound):
    config,path=bound;path.write_text(json.dumps(config))
    return subprocess.run([str(native_original_physical_captures_test()),str(path)],capture_output=True,text=True)


def test_every_original_capture_uses_its_native_cost_and_last_rx_once(bound):
    result=run(bound);assert result.returncode==0,result.stderr
    report=json.loads(result.stdout)
    assert report['original']==3 and report['local']==2 and report['cross']==1 and report['eliminated']==0
    assert report['records']==[['full',0,0,0.,3.,98.],['half-launch',0,0,50.,3.,98.],['half-capture',0,1,165.,3.,49.]]


def test_a_canonical_unobservable_source_keeps_the_original_member_without_fake_delay(bound,tmp_path,monkeypatch):
    from emuflow.emuir_compact import read_compact_emuir
    from emuflow.emuir_compact import write_compact_emuir
    from emuflow.vivado_netlist import emit_vivado_mapped_verilog
    from emuflow.vivado_lineage import import_cell_lineage,INVENTORY_HEADER
    from emuflow.vivado_optimization import build_optimization_proofs
    from tests.test_native_optimization_proofs import checked_context
    from tests.native_build import emuflow_native
    if not __import__('os').environ.get('EMUFLOW_NATIVE_ROOT'):
        native=tmp_path/'native';(native/'bin').mkdir(parents=True)
        (native/'bin/emuflow-native').symlink_to(emuflow_native());monkeypatch.setenv('EMUFLOW_NATIVE_ROOT',str(native))
    config,_=bound
    # Add a real, connected but unobserved sink to the same original source;
    # build its removal proof through the normal mapped/lineage producer.
    value=read_compact_emuir(Path(config['source']))
    dead=deepcopy(next(c for c in value['instances']if c['id']=='positive'));dead.update(id='dead',name='dead')
    value['instances'].append(dead)
    for net in value['nets']:
        for endpoint in list(net['sinks']):
            if endpoint['instance']=='positive'and endpoint['port']in {'C','R'}:
                net['sinks'].append(dict(endpoint,instance='dead'))
        if any(e['instance']=='positive'and e['port']=='Q'for e in net['drivers']):
            net['sinks'].append(dict(instance='dead',port='D',bit=0))
        net['fanout']=len(net['sinks'])
    value['nets'].append(dict(id='dead_q',name='dead_q',aliases=[],drivers=[dict(instance='dead',port='Q',bit=0)],sinks=[],fanout=0,cut_class='register_output'))
    source=tmp_path/'source-with-dead.sqlite';write_compact_emuir(source,value);config['source']=str(source)
    source_seal=hashlib.sha256(source.read_bytes()).hexdigest();config['identity']['original_ir_sha256']=source_seal
    with closing(sqlite3.connect(config['original_payload']))as db, db:
        db.execute("UPDATE paths SET end_instance='dead',end_object='dead/D' WHERE path_id='full'")
    selector=Path(config['original_selector']);manifest=json.loads(selector.read_text())
    manifest['payloads']['paths']['sha256']=hashlib.sha256(Path(config['original_payload']).read_bytes()).hexdigest();selector.write_text(json.dumps(manifest))
    path_seal=hashlib.sha256(selector.read_bytes()).hexdigest();config['identity']['path_database_sha256']=path_seal
    with closing(sqlite3.connect(source))as original, original,closing(sqlite3.connect(config['clocks']))as clocks, clocks:
        clocks.execute("UPDATE metadata SET value=json_set(value,'$.original_ir_sha256',?,'$.path_database_sha256',?) WHERE key='source'",(source_seal,path_seal))
        for i,name in enumerate(['positive','negative\\雪/hier']):
            row=original.execute("SELECT e.instance_seq,e.port_seq,e.bit FROM endpoint_records e JOIN cell_records c ON c.seq=e.instance_seq JOIN pin_names p ON p.seq=e.port_seq WHERE c.id=? AND p.name='Q' AND e.role=0",(name,)).fetchone()
            clocks.execute('UPDATE sources SET cell_seq=?,port_seq=?,bit=? WHERE source_seq=?',(*row,i))
    mapped_report=emit_vivado_mapped_verilog(source,tmp_path/'physical.v')
    mapped=Path(mapped_report['mapped_index']['path']);functions=Path(mapped_report['source_functions']['path']);digest=mapped_report['mapped_index']['sha256']
    inventory=tmp_path/'routed.tsv'
    with closing(sqlite3.connect(mapped))as db, db,inventory.open('w')as output:
        writer=csv.writer(output,delimiter='\t',lineterminator='\n');writer.writerow(INVENTORY_HEADER)
        cells=list(db.execute('SELECT seq,id,type FROM cells ORDER BY seq'));origin=next(row[0]for row in cells if row[1]=='dead')
        for i,(seq,name,kind)in enumerate(cells):
            if name!='dead':writer.writerow(['partition/'+name,kind,f's{seq}','',f'SLICE_X{i}Y0','AFF',str(i),'0','LEAF'])
    lineage=tmp_path/'routed.sqlite';import_cell_lineage(mapped,functions,{'routed':inventory},lineage,mapped_sha256=digest)
    proof=tmp_path/'proof.sqlite';build_optimization_proofs(mapped,lineage,proof,mapped_sha256=digest,fpga='fpga0')
    config['proof_context']=checked_context(proof,mapped,lineage,digest)
    for part in config['partitions']:
        with closing(sqlite3.connect(part['local']))as db, db:
            db.execute("UPDATE metadata SET value=json_set(value,'$.source.original_ir_sha256',?,'$.source.path_database_sha256',?) WHERE key='header'",(source_seal,path_seal))
            db.execute("UPDATE timings SET end_pin='dead/D',measurement='optimized-away',delay_ns=NULL,sampling_cost_ns=NULL,optimization_source=? WHERE id='full'",(f's{origin}',))
            db.execute("DELETE FROM path_pins WHERE path_id='full'")
    result=run(bound);assert result.returncode==0,result.stderr
    report=json.loads(result.stdout);assert report['original']==3 and report['eliminated']==1
    assert report['records'][0]==['full',0,0,None,None,None]


@pytest.mark.parametrize('damage',['missing-original','extra-original','duplicate-capture','wrong-shadow','wrong-end','wrong-capture-kind',
    'wrong-source','wrong-route','wrong-assignment','wrong-count','unmeasured','eliminated-without-owner','no-cost'])
def test_original_members_cannot_be_dropped_mixed_or_given_zero_delay(bound,damage):
    config,_=bound;part=config['partitions'][0];target=part['logic']
    if damage=='missing-original':target=part['local'];sql="DELETE FROM timings WHERE id='full'"
    elif damage=='extra-original':target=part['local'];sql="INSERT INTO timings SELECT seq+50,'extra',kind,fpga,clock_domain,clock_period_ns,required_time_ns,start_pin,end_pin,measurement,delay_ns,optimization_source,sampling_cost_ns FROM timings WHERE id='full'"
    elif damage=='duplicate-capture':sql="INSERT INTO segments SELECT seq+1,'duplicate',kind,'other-group',member_path,cut_index+1,fpga,replace_tx_endpoint,start_pin,end_pin,delay_ns,static_exact_segment_id,start_object_kind,end_object_kind,cone_anchor_object_kind,cone_anchor_pin,measurement,actual_start_object,actual_end_object,optimization_source,sampling_cost_ns FROM segments WHERE segment_id='original-capture'"
    elif damage=='wrong-shadow':sql="UPDATE segments SET start_pin='shadow1/Q' WHERE segment_id='original-capture'"
    elif damage=='wrong-end':sql="UPDATE segments SET end_pin='positive/D' WHERE segment_id='original-capture'"
    elif damage=='wrong-capture-kind':sql="UPDATE segments SET static_exact_segment_id='segment000001' WHERE segment_id='original-capture'"
    elif damage=='wrong-source':target=part['local'];sql="UPDATE metadata SET value=json_set(value,'$.source.original_ir_sha256','bad') WHERE key='header'"
    elif damage=='wrong-route':target=part['local'];sql="UPDATE metadata SET value=json_set(value,'$.source.routes_sha256','bad') WHERE key='header'"
    elif damage=='wrong-assignment':target=part['local'];sql="UPDATE metadata SET value=json_set(value,'$.source.assignment_sha256','bad') WHERE key='header'"
    elif damage=='wrong-count':target=part['local'];sql="UPDATE metadata SET value=json_set(value,'$.source.original_paths',4) WHERE key='header'"
    elif damage=='unmeasured':sql="INSERT INTO unmeasured_segments VALUES(0,'missing','capture','original-group','full','not measured')"
    elif damage=='eliminated-without-owner':sql="UPDATE segments SET measurement='optimized-away',delay_ns=NULL,sampling_cost_ns=NULL,optimization_source='s0' WHERE segment_id='original-capture'"
    else:sql="UPDATE segments SET sampling_cost_ns=NULL WHERE segment_id='original-capture'"
    with closing(sqlite3.connect(target))as db, db:db.execute(sql)
    result=run(bound);assert result.returncode!=0 and not result.stdout,(damage,result.stdout)
