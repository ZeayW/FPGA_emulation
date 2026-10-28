"""Native source coverage distinguishes dynamic inputs from startup state."""
import csv
import json
from pathlib import Path
import sqlite3

import pytest

from emuflow.errors import ValidationError
from emuflow.io import file_sha256
from emuflow.native_word_context import qualify_native_word_source_contexts
from emuflow.word_transport_rtl import PHYSICAL_WORD_CALENDAR_POLICY
from tests.test_native_word_source_catalog import source,prepare


def ref(path):return dict(path=str(path),sha256=file_sha256(path))


@pytest.fixture
def context(source):
    fpga='fpga0';mapped=source.mapped[fpga]
    with sqlite3.connect(mapped)as db:
        db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
        db.execute('INSERT INTO metadata VALUES(?,?)',('ports_json',json.dumps({'fabric_clk':{'bits':[2]}})))
        db.execute('INSERT INTO cells VALUES(?,?,?,?)',(40,'payload_state','FDRE',json.dumps({'parameters':{'IS_C_INVERTED':'0'}})))
        db.executemany('INSERT INTO cell_pins VALUES(?,?,?,?,?,?)',[(40,'C',0,0,0,2),(40,'Q',0,1,0,400)])
        db.execute('INSERT INTO netnames VALUES(?,?)',('__emuflow_transport__/payload_active',json.dumps({'bits':[400]})))
    prep=prepare(source)
    root=source.root
    lineage=root/'lineage.sqlite'
    with sqlite3.connect(lineage)as db:
        db.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);CREATE TABLE objects(stage TEXT,name TEXT,origin INTEGER);')
        db.executemany('INSERT INTO metadata VALUES(?,?)',[('mapped_index_sha256',file_sha256(mapped)),('retained_stage','routed')])
        db.executemany('INSERT INTO objects VALUES(?,?,?)',[('routed','payload_replica',40),('routed','unknown_rx',9)])
    checkpoint=root/'routed.dcp';checkpoint.write_bytes(b'unchanged routed design')
    xdc=root/'runtime.xdc';xdc.write_text('set_input_delay -max 2.5 -clock [get_clocks {emuflow_dut_clk_0}] [get_ports {data[*]}]\n')
    word=root/'word.json';word.write_text(json.dumps(dict(schema='emuflow.vivado-word-top-preparation/v1',status='pass',fpga=fpga,
        external_services=dict(contract='common synchronous global startup/readiness consensus and pausible DUT clock service; no distributed barrier implementation',inputs=['__emuflow_payload_enable']))))
    clock=root/'clock.tsv';clock.write_text('clock_hex\tport_hex\tperiod_ns\n'+b'emuflow_dut_clk_0'.hex()+'\t'+b'clk'.hex()+'\t100\n')
    tx=root/'tx.tsv';tx.write_text('source_kind\tsource_hex\n'+''.join(k+'\t'+n.encode().hex()+'\n'for k,n in[
        ('infrastructure','payload_replica'),('ports','data[7]'),('ports','__emuflow_payload_enable')]))
    missing=root/'missing.tsv';missing.write_text('source_origin\n')
    record=dict(observation=dict(status='complete',fpga=fpga,source_generations=[],counts=dict(dut_clocks=1),
        checkpoint=ref(checkpoint),source_directory=prep['directory'],files={'native-costs.tsv':ref(tx),'missing-rx-roles.tsv':ref(missing)}),
        input_identity=dict(mapped_index=ref(mapped)),lineage=ref(lineage),clock_contract=ref(clock),sources=prep)
    census=root/'census';census.mkdir();c=b'emuflow_dut_clk_0'.hex()
    (census/'source-census.tsv').write_text('capture_clock_hex\ttarget_pins\tnative_startpoints\tdut_cells\trx_sources\tinfrastructure_cells\tports\tunclassified\n'+c+'\t20\t5\t2\t1\t1\t1\t0\n')
    (census/'external-sources.tsv').write_text('capture_clock_hex\tsource_kind\tsource_hex\tsource_origin\n'+c+'\tinfrastructure_cells\t'+b'payload_replica'.hex()+'\ts40\n'+c+'\tports\t'+b'data[0]'.hex()+'\t\n')
    (census/'rx-sources.tsv').write_text('capture_clock_hex\tsource_origin\n'+c+'\ts9\n')
    receipt=census/'summary.json'
    def seal():
        receipt.write_text(json.dumps(dict(status='complete-unconditional-native-capture-source-census',fpga=fpga,
            original_checkpoint_unchanged=True,input_sha256={str(checkpoint):file_sha256(checkpoint),str(xdc):file_sha256(xdc)},
            files={n:ref(census/n)for n in('source-census.tsv','external-sources.tsv','rx-sources.tsv')})))
    seal()
    def run(**kwargs):
        return qualify_native_word_source_contexts(record,runtime_constraints=ref(xdc),word_preparation=ref(word),
            calendar_policy=kwargs.pop('calendar_policy',PHYSICAL_WORD_CALENDAR_POLICY),
            output=root/'contexts.json',capture_census=kwargs.pop('capture_census',ref(receipt)),**kwargs)
    return dict(run=run,root=root,record=record,census=census,seal=seal,xdc=xdc,mapped=mapped,word=word)


