"""Native fixed wiring and static transport configuration qualification."""
import copy
import csv
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

from emuflow.common.native_tools import native_file_generation
from emuflow.platform.default_platform import materialize_research_platform, research_platform_path
from emuflow.transport.virtual_board import INVENTORY_PATH, FIXED_WIRING_ROOT
from tests.board_reference import platform_reference
from tests.board_wiring_reference import build_virtual_board as wiring_reference
from tests.native_build import emuflow_native

ROOT=Path(__file__).resolve().parents[1]


def invoke(args, proof):
    return subprocess.run([str(emuflow_native()),'--report',str(proof),*map(str,args)],
        capture_output=True,text=True,timeout=60)


def record(path,schema,role=None):
    path=Path(path).resolve()
    row={'path':str(path),'schema':schema,'generation':list(native_file_generation(path))}
    if role is not None:row['role']=role
    return row


@pytest.mark.parametrize('count,width',[(2,16),(2,20),(2,24),(4,16),(4,20),(4,24)])
def test_complete_fixed_board_matches_independent_pair_and_allocation_oracle(tmp_path,count,width):
    source=tmp_path/'board.json'
    materialize_research_platform(source,fpga_count=count,wires_per_bank=width)
    platform=platform_reference(json.loads(source.read_text()))
    before=native_file_generation(source)
    table=FIXED_WIRING_ROOT/platform.virtual_board_wiring if platform.virtual_board_wiring else '-'
    output=tmp_path/'wiring.json';proof=tmp_path/'operation.json'
    result=invoke(['board','build-wiring',source,INVENTORY_PATH,table,output],proof)
    assert result.returncode==0,result.stderr
    actual=json.loads(output.read_text());expected=wiring_reference(platform)
    assert actual==expected
    assert json.dumps(actual,sort_keys=True,separators=(',',':'))==json.dumps(expected,sort_keys=True,separators=(',',':'))
    assert native_file_generation(source)==before
    assert json.loads(proof.read_text())['stage']=='board.build-wiring'


@pytest.mark.parametrize('damage',['duplicate-bank','duplicate-pin','duplicate-site','bad-bank-type','missing-n',
    'wrong-polarity','wrong-pair','wrong-function','ineligible','part','bank-exhaustion'])
def test_inventory_identity_violations_fail_before_replacing_publication(tmp_path,damage):
    source=research_platform_path(4);inventory=json.loads(INVENTORY_PATH.read_text())
    bank=inventory['banks'][0]
    p=next(p for p in bank['pins'] if p.get('differential_pair_id') is not None and p['polarity']=='p')
    n=next(n for n in bank['pins'] if n.get('differential_pair_id')==p['differential_pair_id'] and n['polarity']=='n')
    if damage=='duplicate-bank':inventory['banks'].append(copy.deepcopy(bank))
    if damage=='duplicate-pin':bank['pins'][1]['package_pin']=bank['pins'][0]['package_pin']
    if damage=='duplicate-site':bank['pins'][1]['site']=bank['pins'][0]['site']
    if damage=='bad-bank-type':bank['type']='HD'
    if damage=='missing-n':bank['pins'].remove(n)
    if damage=='wrong-polarity':p['polarity']='n'
    if damage=='wrong-pair':p['differential_pair_id']='L999'
    if damage=='wrong-function':p['pin_function']=None
    if damage=='ineligible':p['eligible_data']=False
    if damage=='part':inventory['part']='other-package'
    if damage=='bank-exhaustion':inventory['banks']=inventory['banks'][:35]
    altered=tmp_path/INVENTORY_PATH.name;altered.write_text(json.dumps(inventory))
    output=tmp_path/'wiring.json';output.write_text('prior publication\n')
    result=invoke(['board','build-wiring',source,altered,'-',output],tmp_path/'proof.json')
    assert result.returncode!=0
    assert output.read_text()=='prior publication\n'


