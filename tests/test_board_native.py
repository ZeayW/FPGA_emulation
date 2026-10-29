"""Shared BoardDB authority versus independent finite contract fixtures."""
import copy
import json
from pathlib import Path
import subprocess

import pytest

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation
from tests.board_reference import platform_reference
from tests.native_build import emuflow_native

ROOT=Path(__file__).resolve().parents[1]
PRESETS=sorted((ROOT/'platforms/virtual').glob('*.json'))


def run(value,tmp_path,*,raw=None):
    source=tmp_path/'board.json';source.write_text(json.dumps(value,ensure_ascii=False) if raw is None else raw)
    output=tmp_path/'normalized.json';proof=tmp_path/'operation.json'
    result=subprocess.run([str(emuflow_native()),'--report',str(proof),'board','normalize',str(source),str(output)],capture_output=True,text=True,timeout=15)
    return result,output


@pytest.mark.parametrize('path',PRESETS,ids=lambda p:p.stem)
def test_all_current_board_presets_match_independent_contract(path,tmp_path):
    value=json.loads(path.read_text());before=native_file_generation(path)
    expected=platform_reference(value).to_dict()
    result,output=run(value,tmp_path)
    assert result.returncode==0,result.stderr
    actual=json.loads(output.read_text())
    assert actual==expected
    assert json.dumps(actual,sort_keys=True,separators=(',',':'))==json.dumps(expected,sort_keys=True,separators=(',',':'))
    assert native_file_generation(path)==before
    repeat=tmp_path/'again';repeat.mkdir()
    second,normalized=run(actual,repeat)
    assert second.returncode==0,second.stderr
    assert normalized.read_bytes()==output.read_bytes()


def serial_board():
    value={'schema':'emuflow.boarddb/v1','platform':{'name':'serial','kind':'hardware'},
      'fpgas':[{'id':f,'part':'device','capacity':{'lut':100},'utilization_limit':1} for f in ['b','a']],
      'links':[{'id':'ab','endpoints':['a','b'],'direction':'full_duplex','mode':'serial',
        'data_lanes_per_direction':2,'payload_bits_per_lane_per_cycle':64,'fabric_clock_mhz':100,
        'latency_cycles':1,'max_line_rate_gbps_per_lane':25,'endpoint_bindings':[
          {'fpga':f,'connector':'J1','mgt':'G0','lanes':[
            {'lane':i,'tx_package_pins':{'p':f'T{i}P','n':f'T{i}N'},'rx_package_pins':{'p':f'R{i}P','n':f'R{i}N'}}
            for i in [1,0]]} for f in ['b','a']]}],
      'board_services':{'clocks':[{'id':x,'signal':x,'kind':'system','frequency_mhz':100,'count':1,
        'frequency_qualification':'fixed','destination':'all','binding_status':'package_bound','qualification':'fixture'} for x in ['z','a']],
        'resets':[{'id':'reset','signal':'rst','polarity':'active_low','purpose':'system',
          'binding_status':'package_bound','qualification':'fixture'}]}}
    return value


def test_serial_bindings_and_services_are_canonical_without_changing_fpga_order(tmp_path):
    value=serial_board();result,output=run(value,tmp_path)
    assert result.returncode==0,result.stderr
    assert json.loads(output.read_text())==platform_reference(value).to_dict()
    assert [f['id'] for f in json.loads(output.read_text())['fpgas']]==['b','a']


@pytest.mark.parametrize('damage', ['schema','kind','model','wiring','empty-fpgas','duplicate-fpga','empty-capacity',
 'unknown-resource','negative-resource','boolean-resource','fractional-resource','zero-limit','boolean-limit','over-limit',
 'bank-inventory','missing-inventory','too-many-pairs','partial-bank','bad-cables','unknown-endpoint','self-link',
 'duplicate-link','direction','mode','sharing','sharing-direction','zero-lanes','boolean-lanes','zero-clock','negative-latency',
 'parallel-payload','ratio-quantum','ratio-order','ratio-field','ratio-negative','ratio-boolean','empty-name','unicode-empty-name'])