def test_roles_follow_source_connectivity_and_input_environment(context):
    result=context['run']()
    assert result['status']=='complete'and result['unresolved_contexts']==0
    roles={r['source']:r for r in result['source_contexts']}
    assert roles['data[7]']['max_arrival_ns']==2.5and roles['data[7]']['clock']=='emuflow_dut_clk_0'
    assert roles['payload_replica']['source_origin']=='s40'
    assert roles['__emuflow_payload_enable']['role']=='runtime-startup-service'
    assert result['original_input_minimum_delays']=='not-qualified'and result['timing_acceptance']=='separate'


@pytest.mark.parametrize('missing',['capture','calendar','control-alias','dynamic-input'])
def test_unknown_membership_stays_incomplete_without_discarding_observations(context,missing):
    options={}
    if missing=='capture':options['capture_census']=None
    if missing=='calendar':options['calendar_policy']='unknown-calendar'
    if missing=='control-alias':
        with sqlite3.connect(context['mapped'])as db:db.execute("DELETE FROM netnames WHERE id LIKE '%payload_active'")
        context['record']['input_identity']['mapped_index']=ref(context['mapped'])
        with sqlite3.connect(Path(context['record']['lineage']['path']))as db:
            db.execute('UPDATE metadata SET value=?WHERE key=?',(file_sha256(context['mapped']),'mapped_index_sha256'))
        context['record']['lineage']=ref(Path(context['record']['lineage']['path']))
    if missing=='dynamic-input':context['xdc'].write_text('');context['seal']()
    result=context['run'](**options)
    assert result['status']=='incomplete'
    if missing!='capture':assert result['unresolved_contexts']>0
    assert not any('sampling_cost_ns'in row for row in result['source_contexts'])


@pytest.mark.parametrize('damage',['checkpoint','environment','family','duplicate-source','missing-rx'])
def test_different_census_owners_and_incomplete_populations_cannot_claim_coverage(context,damage):
    census=context['census'];receipt=census/'summary.json'
    if damage in('checkpoint','environment'):
        value=json.loads(receipt.read_text());path=context['record']['observation']['checkpoint']['path']if damage=='checkpoint'else str(context['xdc'])
        value['input_sha256'][path]='0'*64;receipt.write_text(json.dumps(value))
    if damage=='family':
        p=census/'source-census.tsv';p.write_text(p.read_text().replace(b'emuflow_dut_clk_0'.hex(),b'emuflow_dut_clk_1'.hex()));context['seal']()
    if damage=='duplicate-source':
        p=census/'external-sources.tsv';p.write_text(p.read_text()+p.read_text().splitlines(True)[1]);context['seal']()
    if damage=='missing-rx':
        p=Path(context['record']['observation']['files']['missing-rx-roles.tsv']['path']);p.write_text('source_origin\ns9\n')
        context['record']['observation']['files']['missing-rx-roles.tsv']=ref(p)
    with pytest.raises(ValidationError):context['run']()