@pytest.mark.parametrize('damage',['header','missing','extra','pin','swap-polarities','bank','contact','cable','quote'])
def test_checked_in_table_is_verified_record_for_record(tmp_path,damage):
    source=research_platform_path(2);platform=platform_reference(json.loads(source.read_text()))
    table=FIXED_WIRING_ROOT/platform.virtual_board_wiring
    rows=list(csv.reader(io.StringIO(table.read_text())))
    indexes={v:i for i,v in enumerate(rows[0])}
    if damage=='header':rows[0][0]='other'
    if damage=='missing':rows.pop()
    if damage=='extra':rows.append(rows[-1])
    if damage=='pin':rows[1][indexes['package_pin_a_p']]='wrong'
    if damage=='swap-polarities':
        for k in ('package_pin','site','pin_function'):
            p=indexes[k+'_a_p'];n=indexes[k+'_a_n'];rows[1][p],rows[1][n]=rows[1][n],rows[1][p]
    if damage=='bank':rows[1][indexes['bank_a']]='65'
    if damage=='contact':rows[1][indexes['connector_data_contact_p']]='999'
    if damage=='cable':rows[1][indexes['cable']]='wrong:cable0'
    text=io.StringIO();csv.writer(text).writerows(rows)
    altered=tmp_path/'wiring.csv';altered.write_text(text.getvalue() if damage!='quote' else text.getvalue()+'"unfinished')
    output=tmp_path/'wiring.json';result=invoke(['board','build-wiring',source,INVENTORY_PATH,altered,output],tmp_path/'proof.json')
    assert result.returncode!=0 and not output.exists()


def configuration(tmp_path,*,count=2):
    board=research_platform_path(count);platform=platform_reference(json.loads(board.read_text()))
    rows=[record(board,'boarddb-v1','board'),record(INVENTORY_PATH,'hp-package-pin-inventory-v1','inventory'),
        record(FIXED_WIRING_ROOT/platform.virtual_board_wiring,'fixed-wiring-csv-v1','wiring'),
        record(sys.executable,'external-tool-executable-v1','python'),
        record(ROOT/'src/python/emuflow/transport/phase6_oracle.py','phase6-oracle-source-v1','oracle')]
    request={'schema':'emuflow.phase6-configuration-input/v1','sources':rows,
             'options':{'clock_periods':{'fast':2.5,'slow':5.0},'cycles':3,'seed':123}}
    return request,platform


def test_native_phase6_configuration_publishes_whole_legacy_contract(tmp_path):
    request,platform=configuration(tmp_path)
    source=tmp_path/'request.json';source.write_text(json.dumps(request))
    output=tmp_path/'configuration';result=invoke(['phase6','prepare-configuration',source,output],tmp_path/'proof.json')
    assert result.returncode==0,result.stderr
    rows={r['role']:{k:v for k,v in r.items() if k!='role'} for r in request['sources']}
    expected={'schema':'emuflow.phase6-configuration/v1','configuration_sources':[rows['board']],
        'clock_periods':request['options']['clock_periods'],'virtual_board':wiring_reference(platform),
        'transport_cost':None,'small_oracle':{'python':rows['python'],'source':rows['oracle'],'cycles':3,'seed':123}}
    assert json.loads((output/'request.json').read_text())==expected
    proof=json.loads((tmp_path/'proof.json').read_text())
    assert len(proof['inputs'])==len(rows)+1
    assert proof['stage']=='phase6.prepare-configuration'


@pytest.mark.parametrize('damage',['unknown','missing','duplicate','generation','source-schema','relative','missing-inventory',
    'missing-table','bad-cycles','boolean-seed','clock-zero','clock-boolean','version'])