def test_native_and_independent_board_admission_reject_invalid_contracts(tmp_path,damage):
    value=json.loads((ROOT/'platforms/virtual/rapidwright_xcvu19p_4fpga_mesh.json').read_text())
    fpga=value['fpgas'][0];link=value['links'][0]
    if damage=='schema':value['schema']='unknown'
    if damage=='kind':value['platform']['kind']='other'
    if damage=='model':value['platform']['virtual_board_model']='other'
    if damage=='wiring':value['platform']['virtual_board_wiring']='../fixed.csv'
    if damage=='empty-fpgas':value['fpgas']=[]
    if damage=='duplicate-fpga':value['fpgas'].append(copy.deepcopy(fpga))
    if damage=='empty-capacity':fpga['capacity']={}
    if damage=='unknown-resource':fpga['capacity']['unknown']=1
    if damage=='negative-resource':fpga['capacity']['lut']=-1
    if damage=='boolean-resource':fpga['capacity']['lut']=True
    if damage=='fractional-resource':fpga['capacity']['lut']=1.5
    if damage=='zero-limit':fpga['utilization_limit']=0
    if damage=='boolean-limit':fpga['utilization_limit']=True
    if damage=='over-limit':fpga['utilization_limit']=1.1
    if damage=='bank-inventory':fpga['hp_io_bank_count']=1
    if damage=='missing-inventory':fpga.pop('hp_io_bank_count')
    if damage=='too-many-pairs':link['wires_per_bank']=25;link['data_lanes_per_direction']=25*link['bank_count']
    if damage=='partial-bank':link.pop('wires_per_bank')
    if damage=='bad-cables':link['cable_count']+=1
    if damage=='unknown-endpoint':link['endpoints'][0]='unknown'
    if damage=='self-link':link['endpoints'][0]=link['endpoints'][1]
    if damage=='duplicate-link':value['links'].append(copy.deepcopy(link))
    if damage=='direction':link['direction']='other'
    if damage=='mode':link['mode']='other'
    if damage=='sharing':link['capacity_sharing']='other'
    if damage=='sharing-direction':link['direction']='half_duplex'
    if damage=='zero-lanes':link['data_lanes_per_direction']=0
    if damage=='boolean-lanes':link['data_lanes_per_direction']=True
    if damage=='zero-clock':link['fabric_clock_mhz']=0
    if damage=='negative-latency':link['latency_cycles']=-1
    if damage=='parallel-payload':link['payload_bits_per_lane_per_cycle']=64
    if damage=='ratio-quantum':link['tdm_delay_model']['ratio_quantum']=3
    if damage=='ratio-order':link['tdm_delay_model']['min_ratio']=512
    if damage=='ratio-field':link['tdm_delay_model']['unknown']=1
    if damage=='ratio-negative':link['tdm_delay_model']['intercept_ns']=-1
    if damage=='ratio-boolean':link['tdm_delay_model']['min_ratio']=True
    if damage=='empty-name':value['platform']['name']=' '
    if damage=='unicode-empty-name':value['platform']['name']='\u00a0\u2003\u3000'
    with pytest.raises((ValidationError,ValueError,TypeError)):platform_reference(value)
    result,output=run(value,tmp_path)
    assert result.returncode!=0
    assert not output.exists()


@pytest.mark.parametrize('damage',['pin-same-lane','pin-other-lane','missing-lane','duplicate-lane','partial-binding','connector-collision',
 'pin-other-link','parallel-binding','negative-lane','nonserial-width','line-rate','service-id','clock-count','clock-binding','reset-polarity'])
def test_serial_and_service_violations_keep_original_independent_strength(tmp_path,damage):
    value=serial_board();link=value['links'][0];binding=link['endpoint_bindings'][0]
    if damage=='pin-same-lane':binding['lanes'][0]['rx_package_pins']['p']=binding['lanes'][0]['tx_package_pins']['p']
    if damage=='pin-other-lane':binding['lanes'][1]['tx_package_pins']['p']=binding['lanes'][0]['tx_package_pins']['p']
    if damage=='missing-lane':binding['lanes'].pop()
    if damage=='duplicate-lane':binding['lanes'][1]['lane']=binding['lanes'][0]['lane']
    if damage=='partial-binding':link['endpoint_bindings'].pop()
    if damage in {'connector-collision','pin-other-link'}:
        second=copy.deepcopy(link);second['id']='another';value['links'].append(second)
        if damage=='pin-other-link':
            for b in second['endpoint_bindings']:b['connector']='J2'
    if damage=='parallel-binding':link['mode']='parallel';link['payload_bits_per_lane_per_cycle']=1;link.pop('max_line_rate_gbps_per_lane')
    if damage=='negative-lane':binding['lanes'][0]['lane']=-1
    if damage=='nonserial-width':link['mode']='parallel'
    if damage=='line-rate':link['max_line_rate_gbps_per_lane']=1
    if damage=='service-id':value['board_services']['resets'][0]['id']='a'
    if damage=='clock-count':value['board_services']['clocks'][0]['count']=False
    if damage=='clock-binding':value['board_services']['clocks'][0]['binding_status']='other'
    if damage=='reset-polarity':value['board_services']['resets'][0]['polarity']='other'
    with pytest.raises((ValidationError,ValueError,TypeError)):platform_reference(value)
    result,output=run(value,tmp_path)
    assert result.returncode!=0 and not output.exists()


def test_optional_api_never_reenters_the_replaced_python_board_validator(tmp_path,monkeypatch):
    from emuflow.platform.platform import Platform,RatioDelayModel
    monkeypatch.setattr(RatioDelayModel,'from_dict',lambda *args,**kwargs: (_ for _ in ()).throw(AssertionError('Python board validation')))
    board=ROOT/'platforms/virtual/rapidwright_xcvu19p_4fpga_mesh.json'
    value=Platform.load(board)
    assert value.fpgas[0].effective_capacity['lut']==1021440
    assert value.links[0].tdm_delay_model.min_ratio==8
    again=Platform.from_dict(value.to_dict())
    assert again==value


@pytest.mark.parametrize('raw',['{"schema":"emuflow.boarddb/v1","schema":"other"}',
    '{"schema":"emuflow.boarddb/v1",', '[]', '{"schema":"emuflow.boarddb/v99"}'])
def test_invalid_raw_board_never_overwrites_previous_published_output(tmp_path,raw):
    output=tmp_path/'normalized.json';output.write_text('previous generation\n')
    result,path=run({},tmp_path,raw=raw)
    assert result.returncode!=0 and path.read_text()=='previous generation\n'


def test_native_board_publication_rejects_its_input_as_output(tmp_path):
    source=tmp_path/'board.json';source.write_text(json.dumps(serial_board()))
    original=source.read_bytes();generation=native_file_generation(source)
    result=subprocess.run([str(emuflow_native()),'board','normalize',str(source),str(source)],capture_output=True,text=True)
    assert result.returncode!=0 and source.read_bytes()==original
    assert native_file_generation(source)==generation