def test_identical_relocated_runtime_constraints_keep_original_census_owner(context):
    from emuflow.native_word_context import qualify_native_word_source_contexts
    relocated=context['root']/'relocated';relocated.mkdir();xdc=relocated/'runtime.xdc';xdc.write_bytes(context['xdc'].read_bytes())
    result=qualify_native_word_source_contexts(context['record'],runtime_constraints=ref(xdc),word_preparation=ref(context['word']),
        calendar_policy=PHYSICAL_WORD_CALENDAR_POLICY,output=relocated/'contexts.json',capture_census=ref(context['census']/'summary.json'))
    assert result['status']=='complete'


@pytest.mark.parametrize('damage',[None,'checkpoint','census','missing-query','dynamic'])
def test_native_constant_source_properties_keep_physical_acceptance_separate(context,damage):
    from tests.test_native_word_source_constants import facts
    root=context['root'];name='native/A2_DATA[21]'
    tx=Path(context['record']['observation']['files']['native-costs.tsv']['path'])
    tx.write_text(tx.read_text()+'unclassified\t'+name.encode().hex()+'\n')
    context['record']['observation']['files']['native-costs.tsv']=ref(tx)
    path=root/'native-properties.tsv'
    facts(path,ref='DSP_A_B_DATA',outputs=['A2_DATA[20]'if damage=='missing-query'else'A2_DATA[21]'],
          parameters={'AREG':'0','A_INPUT':'DIRECT'},inputs={'A[21]':None if damage=='dynamic'else 0})
    checkpoint=context['record']['observation']['checkpoint'];census=ref(context['census']/'summary.json')
    inputs={checkpoint['path']:checkpoint['sha256'],census['path']:census['sha256']}
    if damage=='checkpoint':inputs[checkpoint['path']]='0'*64
    if damage=='census':inputs[census['path']]='0'*64
    receipt=root/'native-properties.json';receipt.write_text(json.dumps(dict(
        status='complete-native-source-property-observations',fpga='fpga0',requests=1,input_sha256=inputs,file=ref(path))))
    if damage in('checkpoint','census'):
        with pytest.raises(ValidationError):context['run'](source_properties=ref(receipt))
    else:
        result=context['run'](source_properties=ref(receipt))
        assert result['status']==('incomplete'if damage in('dynamic','missing-query')else'complete')
        if damage is None:
            row=next(r for r in result['source_contexts']if r['source']==name)
            assert row['role']=='static-native-startpoint'and row['constant']['value']==0
        assert result['timing_acceptance']=='separate'
        assert not any('sampling_cost_ns'in row for row in result['source_contexts'])


def test_phase7c_keeps_source_membership_separate_and_binds_its_actual_inputs(context):
    from emuflow.native_word_phase7c import _source_contexts
    root=context['root'];output=root/'phase7c';output.mkdir();parents=[]
    evidence={'fpga0':context['record']};summary={'word_phy_implementation':{'physical_calendar_policy':PHYSICAL_WORD_CALENDAR_POLICY}}
    pending=_source_contexts(evidence,summary,output,parents,None)
    assert pending['status']=='incomplete'and pending['pending_partitions']==['fpga0']
    inputs={'fpga0':dict(runtime_constraints=ref(context['xdc']),word_preparation=ref(context['word']),
                        capture_census=ref(context['census']/'summary.json'))}
    complete=_source_contexts(evidence,summary,output,parents,inputs)
    assert complete['status']=='complete'and complete['pending_partitions']==[]
    assert complete['partitions']['fpga0']['unresolved_contexts']==0
    assert parents and (output/complete['partitions']['fpga0']['artifact']).exists()
    context['record']['word_preparation']=ref(context['word'])
    context['record']['observation']['source_context']={k:v for k,v in inputs['fpga0'].items()if k!='word_preparation'}
    assert _source_contexts(evidence,summary,output,parents,None)['status']=='complete'
    with pytest.raises(ValidationError):_source_contexts(evidence,summary,output,parents,{'other':inputs['fpga0']})