def test_configuration_rejects_incomplete_or_mixed_sources_atomically(tmp_path,damage):
    request,_=configuration(tmp_path)
    if damage=='unknown':request['sources'][0]['role']='unknown'
    if damage=='missing':request['sources']=request['sources'][1:]
    if damage=='duplicate':request['sources'].append(copy.deepcopy(request['sources'][0]))
    if damage=='generation':request['sources'][0]['generation'][2]+=1
    if damage=='source-schema':request['sources'][0]['schema']='exact-sha256-text-v1'
    if damage=='relative':request['sources'][0]['path']='board.json'
    if damage=='missing-inventory':request['sources']=[r for r in request['sources'] if r['role']!='inventory']
    if damage=='missing-table':request['sources']=[r for r in request['sources'] if r['role']!='wiring']
    if damage=='bad-cycles':request['options']['cycles']=-1
    if damage=='boolean-seed':request['options']['seed']=True
    if damage=='clock-zero':request['options']['clock_periods']={'clk':0}
    if damage=='clock-boolean':request['options']['clock_periods']={'clk':True}
    if damage=='version':request['schema']='emuflow.phase6-configuration-input/v999'
    source=tmp_path/'request.json';source.write_text(json.dumps(request));output=tmp_path/'configuration'
    result=invoke(['phase6','prepare-configuration',source,output],tmp_path/'proof.json')
    assert result.returncode!=0 and not output.exists()
    assert not list(tmp_path.glob('.phase6-configuration-*'))


@pytest.mark.parametrize('damage',[None,'platform','profile','model','schema','features','empty-resource',
    'extra-parameter','negative','boolean','nonfinite','provenance','fit'])
def test_cost_database_admission_matches_independent_existing_contract(tmp_path,damage):
    from emuflow.transport.transport_cost import validate_transport_cost_database
    from emuflow.common.errors import ValidationError
    request,platform=configuration(tmp_path)
    cost={'schema':'emuflow.transport-cost/v2','platform':platform.name,'profile':'nominal',
          'model':'production-transport-rtl-structural-v2','feature_names':['fixed_shell','rx_shadow_bits'],
          'resources':{'lut':{'fixed_shell':2.5,'rx_shadow_bits':1.0}},
          'provenance':{'class':'open_source_rtl_characterization','fit_sha256':'a'*64}}
    if damage=='platform':cost['platform']='other'
    if damage=='profile':cost['profile']='other'
    if damage=='model':cost['model']='other'
    if damage=='schema':cost['schema']='other'
    if damage=='features':cost['feature_names'].append('fixed_shell')
    if damage=='empty-resource':cost['resources']={}
    if damage=='extra-parameter':cost['resources']['lut']['other']=0
    if damage=='negative':cost['resources']['lut']['fixed_shell']=-1
    if damage=='boolean':cost['resources']['lut']['fixed_shell']=True
    if damage=='nonfinite':cost['resources']['lut']['fixed_shell']=float('inf')
    if damage=='provenance':cost['provenance']['class']='other'
    if damage=='fit':cost['provenance']['fit_sha256']='short'
    if damage is None:validate_transport_cost_database(cost,expected_platform=platform.name)
    else:
        with pytest.raises(ValidationError):validate_transport_cost_database(cost,expected_platform=platform.name)
    path=tmp_path/'cost.json';path.write_text(json.dumps(cost))
    request['sources'].append(record(path,'transport-cost-input-v1','cost'))
    source=tmp_path/'request.json';source.write_text(json.dumps(request));output=tmp_path/'configuration'
    result=invoke(['phase6','prepare-configuration',source,output],tmp_path/'proof.json')
    if damage is None:
        assert result.returncode==0,result.stderr
        value=json.loads((output/'request.json').read_text())
        assert value['transport_cost']==cost
        assert value['configuration_sources'][-1]==record(path,'transport-cost-input-v1')
    else:assert result.returncode!=0 and not output.exists()


def test_nonvirtual_configuration_needs_no_device_inventory(tmp_path):
    from tests.test_board_native import serial_board
    board=tmp_path/'board.json';board.write_text(json.dumps(serial_board()))
    request,_=configuration(tmp_path)
    request['sources']=[r for r in request['sources'] if r['role'] not in {'board','inventory','wiring'}]
    request['sources'].insert(0,record(board,'boarddb-v1','board'))
    source=tmp_path/'request.json';source.write_text(json.dumps(request));output=tmp_path/'configuration'
    result=invoke(['phase6','prepare-configuration',source,output],tmp_path/'proof.json')
    assert result.returncode==0,result.stderr
    assert json.loads((output/'request.json').read_text())['virtual_board'] is None
